"""Read tools end-to-end through the fake HTTP.

These assert the two things that break silently: a field the ENTITY does not
declare is dropped by pydantic without an error, and a comment read from the
wrong place comes back empty. Both are checked on real payload shapes.
"""

import handlers_read as hr
from conftest import (code_of, succeeded, text_of_result, TEST_KEY, TEST_TOKEN, board_payload, card_payload,
                      checklist_payload, comment_action_payload, label_payload,
                      list_payload, member_payload)
from models import (ListBoardsParams, ListCardsParams, ListChecklistsParams,
                    ListCommentsParams, ListLabelsParams, ListListsParams,
                    ListMembersParams, GetCardParams, SearchParams,
                    CheckAccessParams, ListAccountsParams, GetTokenLinkParams)


# --- no credentials ---------------------------------------------------------

async def test_list_boards_without_credentials_explains_the_pair(ctx, http):
    result = await hr.list_boards(ctx, ListBoardsParams())
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_CREDENTIALS_MISSING"
    # The message must name BOTH halves -- that is the whole onboarding trap.
    assert "key" in text_of_result(result).lower() and "token" in text_of_result(result).lower()


async def test_no_call_is_spent_without_credentials(ctx, http):
    await hr.list_boards(ctx, ListBoardsParams())
    assert http.calls == []


# --- boards ----------------------------------------------------------------

async def test_list_boards_returns_named_boards(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload(name="Client Work"),
               board_payload(board_id="6b" + "2" * 22, name="Personal")])
    result = await hr.list_boards(connected_ctx, ListBoardsParams())
    assert succeeded(result) is True
    names = [b.name for b in result.data.items]
    assert names == ["Client Work", "Personal"]


# --- lists -----------------------------------------------------------------

async def test_list_lists_of_the_only_board(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="To Do"), list_payload(
        list_id="7d" + "4" * 22, name="Done")])
    result = await hr.list_lists(connected_ctx, ListListsParams())
    assert succeeded(result) is True
    assert [l.name for l in result.data.items] == ["To Do", "Done"]


# --- cards -----------------------------------------------------------------

async def test_list_cards_carries_every_declared_field(connected_ctx, http):
    """Guards the silent-drop bug: pydantic ignores undeclared kwargs."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload()])
    result = await hr.list_cards(connected_ctx, ListCardsParams())
    assert succeeded(result) is True
    card = result.data.items[0]
    assert card.name == "Ship the landing page"
    assert card.due.startswith("2026-08-01")
    assert card.url.startswith("https://trello.com/c/")
    # `badges.comments` is where Trello keeps the comment count.
    assert card.comment_count == 2
    assert card.attachment_count == 1


async def test_get_card_reads_actual_content(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    card = card_payload()
    card["list"] = list_payload(name="In Progress")
    card["checklists"] = [checklist_payload()]
    http.push(card)
    result = await hr.get_card(connected_ctx, GetCardParams(card="8d" + "5" * 22))
    assert succeeded(result) is True
    assert result.data.desc.startswith("Copy is approved")
    # The nested list object is the only source of the list NAME without a
    # second round trip.
    assert result.data.list_name == "In Progress"


async def test_get_card_asks_for_badges_and_reports_the_counts(
        connected_ctx, http):
    """The counts a user sees on a card front must survive the round trip.

    Trello returns ONLY the fields a request names. `badges` was absent from
    CARD_FIELDS, so comment_count and attachment_count were structurally
    present and permanently 0 -- a card with two comments read as a card with
    none. Asserting the entity alone would not have caught it: the flattener
    was always correct, the REQUEST was not. So the request is asserted too.
    """
    http.push(member_payload())
    http.push([board_payload()])
    card = card_payload()
    card["badges"] = {"comments": 2, "attachments": 1,
                      "checkItems": 3, "checkItemsChecked": 1}
    http.push(card)
    result = await hr.get_card(connected_ctx, GetCardParams(card="8d" + "5" * 22))
    assert succeeded(result) is True

    assert "badges" in http.last_params().get("fields", "")
    assert result.data.comment_count == 2
    assert result.data.attachment_count == 1
    assert result.data.checklist_summary == "1/3"


# --- comments --------------------------------------------------------------

async def test_comments_read_from_actions(connected_ctx, http):
    """A comment is an ACTION; its text is nested at data.text."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([comment_action_payload(text="Ship it")])
    result = await hr.list_comments(
        connected_ctx, ListCommentsParams(card="8d" + "5" * 22))
    assert succeeded(result) is True
    comment = result.data.items[0]
    assert comment.text == "Ship it"
    assert comment.author == "Vlad Ivanco"
    assert comment.created.startswith("2026-07-21")


async def test_comments_request_filters_to_comment_actions(connected_ctx, http):
    """Without the filter, card history (updateCard etc.) floods the result."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([])
    await hr.list_comments(connected_ctx,
                          ListCommentsParams(card="8d" + "5" * 22))
    assert http.last_params().get("filter") == "commentCard"


# --- checklists ------------------------------------------------------------

async def test_checklist_counts_are_derived_from_items(connected_ctx, http):
    """Trello returns no done/total pair -- it must be counted from states."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([checklist_payload()])
    result = await hr.list_checklists(
        connected_ctx, ListChecklistsParams(card="8d" + "5" * 22))
    assert succeeded(result) is True
    item = result.data.items[0]
    assert (item.done_count, item.total_count) == (1, 2)
    assert "Write copy" in item.items


# --- labels & members ------------------------------------------------------

async def test_labels_listed_with_colour(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([label_payload(name="Urgent", color="red")])
    result = await hr.list_labels(connected_ctx, ListLabelsParams())
    assert succeeded(result) is True
    assert (result.data.items[0].name, result.data.items[0].color) == ("Urgent", "red")


async def test_members_fall_back_to_username(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([member_payload(full_name="")])
    result = await hr.list_members(connected_ctx, ListMembersParams())
    assert succeeded(result) is True
    assert result.data.items[0].username == "vladivanco"


# --- search ----------------------------------------------------------------

async def test_search_is_not_board_scoped(connected_ctx, http):
    """`/search` spans every board a token can see; demanding one is pointless."""
    http.push({"cards": [card_payload()], "boards": [], "organizations": []})
    result = await hr.search(connected_ctx, SearchParams(query="landing"))
    assert succeeded(result) is True
    assert result.data.total >= 1
    assert http.last_params().get("query") == "landing"


async def test_empty_search_is_refused_before_the_call(connected_ctx, http):
    result = await hr.search(connected_ctx, SearchParams(query="   "))
    assert succeeded(result) is False
    assert http.calls == []


# --- access report ---------------------------------------------------------

async def test_check_access_explains_emptiness(connected_ctx, http):
    http.push(member_payload())
    http.push([])
    result = await hr.check_access(connected_ctx, CheckAccessParams())
    assert succeeded(result) is True
    # The report must say WHY nothing is visible, not just that nothing is.
    assert result.data.boards_reachable == 0
    assert result.data.detail


async def test_list_accounts_never_returns_a_credential(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    result = await hr.list_accounts(connected_ctx, ListAccountsParams())
    assert succeeded(result) is True
    blob = result.data.model_dump_json()
    assert TEST_TOKEN not in blob
    assert TEST_KEY not in blob


async def test_list_accounts_names_the_account_and_counts_its_boards(
        connected_ctx, http):
    """The row must carry the owner's name and a real board count.

    `accounts.list_accounts` stores `member_name` and a `boards` LIST; this
    handler read `account_name` and `board_count`, keys that function never
    writes. Both defaulted quietly, so a working credential with a reachable
    board listed as an unnamed account with 0 boards -- which reads as "the
    token sees nothing", the exact thing a user checks this tool to rule out.
    """
    http.push(member_payload())
    http.push([board_payload()])
    result = await hr.list_accounts(connected_ctx, ListAccountsParams())
    assert succeeded(result) is True

    row = result.data.items[0]
    assert row.account_name == "Vlad Ivanco"
    assert row.title == "Vlad Ivanco"
    assert row.board_count == 1



# --- get_token_link ---------------------------------------------------------
# The Connect screen asked for a token while Trello's page shows no control
# that issues one, so the field had no reachable source. This tool makes the
# missing half obtainable from the half the user already has.

async def test_the_link_is_built_from_the_key(ctx, http):
    http.push("<html>Allow</html>", status=200)      # authorize page: key live
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key=TEST_KEY))
    assert succeeded(result) is True
    url = result.data.authorize_url
    assert "trello.com/1/authorize" in url
    assert f"key={TEST_KEY}" in url
    # read-only would connect fine and then fail on the first card edit;
    # a 30-day token would die silently. Both are support tickets.
    assert "scope=read,write" in url
    assert "expiration=never" in url


async def test_the_link_never_contains_a_token(ctx, http):
    """The link is built from the KEY. A token in a URL would leak the secret
    half into history, logs and clipboards."""
    http.push("<html>Allow</html>", status=200)
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key=TEST_KEY))
    assert TEST_TOKEN not in result.data.authorize_url
    assert "token=" not in result.data.authorize_url


async def test_a_dead_key_is_refused_before_the_allow_prompt(ctx, http):
    """A dead key still renders an Allow prompt that grants nothing usable, so
    sending the user there would waste the trip."""
    http.push("", status=404)              # authorize page: key unknown
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key=TEST_KEY))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_KEY_REJECTED"


async def test_an_unavailable_check_still_returns_the_link(ctx, http):
    """Not knowing is not evidence: a failed check must not be reported as a
    dead key, and the link is correct regardless."""
    http.push("", status=500)              # neither 200 nor 404 -> unknown
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key=TEST_KEY))
    assert succeeded(result) is True
    assert "not verified" in result.data.key_status
    assert f"key={TEST_KEY}" in result.data.authorize_url


async def test_the_secret_pasted_as_a_key_is_caught(ctx, http):
    """The Secret is 64 hex and sits under the key, so it gets pasted here.
    Verified against the live API: it cannot authorise any call."""
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key="e" * 64))
    assert succeeded(result) is False
    # No pointless authorize round trip for a value that cannot be a key.
    assert http.calls == []


async def test_an_empty_key_asks_for_it_plainly(ctx, http):
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key=""))
    assert succeeded(result) is False
    assert http.calls == []


async def test_the_result_warns_the_secret_is_not_a_token(ctx, http):
    """The single most likely mistake, named where the token is handed over."""
    http.push("<html>Allow</html>", status=200)
    result = await hr.get_token_link(ctx, GetTokenLinkParams(key=TEST_KEY))
    assert "secret" in result.data.next_step.lower()
