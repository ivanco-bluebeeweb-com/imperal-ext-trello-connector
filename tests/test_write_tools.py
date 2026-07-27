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
                      card_payload, checklist_payload, code_of,
                      custom_field_payload, label_payload, list_payload,
                      member_payload, succeeded, text_of_result,
                      workspace_payload)
from models import (AddAttachmentParams, AddCheckItemParams, AddCommentParams,
                    ArchiveCardParams, BoardMemberParams, CardLabelsParams,
                    CardMembersParams, ConnectAccountParams, CopyCardParams,
                    CreateCardParams, CreateLabelParams, CreateListParams,
                    DeleteAttachmentParams, DeleteBoardParams, DeleteCardParams,
                    DeleteCheckItemParams, DeleteChecklistParams,
                    DeleteCommentParams, DeleteLabelParams, EditCommentParams,
                    CreateCustomFieldParams, CustomFieldOptionParams,
                    DeleteCustomFieldParams, DeleteWorkspaceParams,
                    CopyBoardParams, MoveListParams, SetCustomFieldParams,
                    WorkspaceMemberParams,
                    ListBulkParams, MoveCardParams, StickerParams,
                    UpdateBoardParams, UpdateCardParams, UpdateChecklistParams,
                    UpdateLabelParams, UpdateListParams, UpdateWorkspaceParams,
                    VoteParams)


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


# --- custom fields ----------------------------------------------------------
# The value shape is decided by the FIELD TYPE. These pin the two cases that
# fail silently rather than loudly: a dropdown value that matches no option, and
# a number sent as a JSON number instead of a string.

async def test_dropdown_value_is_sent_as_an_option_id(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload()])          # resolve_custom_field
    http.push({"id": "it" + "0" * 22})
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Priority", value="High"))
    assert succeeded(result) is True
    body = http.last_body()
    # The option ID, not the word "High".
    assert body == {"idValue": "bb" + "2" * 22}, body


async def test_unmatched_dropdown_value_is_refused_not_written(
        connected_ctx, http):
    """A value matching no option must NOT reach Trello.

    Sending an empty body here returns 200 and changes nothing, which would be
    reported as "set" -- the field would silently keep its old value.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload()])
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Priority", value="Urgent-ish"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_TARGET_NOT_FOUND"
    # Resolving the board, card and field costs GETs; what must NOT happen is a
    # WRITE. Asserting "no calls at all" was wrong -- it asserted the resolve
    # never ran, which is not the claim and made the test fail on correct code.
    assert [c for c in http.calls if c["method"] != "GET"] == []
    # The refusal lists what the choices actually are.
    text = text_of_result(result).lower()
    assert "low" in text and "high" in text


async def test_number_field_value_is_a_string(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload(name="Estimate", field_type="number",
                                    with_options=False)])
    http.push({"id": "it" + "1" * 22})
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Estimate", value="42"))
    assert succeeded(result) is True
    body = http.last_body()
    assert body == {"value": {"number": "42"}}
    assert isinstance(body["value"]["number"], str)


async def test_clearing_a_dropdown_unsets_the_option_id(connected_ctx, http):
    """A dropdown has no `value` to empty -- it has an option id to unset.

    REGRESSION TEST for a live 400 ("Invalid custom field item value"). Clearing
    was written as one shape for every type, {"value": {}}, which works for text,
    number, date and checkbox and is rejected for a dropdown: the spec defines
    the body as a oneOf where a list-type field carries `idValue` and never
    `value`. The type has to decide the clear body just as it decides the set
    body.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload()])          # type "list", with options
    http.push({})
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Priority", clear=True))
    assert succeeded(result) is True
    assert http.last_method() == "PUT"
    body = http.last_body()
    assert "idValue" in body
    # `value` must be absent entirely: the two shapes are mutually exclusive.
    assert "value" not in body


async def test_clearing_a_field_is_an_empty_put_not_a_delete(
        connected_ctx, http):
    """Trello has no delete route for a card's field value."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload(name="Estimate", field_type="number",
                                    with_options=False)])
    http.push({})
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Estimate", clear=True))
    assert succeeded(result) is True
    assert http.last_method() == "PUT"
    assert http.last_body() == {"value": {}}


async def test_deleting_a_custom_field_requires_confirmation(
        connected_ctx, http):
    """It destroys the value on every card, and Trello has no undo."""
    result = await hw.delete_custom_field(connected_ctx,
                                          DeleteCustomFieldParams(
                                              field="Priority", confirm=False))
    assert succeeded(result) is False
    assert http.calls == []
    assert "every card" in text_of_result(result).lower()


async def test_dropdown_without_options_is_refused(connected_ctx, http):
    """Trello accepts it; the result is a field no value can be set on."""
    http.push(member_payload())
    http.push([board_payload()])
    result = await hw.create_custom_field(connected_ctx,
                                         CreateCustomFieldParams(
                                             name="Priority",
                                             field_type="dropdown"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_VALIDATION_FAILED"


async def test_options_on_a_non_dropdown_field_are_refused(
        connected_ctx, http):
    """Only a dropdown has options; the others would silently ignore them."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([custom_field_payload(name="Estimate", field_type="number",
                                    with_options=False)])
    result = await hw.set_custom_field_option(connected_ctx,
                                              CustomFieldOptionParams(
                                                  field="Estimate",
                                                  option="Low"))
    assert succeeded(result) is False
    assert "not a dropdown" in text_of_result(result).lower()


# --- workspaces -------------------------------------------------------------

async def test_deleting_a_workspace_requires_confirmation(connected_ctx, http):
    result = await hw.delete_workspace(connected_ctx, DeleteWorkspaceParams(
        workspace="Acme Studio", confirm=False))
    assert succeeded(result) is False
    assert http.calls == []


async def test_removing_a_workspace_member_says_boards_are_unchanged(
        connected_ctx, http):
    """Trello keeps workspace and board membership separate.

    Reporting a bare "removed" would imply an access revocation that has not
    happened -- the person still reaches every board they were on.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([workspace_payload()])                 # resolve_workspace
    http.push([{"id": "m9" + "3" * 22, "fullName": "Sam Ray",
                "username": "samray"}])
    http.push({})
    result = await hw.set_workspace_member(connected_ctx,
                                          WorkspaceMemberParams(
                                              workspace="Acme Studio",
                                              member="Sam Ray", remove=True))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    text = text_of_result(result).lower()
    assert "board" in text and "unchanged" in text


# --- board copy / list move -------------------------------------------------

async def test_copying_a_board_without_cards_keeps_none(connected_ctx, http):
    """keepFromSource is Trello's own vocabulary: "cards" or "none"."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push({"id": "nb" + "4" * 22, "name": "Template",
               "url": "https://trello.com/b/xyz"})
    result = await hw.copy_board(connected_ctx, CopyBoardParams(
        board="Client Work", name="Template", keep_cards=False))
    assert succeeded(result) is True
    assert http.last_body().get("keepFromSource") == "none"
    assert http.last_body().get("idBoardSource")


async def test_moving_a_list_to_its_own_board_is_refused(connected_ctx, http):
    """A no-op move would report success while nothing moved."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([list_payload(name="Today")])
    http.push(member_payload())
    http.push([board_payload()])
    result = await hw.move_list_to_board(connected_ctx, MoveListParams(
        list_name="Today", to_board="Client Work"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_VALIDATION_FAILED"


# --- stickers and votes ------------------------------------------------------

async def test_removing_a_sticker_finds_it_by_image_name(connected_ctx, http):
    """Removal needs the sticker's id, which the user does not have."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([{"id": "st" + "1" * 22, "image": "taco-cool"}])
    http.push({})
    result = await hw.set_sticker(connected_ctx, StickerParams(
        card="Fix hero", sticker="taco-cool", remove=True))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    assert "st" + "1" * 22 in http.last_path()


async def test_removing_an_absent_sticker_says_what_is_there(
        connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([{"id": "st" + "1" * 22, "image": "thumbsup"}])
    result = await hw.set_sticker(connected_ctx, StickerParams(
        card="Fix hero", sticker="taco-cool", remove=True))
    assert succeeded(result) is False
    assert "thumbsup" in text_of_result(result)
    assert [c for c in http.calls if c["method"] != "GET"] == []


async def test_vote_uses_the_membersVoted_route(connected_ctx, http):
    """A vote is a member added to the card's voters, not a counter."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(member_payload())
    http.push({})
    result = await hw.set_vote(connected_ctx, VoteParams(card="Fix hero"))
    assert succeeded(result) is True
    assert "membersVoted" in http.last_path()


# --- workspaces --------------------------------------------------------------

async def test_removing_a_workspace_member_is_not_a_deactivation(
        connected_ctx, http):
    """Trello has two different removals; this tool must use the plain one."""
    # Call order, TRACED not assumed: GET members/me, GET members/me/boards
    # (both from the credential resolve), GET members/me/organizations to
    # resolve the workspace, GET its members to turn a name into an id, DELETE.
    # Queuing one payload for the credential step left everything after it
    # reading the wrong response.
    http.push(member_payload())
    http.push([board_payload()])
    http.push([workspace_payload()])
    http.push([{"id": "9c" + "7" * 22, "fullName": "Teammate",
                "username": "teammate"}])
    http.push({})
    result = await hw.set_workspace_member(connected_ctx,
                                          WorkspaceMemberParams(
                                              workspace="Acme Studio",
                                              member="teammate",
                                              remove=True))
    assert succeeded(result) is True
    assert http.last_method() == "DELETE"
    # `/all` would remove them from every board too -- a far bigger action.
    assert not http.last_path().endswith("/all")


async def test_deleting_a_workspace_requires_confirmation(connected_ctx, http):
    result = await hw.delete_workspace(connected_ctx, DeleteWorkspaceParams(
        workspace="Acme Studio", confirm=False))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_VALIDATION_FAILED"
    assert http.calls == []


async def test_updating_a_workspace_refuses_an_empty_change(
        connected_ctx, http):
    result = await hw.update_workspace(connected_ctx, UpdateWorkspaceParams(
        workspace="Acme Studio"))
    assert succeeded(result) is False
    # The guard is checked BEFORE credentials, so a no-op reports the malformed
    # request rather than a connection problem the user does not have.
    assert code_of(result) == "TRELLO_VALIDATION_FAILED"
    assert http.calls == []


# --- board copy and cross-board list move -----------------------------------

async def test_copy_board_sends_the_source_as_idBoardSource(
        connected_ctx, http):
    """A copy is a CREATE with a source, not a copy endpoint."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push({"id": "n7" + "9" * 22, "name": "Client Work (copy)",
               "url": "https://trello.com/b/xyz"})
    result = await hw.copy_board(connected_ctx, CopyBoardParams(
        board="Client Work", name="Client Work (copy)"))
    assert succeeded(result) is True
    assert http.last_method() == "POST"
    # The client sends `data` as the JSON BODY; only key/token ride the query
    # string. Asserting on last_params() checked the wrong half of the request.
    assert http.last_body().get("idBoardSource")


async def test_moving_a_list_to_another_board_names_the_target(
        connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload(), board_payload(board_id="d8" + "4" * 22,
                                              name="Archive board")])
    http.push([list_payload(name="Today")])
    http.push([board_payload(), board_payload(board_id="d8" + "4" * 22,
                                              name="Archive board")])
    http.push({"id": "6a" + "3" * 22, "name": "Today"})
    # The SOURCE board is named too: with two boards reachable, leaving it out
    # is genuinely ambiguous and the connector is right to refuse.
    result = await hw.move_list_to_board(connected_ctx, MoveListParams(
        board="Client Work", list_name="Today", to_board="Archive board"))
    assert succeeded(result) is True
    assert "idBoard" in (http.last_path() + str(http.last_params()))


async def test_vote_on_a_board_with_voting_off_blames_the_board(
        connected_ctx, http):
    """Trello uses 401 for "voting disabled" as well as "bad token".

    Found live: the vote came back as "your token lacks write scope" while a
    sticker had written successfully with the SAME token seconds earlier. Passing
    the 401 through sends the user to regenerate credentials that are fine. The
    board's own preference is the real answer.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(member_payload())
    http.push("unauthorized card permission requested", status=401)
    http.push(board_payload(prefs={"voting": "disabled"}))
    result = await hw.set_vote(connected_ctx, VoteParams(card="Fix hero"))
    assert succeeded(result) is False
    text = text_of_result(result).lower()
    assert "voting" in text
    # The credentials are explicitly exonerated, and no re-auth is suggested.
    assert "credentials are fine" in text
    assert "scope" not in text


async def test_vote_401_without_a_disabled_pref_stays_an_auth_failure(
        connected_ctx, http):
    """The override must not swallow a genuine permission problem.

    If voting IS enabled and Trello still says 401, the token really is the
    problem and the original advice is the right advice.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push(member_payload())
    http.push("unauthorized card permission requested", status=401)
    http.push(board_payload(prefs={"voting": "enabled"}))
    result = await hw.set_vote(connected_ctx, VoteParams(card="Fix hero"))
    assert succeeded(result) is False
    assert code_of(result) == "TRELLO_SCOPE_INSUFFICIENT"


async def test_checkbox_result_reports_what_trello_stored(connected_ctx, http):
    """The summary must not echo the raw input when it was normalised.

    Setting a checkbox to "yes" stores `true`. Echoing "yes" back read as if
    Trello held the string "yes" -- close enough to look right, wrong enough to
    mislead anyone comparing the reply against the card.
    """
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload(name="Reviewed", field_type="checkbox",
                                    with_options=False)])
    http.push({})
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Reviewed", value="yes"))
    assert succeeded(result) is True
    text = text_of_result(result).lower()
    assert "true" in text
    assert "'yes'" not in text


async def test_dropdown_result_shows_the_option_text_not_its_id(
        connected_ctx, http):
    """Honesty cuts both ways: an option ID is not a useful thing to show."""
    http.push(member_payload())
    http.push([board_payload()])
    http.push([card_payload(name="Fix hero")])
    http.push([custom_field_payload()])
    http.push({})
    result = await hw.set_custom_field(connected_ctx, SetCustomFieldParams(
        card="Fix hero", field="Priority", value="High"))
    assert succeeded(result) is True
    text = text_of_result(result)
    assert "High" in text
    # The raw option id must not leak into the summary.
    assert "bb" + "2" * 22 not in text
