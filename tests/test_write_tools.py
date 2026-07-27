"""Write tools end-to-end through the fake HTTP.

These pin the four Trello shapes that a PATCH-and-envelope habit gets wrong:

* an update is PUT, not PATCH;
* the body is the fields themselves, with no `data` wrapper;
* a comment is posted to `actions/comments`, not to a comment resource;
* a cross-board move must send `idBoard` AND `idList`.

Plus the rule that matters more than any of them: a name is resolved to an id
BEFORE anything is written, so a typo fails without leaving a half-made card.
"""

import handlers_write as hw
from conftest import (TEST_KEY, TEST_TOKEN, board_payload, card_payload,
                      code_of, list_payload, member_payload, succeeded,
                      text_of_result)
from models import (AddCommentParams, ArchiveCardParams, CardLabelsParams,
                    CardMembersParams, ConnectAccountParams, CreateCardParams,
                    CreateListParams, DeleteCardParams, MoveCardParams,
                    UpdateCardParams)


# --- connect ----------------------------------------------------------------

async def test_connect_requires_both_halves(ctx, http):
    result = await hw.connect_account(
        ctx, ConnectAccountParams(key=TEST_KEY, token=""))
    assert succeeded(result) is False
    # No request is spent on a credential that cannot possibly authorise.
    assert http.calls == []


async def test_connect_verifies_before_saving(ctx, http):
    """A rejected pair must not be stored -- a saved broken credential looks
    connected and fails on every later call."""
    http.push("invalid token", status=401)
    result = await hw.connect_account(
        ctx, ConnectAccountParams(key=TEST_KEY, token=TEST_TOKEN))
    assert succeeded(result) is False
    assert await ctx.secrets.get("trello_credentials") in (None, "")


async def test_connect_saves_a_working_pair(ctx, http):
    http.push(member_payload())
    http.push([board_payload(name="Client Work")])
    result = await hw.connect_account(
        ctx, ConnectAccountParams(key=TEST_KEY, token=TEST_TOKEN))
    assert succeeded(result) is True
    stored = await ctx.secrets.get("trello_credentials")
    assert stored == f"{TEST_KEY}:{TEST_TOKEN}"


async def test_connect_never_echoes_the_token(ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    result = await hw.connect_account(
        ctx, ConnectAccountParams(key=TEST_KEY, token=TEST_TOKEN))
    assert TEST_TOKEN not in text_of_result(result)
    assert TEST_TOKEN not in str(result.data.model_dump())


# --- create_card ------------------------------------------------------------

async def test_create_card_resolves_the_list_by_name(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="To Do"), list_payload(
        list_id="7d" + "5" * 22, name="Doing")])
    http.push(card_payload(name="Fix hero"))

    result = await hw.create_card(connected_ctx, CreateCardParams(
        name="Fix hero", list_name="Doing"))
    assert succeeded(result) is True
    body = http.last_body()
    # The id of "Doing", never the word.
    assert body["idList"] == "7d" + "5" * 22
    assert body["name"] == "Fix hero"
    # No envelope: the fields ARE the body.
    assert "data" not in body


async def test_create_card_refuses_an_unknown_list(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="To Do")])
    result = await hw.create_card(connected_ctx, CreateCardParams(
        name="x", list_name="Nope"))
    assert succeeded(result) is False
    # The error names what DOES exist, so the next attempt can succeed.
    assert "To Do" in text_of_result(result)


async def test_create_card_posts_to_cards(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="To Do")])
    http.push(card_payload())
    await hw.create_card(connected_ctx, CreateCardParams(
        name="n", list_name="To Do"))
    assert http.last_method() == "POST"
    assert http.last_path().endswith("/cards")


# --- update_card ------------------------------------------------------------

async def test_update_uses_put_not_patch(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(card_payload(name="Fix hero properly"))
    result = await hw.update_card(connected_ctx, UpdateCardParams(
        card="Fix hero", name="Fix hero properly"))
    assert succeeded(result) is True
    assert http.last_method() == "PUT"


async def test_update_sends_only_named_fields(connected_ctx, http):
    """Renaming a card must not blank its description."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(card_payload())
    await hw.update_card(connected_ctx, UpdateCardParams(
        card="Fix hero", name="New title"))
    body = http.last_body()
    assert body == {"name": "New title"}


async def test_clear_due_sends_an_empty_value(connected_ctx, http):
    """Removing a due date needs an explicit empty string -- omitting the field
    leaves the old date in place."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(card_payload())
    await hw.update_card(connected_ctx, UpdateCardParams(
        card="Fix hero", clear_due=True))
    # An explicit JSON null, not "": Trello reads an empty string as an
    # invalid date and rejects the write, while omitting `due` entirely would
    # leave the existing date untouched. Null is the only value that CLEARS it.
    assert http.last_body()["due"] is None


async def test_update_with_nothing_to_change_is_refused(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    result = await hw.update_card(connected_ctx, UpdateCardParams(
        card="Fix hero"))
    assert succeeded(result) is False


# --- move_card --------------------------------------------------------------

async def test_move_within_board_sends_only_the_list(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([list_payload(list_id="7e" + "6" * 22, name="Done")])
    http.push(card_payload())
    result = await hw.move_card(connected_ctx, MoveCardParams(
        card="Fix hero", list_name="Done"))
    assert succeeded(result) is True
    body = http.last_body()
    assert body["idList"] == "7e" + "6" * 22
    assert "idBoard" not in body


async def test_cross_board_move_without_a_list_is_refused(connected_ctx, http):
    """Lists do not exist across boards, so there is no destination to infer."""
    http.push(member_payload())
    http.push([board_payload(name="Client Work"),
               board_payload(board_id="6c" + "9" * 22, name="Archive")])
    http.push([card_payload(name="Fix hero")])
    result = await hw.move_card(connected_ctx, MoveCardParams(
        board="Client Work", card="Fix hero", to_board="Archive"))
    assert succeeded(result) is False
    assert "list" in text_of_result(result).lower()


# --- archive / delete -------------------------------------------------------

async def test_archive_sets_closed(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(card_payload(closed=True))
    result = await hw.archive_card(connected_ctx, ArchiveCardParams(
        card="Fix hero"))
    assert succeeded(result) is True
    assert http.last_body() == {"closed": True}
    assert http.last_method() == "PUT"


async def test_unarchive_is_the_same_route(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero", closed=True)])
    http.push(card_payload(closed=False))
    await hw.archive_card(connected_ctx, ArchiveCardParams(
        card="Fix hero", archived=False))
    assert http.last_body() == {"closed": False}


async def test_delete_card_uses_delete(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push({})
    result = await hw.delete_card(connected_ctx, DeleteCardParams(
        card="Fix hero"))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"


async def test_delete_says_it_is_permanent(connected_ctx, http):
    """Trello card deletion is NOT recoverable like Asana's 30-day window, and
    the confirmation the user reads has to say so."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push({})
    result = await hw.delete_card(connected_ctx, DeleteCardParams(
        card="Fix hero"))
    text = text_of_result(result).lower()
    assert "permanent" in text or "cannot be undone" in text


# --- comments ---------------------------------------------------------------

async def test_comment_posts_to_actions_comments(connected_ctx, http):
    """A Trello comment is an ACTION; there is no comment resource to create."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push({"id": "9f" + "8" * 22, "data": {"text": "Ship it"}})
    result = await hw.add_comment(connected_ctx, AddCommentParams(
        card="Fix hero", comment="Ship it"))
    assert succeeded(result) is True
    # Asserted against Atlassian's documented route. This test previously
    # pinned `/actionsComments`, which does not exist -- so the suite was green
    # while every live comment 404'd. A test that agrees with the bug is worse
    # than no test: it defends the bug from being noticed.
    assert http.last_path().endswith("/actions/comments")
    # The text rides in the QUERY string, which is where Atlassian documents it
    # for this route. It was previously sent as a JSON body -- accepted by the
    # double, ignored by Trello.
    assert http.last_params().get("text") == "Ship it"


async def test_empty_comment_is_refused_before_the_call(connected_ctx, http):
    result = await hw.add_comment(connected_ctx, AddCommentParams(
        card="Fix hero", comment="   "))
    assert succeeded(result) is False
    assert http.calls == []


# --- members / labels -------------------------------------------------------

async def test_member_me_is_resolved_to_a_real_id(connected_ctx, http):
    """Trello rejects the literal word `me` where a member id is expected."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(member_payload())          # resolve 'me'
    http.push([{"id": "x"}])
    result = await hw.set_card_members(connected_ctx, CardMembersParams(
        card="Fix hero", members="me"))
    assert succeeded(result) is True
    assert http.last_body()["value"] == "5f" + "1" * 22
    assert http.last_body()["value"] != "me"


async def test_removing_a_member_uses_delete(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(member_payload())
    http.push({})
    await hw.set_card_members(connected_ctx, CardMembersParams(
        card="Fix hero", members="me", remove=True))
    assert http.last_method() == "DELETE"


async def test_no_members_named_is_refused(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    result = await hw.set_card_members(connected_ctx, CardMembersParams(
        card="Fix hero", members="  "))
    assert succeeded(result) is False


# --- lists ------------------------------------------------------------------

async def test_create_list_posts_the_board_id(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push(list_payload(name="Blocked"))
    result = await hw.create_list(connected_ctx, CreateListParams(
        name="Blocked"))
    assert succeeded(result) is True
    body = http.last_body()
    assert body["idBoard"] == "6a" + "2" * 22
    assert body["name"] == "Blocked"


async def test_empty_member_list_costs_nothing(connected_ctx, http):
    """An empty name list is knowably invalid: refuse before any lookup."""
    result = await hw.set_card_members(connected_ctx, CardMembersParams(
        card="Fix hero", members="  "))
    assert succeeded(result) is False
    assert http.calls == []


async def test_empty_label_list_costs_nothing(connected_ctx, http):
    result = await hw.set_card_labels(connected_ctx, CardLabelsParams(
        card="Fix hero", labels=""))
    assert succeeded(result) is False
    assert http.calls == []
