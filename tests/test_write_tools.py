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
from conftest import (TEST_KEY, TEST_TOKEN, attachment_payload, board_payload,
                      card_payload, checklist_payload, code_of, label_payload,
                      list_payload, member_payload, succeeded, text_of_result)
from models import (AddAttachmentParams, AddCheckItemParams, AddCommentParams,
                    ArchiveCardParams, BoardMemberParams, CardLabelsParams,
                    CardMembersParams, ConnectAccountParams, CopyCardParams,
                    CreateCardParams, CreateLabelParams, CreateListParams,
                    DeleteAttachmentParams, DeleteBoardParams, DeleteCardParams,
                    DeleteCheckItemParams, DeleteChecklistParams,
                    DeleteCommentParams, DeleteLabelParams, EditCommentParams,
                    ListBulkParams, MoveCardParams, UpdateBoardParams,
                    UpdateCardParams, UpdateChecklistParams, UpdateLabelParams,
                    UpdateListParams)


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


# --- attachments ------------------------------------------------------------
# Only links are attachable: the shared client sends JSON, and a file upload is
# multipart. A `file` parameter would accept a path it could never deliver.

async def test_attachment_url_rides_in_the_query(connected_ctx, http):
    """Trello takes the URL as a query parameter on this route."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(attachment_payload())
    result = await hw.add_attachment(connected_ctx, AddAttachmentParams(
        card="Fix hero", url="https://example.dev/brief.pdf", name="Brief"))
    assert succeeded(result) is True
    assert http.last_path().endswith("/attachments")
    assert http.last_params().get("url") == "https://example.dev/brief.pdf"
    assert http.last_params().get("name") == "Brief"


async def test_attachment_without_a_url_is_refused_before_the_call(
        connected_ctx, http):
    """No URL is knowably invalid from the parameters alone."""
    result = await hw.add_attachment(connected_ctx, AddAttachmentParams(
        card="Fix hero", url="   "))
    assert succeeded(result) is False
    assert http.calls == []


async def test_deleting_an_attachment_says_whether_it_was_an_upload(
        connected_ctx, http):
    """An upload is the only copy; a link is a reference. The wording differs.

    Reporting both as "removed attachment" hides that one of them destroyed
    data Trello was storing.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([attachment_payload(name="Brief.pdf", is_upload=True)])
    http.push({})
    result = await hw.delete_attachment(connected_ctx, DeleteAttachmentParams(
        card="Fix hero", attachment="Brief.pdf"))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    text = text_of_result(result).lower()
    assert "upload" in text or "stored" in text


# --- checklists -------------------------------------------------------------

async def test_add_check_item_finds_the_only_checklist(connected_ctx, http):
    """`checklist` may be omitted when the card has exactly one.

    Requiring it would be asking for information the card already determines.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([checklist_payload()])                  # resolve_checklist
    http.push({"id": "f4" + "c" * 22, "name": "Ship it"})
    result = await hw.add_check_item(connected_ctx, AddCheckItemParams(
        card="Fix hero", item="Ship it"))
    assert succeeded(result) is True
    assert http.last_path().endswith("/checkItems")


async def test_add_check_item_refuses_to_guess_between_checklists(
        connected_ctx, http):
    """Two checklists and no name: refuse, do not pick.

    Appending to the wrong checklist is a silent misfile -- the item exists, so
    nothing looks broken, and it is on the wrong list forever.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([checklist_payload(name="Launch steps"),
               checklist_payload(checklist_id="a1" + "2" * 22, name="QA")])
    result = await hw.add_check_item(connected_ctx, AddCheckItemParams(
        card="Fix hero", item="Ship it"))
    assert succeeded(result) is False
    text = text_of_result(result).lower()
    # The refusal has to NAME them, or the user cannot act on it.
    assert "launch steps" in text and "qa" in text


async def test_delete_check_item_addresses_it_under_the_card(
        connected_ctx, http):
    """Trello deletes a check item at cards/{id}/checkItem/{itemId}."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([checklist_payload()])                  # resolve_check_item
    http.push({"limits": {}})
    result = await hw.delete_check_item(connected_ctx, DeleteCheckItemParams(
        card="Fix hero", item="Add images"))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    assert "/checkItem/" in http.last_path()


async def test_delete_checklist_says_its_items_go_too(connected_ctx, http):
    """Deleting a checklist takes its items; the result must say so."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([checklist_payload()])
    http.push({"limits": {}})
    result = await hw.delete_checklist(connected_ctx, DeleteChecklistParams(
        card="Fix hero", checklist="Launch steps"))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    text = text_of_result(result).lower()
    assert "item" in text


# --- lists (columns) --------------------------------------------------------

async def test_update_list_refuses_an_empty_change(connected_ctx, http):
    """An empty PUT would report success while changing nothing."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="Today")])
    result = await hw.update_list(connected_ctx, UpdateListParams(
        list_name="Today"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_VALIDATION_FAILED"


async def test_move_all_cards_sends_both_board_and_list(connected_ctx, http):
    """Trello needs idBoard AND idList, even within one board."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="Today")])                        # source
    http.push([list_payload(list_id="7c" + "9" * 22, name="Done")])  # dest
    http.push([card_payload()])
    result = await hw.move_all_cards(connected_ctx, ListBulkParams(
        list_name="Today", to_list="Done"))
    assert succeeded(result) is True
    sent = http.last_params()
    assert sent.get("idList")
    assert sent.get("idBoard")


async def test_archive_all_cards_reports_it_is_reversible(connected_ctx, http):
    """Archiving is not deleting, and the difference must be visible."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="Today")])
    http.push({"limits": {}})
    result = await hw.archive_all_cards(connected_ctx, ListBulkParams(
        list_name="Today"))
    assert succeeded(result) is True
    assert http.last_path().endswith("/archiveAllCards")


# --- labels -----------------------------------------------------------------

async def test_create_label_belongs_to_the_board(connected_ctx, http):
    """A Trello label is board-level: created with idBoard, not on a card."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push(label_payload(name="Blocked", color="orange"))
    result = await hw.create_label(connected_ctx, CreateLabelParams(
        name="Blocked", color="orange"))
    assert succeeded(result) is True
    assert http.last_path().endswith("labels")
    assert http.last_body().get("idBoard") or http.last_params().get("idBoard")


async def test_delete_label_warns_it_leaves_every_card(connected_ctx, http):
    """Deleting a label strips it from every card that had it."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([label_payload(name="Urgent")])
    http.push({"limits": {}})
    result = await hw.delete_label(connected_ctx, DeleteLabelParams(
        label="Urgent"))
    assert succeeded(result) is True
    text = text_of_result(result).lower()
    assert "every card" in text or "all cards" in text or "card" in text


# --- boards -----------------------------------------------------------------

async def test_delete_board_requires_confirmation(connected_ctx, http):
    """Without confirm=true, nothing is sent at all.

    A board deletion destroys every list, card and comment on it and Trello
    offers no undo. The gate must fire BEFORE any request, so a mistyped board
    name cannot even begin.
    """
    result = await hw.delete_board(connected_ctx, DeleteBoardParams(
        board="Client Work", confirm=False))
    assert succeeded(result) is False
    # Not one request spent: no lookup, no delete.
    assert http.calls == []
    text = text_of_result(result).lower()
    assert "confirm" in text


async def test_update_board_refuses_an_empty_change(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    result = await hw.update_board(connected_ctx, UpdateBoardParams(
        board="Client Work"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_VALIDATION_FAILED"


async def test_closing_a_board_is_described_as_reversible(connected_ctx, http):
    """Closing keeps everything; the wording must not read like deletion."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push(board_payload(closed=True))
    result = await hw.update_board(connected_ctx, UpdateBoardParams(
        board="Client Work", closed=True))
    assert succeeded(result) is True
    assert http.last_method() == "PUT"


async def test_board_member_role_is_sent_as_type(connected_ctx, http):
    """Trello names the role parameter `type`, not `role`."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push({"id": "6a" + "2" * 22})
    result = await hw.set_board_member(connected_ctx, BoardMemberParams(
        board="Client Work", member="teammate@example.dev", role="normal"))
    assert succeeded(result) is True
    assert http.last_method() == "PUT"
    sent = {**http.last_params(), **http.last_body()}
    assert sent.get("type") == "normal"


# --- copy -------------------------------------------------------------------

async def test_copy_card_sends_the_source_id(connected_ctx, http):
    """A copy is POST /cards with idCardSource -- not a card built by hand."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([list_payload(name="Today")])
    http.push(card_payload(card_id="9a" + "1" * 22, name="Fix hero (copy)"))
    result = await hw.copy_card(connected_ctx, CopyCardParams(
        card="Fix hero", to_list="Today", name="Fix hero (copy)"))
    assert succeeded(result) is True
    sent = {**http.last_params(), **http.last_body()}
    assert sent.get("idCardSource")


async def test_add_check_item_refuses_an_ambiguous_checklist_NAME(
        connected_ctx, http):
    """A NAMED checklist that matches two is refused as well.

    Separate from the omitted-name case above: that one is refused by the
    "which of the card's checklists" branch, this one by the name matcher. The
    first test passed while the name matcher was silently taking the first of
    two matches, so the two branches need their own tests -- one green test does
    not cover a refusal it never reaches.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([checklist_payload(name="Launch steps"),
               checklist_payload(checklist_id="a1" + "2" * 22,
                                 name="Launch QA")])
    result = await hw.add_check_item(connected_ctx, AddCheckItemParams(
        card="Fix hero", item="Ship it", checklist="Launch"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_TARGET_AMBIGUOUS"
    text = text_of_result(result).lower()
    assert "launch steps" in text and "launch qa" in text


# --- comments: editing and deleting ------------------------------------------
# A comment is an ACTION. Its route is cards/{id}/actions/{idAction}/comments --
# the same shape that `add_comment` got wrong once by posting to a comment
# resource that does not exist, where the 404 read as "card not found".

async def test_edit_comment_addresses_it_as_an_action(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push({"id": "ac" + "1" * 22, "data": {"text": "Revised"}})
    result = await hw.edit_comment(connected_ctx, EditCommentParams(
        card="Fix hero", comment_id="ac" + "1" * 22, text="Revised"))
    assert succeeded(result) is True
    path = http.last_path()
    assert "/actions/" in path and path.endswith("/comments"), path
    # The new text is a query parameter here, exactly as when posting one.
    assert http.last_params().get("text") == "Revised"


async def test_edit_comment_refuses_empty_text(connected_ctx, http):
    """Empty text would blank the comment while reporting an edit."""
    result = await hw.edit_comment(connected_ctx, EditCommentParams(
        card="Fix hero", comment_id="ac" + "1" * 22, text="   "))
    assert succeeded(result) is False
    assert http.calls == []


async def test_delete_comment_uses_the_action_route(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push({})
    result = await hw.delete_comment(connected_ctx, DeleteCommentParams(
        card="Fix hero", comment_id="ac" + "1" * 22))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    assert "/actions/" in http.last_path()


# --- renames ----------------------------------------------------------------

async def test_update_checklist_renames_the_checklist_itself(
        connected_ctx, http):
    """PUT goes to the CHECKLIST, not to the card that holds it."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([checklist_payload()])
    http.push({"id": "af" + "7" * 22, "name": "Launch steps v2"})
    result = await hw.update_checklist(connected_ctx, UpdateChecklistParams(
        card="Fix hero", checklist="Launch steps", name="Launch steps v2"))
    assert succeeded(result) is True
    assert http.last_method() == "PUT"
    assert "/1/checklists/" in http.last_path(), http.last_path()


async def test_update_label_can_change_colour_alone(connected_ctx, http):
    """Recolouring without renaming must not blank the name."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([label_payload(name="Urgent", color="red")])
    http.push(label_payload(name="Urgent", color="blue"))
    result = await hw.update_label(connected_ctx, UpdateLabelParams(
        label="Urgent", color="blue"))
    assert succeeded(result) is True
    body = http.last_body() or {}
    assert body.get("color") == "blue"
    # No empty name in the body: Trello would accept it and erase the label's.
    assert "name" not in body or body["name"]
