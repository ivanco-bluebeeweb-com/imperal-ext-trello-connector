"""Write tools: create/update/move/archive/delete cards, comments, members,
labels, lists, boards, checklists -- plus `connect_account`, which stores the
credential pair.

Four Trello shapes drive this file, all verified against the docs:

* WRITES USE PUT, NOT PATCH. `PUT /cards/{id}` updates a card; there is no
  PATCH route, and sending one is a 404 that reads like a missing card.
* NO REQUEST ENVELOPE. Unlike Asana's `{"data": {...}}`, a Trello write body is
  the fields themselves. `trello_client.request` sends `data` as the JSON body
  and always appends `key`/`token` to the query string.
* A COMMENT IS AN ACTION. Posting one is `POST /cards/{id}/actions/comments`
  with `text` -- there is no comment resource to create.
* MOVING ACROSS BOARDS NEEDS BOTH IDS. `idList` alone is not enough when the
  destination is on another board: Trello requires `idBoard` too, and a list id
  from a different board is rejected. Lists do not exist across boards, so a
  cross-board move must name the destination list as well.

Every handler resolves NAMES to ids first, so the user never types a
24-character hex string.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acct
import shared
import trello_client as tc
import trello_objects as to
from app import chat
from models import (
    AddAttachmentParams,
    AddCheckItemParams,
    AddCommentParams,
    ArchiveCardParams,
    ArchiveListParams,
    BoardMemberParams,
    CardLabelsParams,
    CardMembersParams,
    CheckItemParams,
    ConnectAccountParams,
    ConnectResult,
    CopyBoardParams,
    CopyCardParams,
    CreateBoardParams,
    CreateCardParams,
    CreateChecklistParams,
    CreateCustomFieldParams,
    CreateLabelParams,
    CreateListParams,
    CreateWorkspaceParams,
    CustomFieldOptionParams,
    DeleteAttachmentParams,
    DeleteBoardParams,
    DeleteCardParams,
    DeleteCheckItemParams,
    DeleteChecklistParams,
    DeleteCommentParams,
    DeleteCustomFieldParams,
    DeleteLabelParams,
    DeleteWorkspaceParams,
    EditCommentParams,
    ListBulkParams,
    MoveCardParams,
    MoveListParams,
    SetCustomFieldParams,
    StickerParams,
    UpdateBoardParams,
    UpdateCardParams,
    UpdateChecklistParams,
    UpdateLabelParams,
    UpdateListParams,
    UpdateWorkspaceParams,
    VoteParams,
    WorkspaceMemberParams,
    WriteResult,
)

# Shared with handlers_read via `shared` so neither tool layer depends on the
# other. Re-exported under short private names to keep call sites readable.
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_resolve_card = shared.resolve_card
_resolve_list = shared.resolve_list
_resolve_member = shared.resolve_member
_resolve_label = shared.resolve_label


def _split_names(raw: str) -> list[str]:
    """Split a comma-separated parameter into clean names.

    Tolerant of stray spaces and trailing commas: the user is typing prose, and
    a trailing comma should not create an empty name that then fails to resolve.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _position_value(position: str, default: str = "bottom") -> str:
    """Trello accepts 'top', 'bottom' or a number for `pos`.

    Anything else is passed through untouched only when numeric; an unknown word
    falls back to the default rather than being forwarded to be rejected.
    """
    value = (position or "").strip().lower()
    if value in ("top", "bottom"):
        return value
    if value.replace(".", "", 1).isdigit():
        return value
    return default


@chat.function(
    "connect_account",
    "Connect a Trello account by saving its API key and token, after checking "
    "the pair actually works.",
    action_type="write", chain_callable=True,
    effects=["trello.account.connected"],
    data_model=ConnectResult,
    event="trello-connector.connect_account",
)
async def connect_account(ctx, params: ConnectAccountParams) -> ActionResult:
    """Validate a key/token pair against Trello, then store it.

    Both halves are required and neither is echoed back. `add_pair` verifies
    before writing, so a bad paste never lands in the secret.
    """
    out = await acct.add_pair(ctx, params.key, params.token)
    if not out.get("ok"):
        return _from_envelope(out)

    names = out.get("board_names") or []
    boards = ", ".join(names[:12])
    if len(names) > 12:
        boards += f" (+{len(names) - 12} more)"

    account_name = out.get("member_name") or "this Trello account"
    if out.get("already_connected"):
        return ActionResult.success(
            ConnectResult(
                account_name=account_name,
                username=out.get("username", ""),
                email=out.get("email", ""),
                already_connected=True,
                board_count=len(names),
                boards=boards,
                next_step="Nothing to do -- these credentials were already saved.",
            ),
            f"{account_name} is already connected ({len(names)} board(s) "
            "reachable).")

    next_step = (
        "Ask for your boards, or create a card -- boards are named, not id'd."
        if names else
        "No boards are visible yet. If you expect some, the token may belong to "
        "a different Trello account than the one you are looking at.")

    return ActionResult.success(
        ConnectResult(
            account_name=account_name,
            username=out.get("username", ""),
            email=out.get("email", ""),
            already_connected=False,
            board_count=len(names),
            boards=boards,
            next_step=next_step,
        ),
        f"Connected {account_name} -- {len(names)} board(s) reachable.")


@chat.function(
    "create_card",
    "Create a card in a Trello list, optionally with a description, due date, "
    "members and labels.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_card",
    effects=["trello.card.created"],
)
async def create_card(ctx, params: CreateCardParams) -> ActionResult:
    """Create a card.

    A card cannot exist outside a list, so `list_name` is required and resolved
    before anything is sent -- creating the card first and then discovering the
    list does not exist would leave a card in the wrong place.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    target = await _resolve_list(ctx, creds, board["id"], params.list_name)
    if not target.get("ok"):
        return _from_envelope(target)

    body: dict = {
        "name": params.name,
        "idList": target["id"],
        "pos": _position_value(params.position),
    }
    if params.desc:
        body["desc"] = params.desc
    if params.due:
        body["due"] = params.due

    # Members and labels are resolved BEFORE the card is created so a typo in
    # either fails cleanly instead of leaving a half-configured card behind.
    member_ids: list[str] = []
    for name in _split_names(params.members):
        found = await _resolve_member(ctx, creds, board["id"], name)
        if not found.get("ok"):
            return _from_envelope(found)
        member_ids.append(found["id"])
    if member_ids:
        body["idMembers"] = ",".join(member_ids)

    label_ids: list[str] = []
    for name in _split_names(params.labels):
        found = await _resolve_label(ctx, creds, board["id"], name)
        if not found.get("ok"):
            return _from_envelope(found)
        label_ids.append(found["id"])
    if label_ids:
        body["idLabels"] = ",".join(label_ids)

    out = await tc.request(ctx, "POST", "cards", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    card = out.get("data") or {}
    where = target.get("name") or params.list_name
    return ActionResult.success(
        WriteResult(
            id=to.id_of(card),
            name=to.name_of(card) or params.name,
            action="created",
            detail=f"in list '{where}' on board '{board.get('name', '')}'",
            url=to.text_of(card, "shortUrl") or to.text_of(card, "url"),
        ),
        f"Created card '{to.name_of(card) or params.name}' in '{where}'.")


@chat.function(
    "update_card",
    "Update a card's title, description or due date -- or archive it.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.update_card",
    effects=["trello.card.updated"],
)
async def update_card(ctx, params: UpdateCardParams) -> ActionResult:
    """Update card fields via PUT (Trello has no PATCH route for cards).

    `clear_due` sends an explicit null: omitting `due` leaves it untouched, so
    "remove the due date" needs its own instruction rather than an empty string,
    which Trello reads as an invalid date.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    body: dict = {}
    if params.name:
        body["name"] = params.name
    if params.desc:
        body["desc"] = params.desc
    if params.clear_due:
        body["due"] = None
    elif params.due:
        body["due"] = params.due
    if params.due_complete is not None:
        body["dueComplete"] = params.due_complete
    if params.closed is not None:
        body["closed"] = params.closed

    if not body:
        return _error(
            "Nothing to update. Name a new title, description or due date -- "
            "or ask to archive the card.",
            tc.TRELLO_VALIDATION_FAILED)

    out = await tc.request(ctx, "PUT", f"cards/{card['id']}", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    updated = out.get("data") or {}
    changed = ", ".join(sorted(body.keys()))
    return ActionResult.success(
        WriteResult(
            id=to.id_of(updated) or card["id"],
            name=to.name_of(updated) or card.get("name", ""),
            action="updated",
            detail=f"changed: {changed}",
            url=to.text_of(updated, "shortUrl") or to.text_of(updated, "url"),
        ),
        f"Updated '{to.name_of(updated) or card.get('name', 'card')}'.")


@chat.function(
    "move_card",
    "Move a card to another list, or to a different board.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.move_card",
    effects=["trello.card.moved"],
)
async def move_card(ctx, params: MoveCardParams) -> ActionResult:
    """Move a card between lists, or across boards.

    A cross-board move needs BOTH `idBoard` and `idList`: a list id from the
    source board is rejected on the destination, and lists do not exist across
    boards -- so the destination list has to be named too. That is why naming
    `to_board` without `list_name` is refused here instead of being sent and
    failing with Trello's less specific complaint.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    body: dict = {}
    destination = board
    where = ""

    if params.to_board:
        picked = await acct.resolve_board(ctx, params.to_board)
        if not picked.get("ok"):
            return _from_envelope(picked)
        destination = picked.get("board", {})
        if not params.list_name:
            return _error(
                f"Moving to board '{destination.get('name', '')}' also needs the "
                "destination list -- lists belong to one board, so there is no "
                "equivalent of the card's current list over there.",
                tc.TRELLO_VALIDATION_FAILED)
        body["idBoard"] = destination["id"]

    if params.list_name:
        target = await _resolve_list(ctx, creds, destination["id"],
                                     params.list_name)
        if not target.get("ok"):
            return _from_envelope(target)
        body["idList"] = target["id"]
        where = target.get("name") or params.list_name

    if params.position:
        body["pos"] = _position_value(params.position, "top")

    if not body:
        return _error(
            "Nothing to move. Name a destination list, or a board to move to.",
            tc.TRELLO_VALIDATION_FAILED)

    out = await tc.request(ctx, "PUT", f"cards/{card['id']}", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    moved = out.get("data") or {}
    detail = f"to list '{where}'" if where else "repositioned"
    if params.to_board:
        detail += f" on board '{destination.get('name', '')}'"
    return ActionResult.success(
        WriteResult(
            id=to.id_of(moved) or card["id"],
            name=to.name_of(moved) or card.get("name", ""),
            action="moved",
            detail=detail,
            url=to.text_of(moved, "shortUrl") or to.text_of(moved, "url"),
        ),
        f"Moved '{to.name_of(moved) or card.get('name', 'card')}' {detail}.")


@chat.function(
    "archive_card",
    "Archive a card, or restore one from the archive.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.archive_card",
    effects=["trello.card.archived"],
)
async def archive_card(ctx, params: ArchiveCardParams) -> ActionResult:
    """Archive or restore a card.

    Archiving is Trello's reversible "done with this" -- deliberately a plain
    write, not destructive, because the card stays recoverable. Deleting is the
    separate, gated tool.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    out = await tc.request(ctx, "PUT", f"cards/{card['id']}", creds,
                           data={"closed": params.archived})
    if not out.get("ok"):
        return _from_envelope(out)

    changed = out.get("data") or {}
    action = "archived" if params.archived else "restored"
    return ActionResult.success(
        WriteResult(
            id=to.id_of(changed) or card["id"],
            name=to.name_of(changed) or card.get("name", ""),
            action=action,
            detail="still recoverable from the board's archive"
            if params.archived else "back on the board",
            url=to.text_of(changed, "shortUrl") or to.text_of(changed, "url"),
        ),
        f"{action.capitalize()} '{to.name_of(changed) or card.get('name', 'card')}'.")


@chat.function(
    "delete_card",
    "Permanently delete a card. Unlike archiving, this cannot be undone.",
    action_type="destructive", chain_callable=True,
    effects=["trello.card.deleted"],
    data_model=WriteResult,
    event="trello-connector.delete_card",
)
async def delete_card(ctx, params: DeleteCardParams) -> ActionResult:
    """Delete a card for good.

    Declared `action_type="destructive"`, which is the ONLY correct way to ask
    the platform for a confirmation gate -- a handler that prompts by itself
    would be asking on a surface that may not be able to answer.

    Trello's delete is genuinely permanent: unlike Asana's 30-day recovery
    window, a deleted card is gone. The description says so, because the user
    approving the gate deserves to know the difference from archiving.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    name = card.get("name", "")
    out = await tc.request(ctx, "DELETE", f"cards/{card['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=card["id"],
            name=name,
            action="deleted",
            detail="permanently -- Trello keeps no copy of a deleted card",
        ),
        f"Deleted '{name or 'card'}' permanently.")


@chat.function(
    "add_comment",
    "Add a comment to a card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.add_comment",
    effects=["trello.comment.created"],
)
async def add_comment(ctx, params: AddCommentParams) -> ActionResult:
    """Post a comment on a card.

    Trello has no comment resource: this creates an ACTION of type
    `commentCard` via `/cards/{id}/actions/comments`, which is why the response
    carries an action id rather than a comment id.

    The text is checked BEFORE any lookup. An empty comment is knowably invalid
    from the parameters alone, and resolving the board and card first would
    spend three requests to arrive at a refusal that needed none -- and Trello's
    own rejection of an empty body reads like a problem with the card.
    """
    if not (params.comment or "").strip():
        return _error(
            "A comment needs some text. Nothing was sent to Trello.",
            tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    # The path is `actions/comments`, with the slash. `actionsComments` -- the
    # spelling used here originally -- is not a Trello route at all: it 404s,
    # and the 404 reads as "no such card", blaming a card that was just
    # resolved successfully. The text rides as a QUERY parameter, which is the
    # placement Atlassian documents for this route.
    out = await tc.request(ctx, "POST", f"cards/{card['id']}/actions/comments",
                           creds, params={"text": params.comment})
    if not out.get("ok"):
        return _from_envelope(out)

    action = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(action),
            name=card.get("name", ""),
            action="commented",
            detail=f"on '{card.get('name', 'card')}'",
        ),
        f"Comment added to '{card.get('name', 'card')}'.")


@chat.function(
    "set_card_members",
    "Add or remove people on a card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_card_members",
    effects=["trello.card.members_changed"],
)
async def set_card_members(ctx, params: CardMembersParams) -> ActionResult:
    """Add or remove card members.

    Each member is a separate call: Trello adds one member at a time via
    `POST /cards/{id}/idMembers` and removes via
    `DELETE /cards/{id}/idMembers/{memberId}`. Failures are collected rather
    than aborting on the first one, so naming three people and mistyping one
    still applies the other two -- and says which failed.

    The name list is checked BEFORE any lookup: an empty one is knowably invalid
    from the parameters, and resolving the board and card first would spend two
    requests to reach a refusal that needed none.
    """
    names = _split_names(params.members)
    if not names:
        return _error("Name at least one person to add or remove.",
                      tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    applied: list[str] = []
    failed: list[str] = []
    for name in names:
        found = await _resolve_member(ctx, creds, board["id"], name)
        if not found.get("ok"):
            failed.append(f"{name} ({found.get('error', 'not found')})")
            continue
        if params.remove:
            out = await tc.request(
                ctx, "DELETE", f"cards/{card['id']}/idMembers/{found['id']}",
                creds)
        else:
            out = await tc.request(
                ctx, "POST", f"cards/{card['id']}/idMembers", creds,
                data={"value": found["id"]})
        if out.get("ok"):
            applied.append(found.get("name") or name)
        else:
            failed.append(f"{name} ({out.get('error', 'failed')})")

    verb = "removed from" if params.remove else "added to"
    if not applied:
        return _error(
            f"No one was {verb} '{card.get('name', 'card')}'. {'; '.join(failed)}",
            tc.TRELLO_VALIDATION_FAILED)

    detail = f"{', '.join(applied)} {verb} the card"
    if failed:
        detail += f"; not applied: {'; '.join(failed)}"
    return ActionResult.success(
        WriteResult(
            id=card["id"],
            name=card.get("name", ""),
            action="members_changed",
            detail=detail,
        ),
        detail)


@chat.function(
    "set_card_labels",
    "Add or remove labels on a card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_card_labels",
    effects=["trello.card.labels_changed"],
)
async def set_card_labels(ctx, params: CardLabelsParams) -> ActionResult:
    """Add or remove card labels.

    Labels are board-scoped and may be UNNAMED -- a colour with no text is
    normal in Trello -- so `resolve_label` matches on name OR colour. Same
    one-at-a-time shape as members, and same collected-failure behaviour.

    Same pre-flight as members: an empty label list is refused before any
    request, not after two.
    """
    names = _split_names(params.labels)
    if not names:
        return _error("Name at least one label to add or remove.",
                      tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    applied: list[str] = []
    failed: list[str] = []
    for name in names:
        found = await _resolve_label(ctx, creds, board["id"], name)
        if not found.get("ok"):
            failed.append(f"{name} ({found.get('error', 'not found')})")
            continue
        if params.remove:
            out = await tc.request(
                ctx, "DELETE", f"cards/{card['id']}/idLabels/{found['id']}",
                creds)
        else:
            out = await tc.request(
                ctx, "POST", f"cards/{card['id']}/idLabels", creds,
                data={"value": found["id"]})
        if out.get("ok"):
            applied.append(found.get("name") or name)
        else:
            failed.append(f"{name} ({out.get('error', 'failed')})")

    verb = "removed from" if params.remove else "added to"
    if not applied:
        return _error(
            f"No labels were {verb} '{card.get('name', 'card')}'. "
            f"{'; '.join(failed)}",
            tc.TRELLO_VALIDATION_FAILED)

    detail = f"{', '.join(applied)} {verb} the card"
    if failed:
        detail += f"; not applied: {'; '.join(failed)}"
    return ActionResult.success(
        WriteResult(
            id=card["id"],
            name=card.get("name", ""),
            action="labels_changed",
            detail=detail,
        ),
        detail)


@chat.function(
    "create_list",
    "Create a new list (column) on a board.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_list",
    effects=["trello.list.created"],
)
async def create_list(ctx, params: CreateListParams) -> ActionResult:
    """Create a list on a board."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    out = await tc.request(ctx, "POST", "lists", creds, data={
        "name": params.name,
        "idBoard": board["id"],
        "pos": _position_value(params.position),
    })
    if not out.get("ok"):
        return _from_envelope(out)

    created = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(created),
            name=to.name_of(created) or params.name,
            action="created",
            detail=f"list on board '{board.get('name', '')}'",
        ),
        f"Created list '{to.name_of(created) or params.name}' on "
        f"'{board.get('name', '')}'.")


@chat.function(
    "archive_list",
    "Archive a list (column), or restore one.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.archive_list",
    effects=["trello.list.archived"],
)
async def archive_list(ctx, params: ArchiveListParams) -> ActionResult:
    """Archive or restore a list.

    Trello has no delete for lists at all -- archiving is the only way to remove
    one from a board, which is why there is no destructive counterpart here.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    target = await _resolve_list(ctx, creds, board["id"], params.list_name)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(ctx, "PUT", f"lists/{target['id']}/closed", creds,
                           data={"value": params.archived})
    if not out.get("ok"):
        return _from_envelope(out)

    changed = out.get("data") or {}
    action = "archived" if params.archived else "restored"
    return ActionResult.success(
        WriteResult(
            id=to.id_of(changed) or target["id"],
            name=to.name_of(changed) or target.get("name", ""),
            action=action,
            detail="cards on it are archived with it"
            if params.archived else "back on the board",
        ),
        f"{action.capitalize()} list "
        f"'{to.name_of(changed) or target.get('name', '')}'.")


@chat.function(
    "create_board",
    "Create a new Trello board.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_board",
    effects=["trello.board.created"],
)
async def create_board(ctx, params: CreateBoardParams) -> ActionResult:
    """Create a board.

    `defaultLists` is passed explicitly because Trello's own default is TRUE:
    a board created without saying otherwise arrives with To Do / Doing / Done
    already in it, which is a surprise if the caller meant an empty board.
    """
    creds, cred_err = await shared.any_credentials(ctx)
    if cred_err:
        return cred_err

    body: dict = {
        "name": params.name,
        "defaultLists": params.default_lists,
    }
    if params.desc:
        body["desc"] = params.desc

    out = await tc.request(ctx, "POST", "boards", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    created = out.get("data") or {}
    # The new board is not in the cached board list yet; drop the cache so the
    # very next "which boards do I have" includes it.
    await acct.forget_cache(ctx)

    lists_note = ("with Trello's default To Do / Doing / Done lists"
                  if params.default_lists else "with no lists yet")
    return ActionResult.success(
        WriteResult(
            id=to.id_of(created),
            name=to.name_of(created) or params.name,
            action="created",
            detail=f"board {lists_note}",
            url=to.board_url(created),
        ),
        f"Created board '{to.name_of(created) or params.name}' {lists_note}.")


@chat.function(
    "create_checklist",
    "Add a checklist to a card, optionally with its items.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_checklist",
    effects=["trello.checklist.created"],
)
async def create_checklist(ctx, params: CreateChecklistParams) -> ActionResult:
    """Create a checklist on a card and add its items.

    Two steps by necessity: `POST /checklists` creates the container, and each
    item needs its own `POST /checklists/{id}/checkItems`. Trello has no
    create-with-items form. Items that fail are reported rather than silently
    dropped, since a checklist missing half its items looks like it worked.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    out = await tc.request(ctx, "POST", "checklists", creds, data={
        "idCard": card["id"],
        "name": params.name,
    })
    if not out.get("ok"):
        return _from_envelope(out)

    checklist = out.get("data") or {}
    checklist_id = to.id_of(checklist)

    added: list[str] = []
    failed: list[str] = []
    for item in _split_names(params.items):
        item_out = await tc.request(
            ctx, "POST", f"checklists/{checklist_id}/checkItems", creds,
            data={"name": item})
        if item_out.get("ok"):
            added.append(item)
        else:
            failed.append(f"{item} ({item_out.get('error', 'failed')})")

    detail = f"on '{card.get('name', 'card')}'"
    if added:
        detail += f" with {len(added)} item(s)"
    if failed:
        detail += f"; items not added: {'; '.join(failed)}"

    return ActionResult.success(
        WriteResult(
            id=checklist_id,
            name=to.name_of(checklist) or params.name,
            action="created",
            detail=detail,
        ),
        f"Created checklist '{to.name_of(checklist) or params.name}' {detail}.")


@chat.function(
    "set_check_item",
    "Tick or untick an item in a card's checklist.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_check_item",
    effects=["trello.checkitem.updated"],
)
async def set_check_item(ctx, params: CheckItemParams) -> ActionResult:
    """Tick or untick a checklist item, found by its text.

    The item is located by reading the card's checklists first: Trello addresses
    a check item by id under its CARD (`PUT /cards/{id}/checkItem/{itemId}`),
    and the user knows the item by its wording, not that id. An ambiguous match
    is refused rather than guessed -- ticking the wrong item is a silent lie
    about what is done.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    listing = await tc.request(
        ctx, "GET", f"cards/{card['id']}/checklists", creds,
        params={"checkItems": "all",
                "checkItem_fields": "name,state", "fields": "name"})
    if not listing.get("ok"):
        return _from_envelope(listing)

    wanted = (params.item or "").strip().lower()
    matches: list[tuple[str, str, str]] = []
    for checklist in listing.get("data") or []:
        if not isinstance(checklist, dict):
            continue
        for item in checklist.get("checkItems") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name.strip().lower() == wanted:
                matches.append((to.id_of(item), name, to.name_of(checklist)))
    if not matches:
        # Fall back to a contains match only when nothing matched exactly, so an
        # exact hit is never overruled by a longer partial one.
        for checklist in listing.get("data") or []:
            if not isinstance(checklist, dict):
                continue
            for item in checklist.get("checkItems") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if wanted and wanted in name.strip().lower():
                    matches.append((to.id_of(item), name,
                                    to.name_of(checklist)))

    if not matches:
        return _error(
            f"No checklist item on '{card.get('name', 'card')}' matches "
            f"'{params.item}'.",
            tc.TRELLO_TARGET_NOT_FOUND)
    if len(matches) > 1:
        names = ", ".join(f"'{m[1]}' (in {m[2]})" for m in matches[:6])
        return _error(
            f"Several checklist items match '{params.item}': {names}. "
            "Name it more precisely.",
            tc.TRELLO_TARGET_AMBIGUOUS)

    item_id, item_name, checklist_name = matches[0]
    state = "complete" if params.complete else "incomplete"
    out = await tc.request(
        ctx, "PUT", f"cards/{card['id']}/checkItem/{item_id}", creds,
        data={"state": state})
    if not out.get("ok"):
        return _from_envelope(out)

    action = "ticked" if params.complete else "unticked"
    return ActionResult.success(
        WriteResult(
            id=item_id,
            name=item_name,
            action=action,
            detail=f"in checklist '{checklist_name}' on "
                   f"'{card.get('name', 'card')}'",
        ),
        f"{action.capitalize()} '{item_name}' in '{checklist_name}'.")


# --------------------------- attachments ---------------------------
# Only LINKS are attachable here. `POST /cards/{id}/attachments` takes an
# uploaded file as multipart/form-data, and every call in this app goes through
# one JSON client -- so offering a `file` parameter would accept a path and then
# have no way to deliver its bytes. A named limitation beats a broken promise.

@chat.function(
    "add_attachment",
    "Attach a link to a Trello card -- a document, an image URL, anything "
    "addressable.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.add_attachment",
    effects=["trello.attachment.created"],
)
async def add_attachment(ctx, params: AddAttachmentParams) -> ActionResult:
    """Attach a URL to a card."""
    url = (params.url or "").strip()
    if not url:
        return _error("An attachment needs a URL. Nothing was sent to Trello.",
                      tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    body = {"url": url}
    if (params.name or "").strip():
        body["name"] = params.name.strip()

    out = await tc.request(ctx, "POST", f"cards/{card['id']}/attachments",
                           creds, params=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    label = to.name_of(made) or url
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=label,
            action="attached",
            detail=f"to '{card.get('name', 'card')}'",
            url=str(made.get("url") or url),
        ),
        f"Attached '{label}' to '{card.get('name', 'card')}'.")


@chat.function(
    "delete_attachment",
    "Remove an attachment from a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_attachment",
    effects=["trello.attachment.deleted"],
)
async def delete_attachment(ctx, params: DeleteAttachmentParams) -> ActionResult:
    """Delete one attachment, found by name or id.

    An uploaded file is destroyed by this, not merely unlinked, so the result
    says which kind it was: for a link the same URL can be re-attached, for an
    upload there is nothing left to re-attach.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    found = await shared.resolve_attachment(
        ctx, creds, card["id"], params.attachment)
    if not found.get("ok"):
        return _from_envelope(found)

    out = await tc.request(
        ctx, "DELETE", f"cards/{card['id']}/attachments/{found['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    kind = "uploaded file" if found.get("is_upload") else "link"
    return ActionResult.success(
        WriteResult(
            id=found["id"],
            name=found.get("name", ""),
            action="deleted",
            detail=f"{kind} removed from '{card.get('name', 'card')}'",
        ),
        f"Removed {kind} '{found.get('name', '')}' from "
        f"'{card.get('name', 'card')}'.")


# --------------------------- comments ---------------------------
# A comment is an ACTION, so editing and deleting address it under the card as
# `.../actions/{idAction}/comments`. The id comes from `list_comments`; there is
# no way to name a comment, which is why these take an id rather than text.

@chat.function(
    "edit_comment",
    "Change the text of a comment already on a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.edit_comment",
    effects=["trello.comment.updated"],
)
async def edit_comment(ctx, params: EditCommentParams) -> ActionResult:
    """Rewrite an existing comment."""
    if not (params.text or "").strip():
        return _error(
            "A comment needs some text. To remove one, use delete_comment.",
            tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    out = await tc.request(
        ctx, "PUT",
        f"cards/{card['id']}/actions/{params.comment_id}/comments",
        creds, params={"text": params.text})
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=params.comment_id,
            name=card.get("name", ""),
            action="comment_updated",
            detail=f"on '{card.get('name', 'card')}'",
        ),
        f"Comment updated on '{card.get('name', 'card')}'.")


@chat.function(
    "delete_comment",
    "Delete a comment from a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_comment",
    effects=["trello.comment.deleted"],
)
async def delete_comment(ctx, params: DeleteCommentParams) -> ActionResult:
    """Remove a comment. Trello offers no undo for this."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    out = await tc.request(
        ctx, "DELETE",
        f"cards/{card['id']}/actions/{params.comment_id}/comments", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=params.comment_id,
            name=card.get("name", ""),
            action="comment_deleted",
            detail=f"from '{card.get('name', 'card')}'",
        ),
        f"Comment deleted from '{card.get('name', 'card')}'.")


# --------------------------- checklists ---------------------------

@chat.function(
    "add_check_item",
    "Add an item to a checklist already on a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.add_check_item",
    effects=["trello.checkitem.created"],
)
async def add_check_item(ctx, params: AddCheckItemParams) -> ActionResult:
    """Append one item to an existing checklist.

    `checklist` may be omitted when the card has exactly one -- the common case,
    and making the user name it would be asking for information the card already
    determines. With several, the resolver refuses rather than picking.
    """
    if not (params.item or "").strip():
        return _error("An item needs some text.", tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    target = await shared.resolve_checklist(
        ctx, creds, card["id"], params.checklist)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(
        ctx, "POST", f"checklists/{target['id']}/checkItems", creds,
        data={"name": params.item.strip(),
              "pos": _position_value(params.position)})
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=to.name_of(made) or params.item.strip(),
            action="created",
            detail=f"in checklist '{target.get('name', '')}' on "
                   f"'{card.get('name', 'card')}'",
        ),
        f"Added '{params.item.strip()}' to '{target.get('name', '')}'.")


@chat.function(
    "delete_check_item",
    "Remove an item from a checklist on a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_check_item",
    effects=["trello.checkitem.deleted"],
)
async def delete_check_item(ctx, params: DeleteCheckItemParams) -> ActionResult:
    """Delete a checklist item, found by its text."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    found = await shared.resolve_check_item(ctx, creds, card["id"], params.item)
    if not found.get("ok"):
        return _from_envelope(found)

    # Addressed under the CARD, not the checklist: that is the route Trello
    # documents for removing an item once it exists.
    out = await tc.request(
        ctx, "DELETE", f"cards/{card['id']}/checkItem/{found['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=found["id"],
            name=found.get("name", ""),
            action="deleted",
            detail=f"from checklist '{found.get('checklist_name', '')}' on "
                   f"'{card.get('name', 'card')}'",
        ),
        f"Deleted '{found.get('name', '')}' from "
        f"'{found.get('checklist_name', 'the checklist')}'.")


@chat.function(
    "update_checklist",
    "Rename a checklist on a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.update_checklist",
    effects=["trello.checklist.updated"],
)
async def update_checklist(ctx, params: UpdateChecklistParams) -> ActionResult:
    """Rename a checklist."""
    if not (params.name or "").strip():
        return _error("A checklist needs a name.", tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    target = await shared.resolve_checklist(
        ctx, creds, card["id"], params.checklist)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(ctx, "PUT", f"checklists/{target['id']}", creds,
                           data={"name": params.name.strip()})
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=params.name.strip(),
            action="renamed",
            detail=f"on '{card.get('name', 'card')}'",
        ),
        f"Checklist renamed to '{params.name.strip()}'.")


@chat.function(
    "delete_checklist",
    "Delete a whole checklist from a Trello card, with its items.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_checklist",
    effects=["trello.checklist.deleted"],
)
async def delete_checklist(ctx, params: DeleteChecklistParams) -> ActionResult:
    """Delete a checklist. Its items go with it and Trello offers no undo."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    target = await shared.resolve_checklist(
        ctx, creds, card["id"], params.checklist)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(ctx, "DELETE", f"checklists/{target['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=target.get("name", ""),
            action="deleted",
            detail=f"removed from '{card.get('name', 'card')}' with its items",
        ),
        f"Deleted checklist '{target.get('name', '')}' and its items from "
        f"'{card.get('name', 'card')}'.")


# --------------------------- lists (columns) ---------------------------

@chat.function(
    "update_list",
    "Rename a Trello list (column), move it, or follow it.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.update_list",
    effects=["trello.list.updated"],
)
async def update_list(ctx, params: UpdateListParams) -> ActionResult:
    """Change a list's name, position or subscription.

    Refuses a no-op: sending an empty PUT would report success while changing
    nothing, which reads as "renamed" to whoever asked.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    target = await _resolve_list(ctx, creds, board["id"], params.list_name)
    if not target.get("ok"):
        return _from_envelope(target)

    body: dict = {}
    changed: list[str] = []
    if (params.name or "").strip():
        body["name"] = params.name.strip()
        changed.append("name")
    if (params.position or "").strip():
        body["pos"] = _position_value(params.position, "top")
        changed.append("position")
    if params.subscribed is not None:
        body["subscribed"] = bool(params.subscribed)
        changed.append("subscribed")

    if not body:
        return _error(
            "Nothing to change: name a new name, a position, or a subscription.",
            tc.TRELLO_VALIDATION_FAILED)

    out = await tc.request(ctx, "PUT", f"lists/{target['id']}", creds,
                           data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=to.name_of(made) or target.get("name", ""),
            action="updated",
            detail=f"changed: {', '.join(changed)}",
        ),
        f"Updated list '{to.name_of(made) or target.get('name', '')}'.")


@chat.function(
    "archive_all_cards",
    "Archive every card in a Trello list at once.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.archive_all_cards",
    effects=["trello.card.archived"],
)
async def archive_all_cards(ctx, params: ListBulkParams) -> ActionResult:
    """Archive a whole column's cards in one call.

    Trello has a dedicated route for this. Archiving card by card would be N
    requests and could fail halfway, leaving a column half-cleared with no
    record of where it stopped.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    target = await _resolve_list(ctx, creds, board["id"], params.list_name)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(
        ctx, "POST", f"lists/{target['id']}/archiveAllCards", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=target.get("name", ""),
            action="archived",
            detail=f"every card in '{target.get('name', 'the list')}' -- "
                   "archived cards are recoverable from the board's archive",
        ),
        f"Archived every card in '{target.get('name', 'the list')}'.")


@chat.function(
    "move_all_cards",
    "Move every card from one Trello list into another.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.move_all_cards",
    effects=["trello.card.moved"],
)
async def move_all_cards(ctx, params: ListBulkParams) -> ActionResult:
    """Move a column's cards into another column.

    Trello needs BOTH the destination list and its board, even within one board,
    so the destination board is resolved explicitly rather than assumed.
    """
    if not (params.to_list or "").strip():
        return _error("Name the destination list.",
                      tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    source = await _resolve_list(ctx, creds, board["id"], params.list_name)
    if not source.get("ok"):
        return _from_envelope(source)

    dest_board = board
    if (params.to_board or "").strip():
        # `resolve_board` takes (ctx, name) -- it loads the credentials itself --
        # and returns the board nested under "board", the same shape move_card
        # relies on. Passing creds and reading found["id"] would have failed on
        # the first cross-board move.
        found = await acct.resolve_board(ctx, params.to_board)
        if not found.get("ok"):
            return _from_envelope(found)
        dest_board = found.get("board", {})

    dest = await _resolve_list(ctx, creds, dest_board["id"], params.to_list)
    if not dest.get("ok"):
        return _from_envelope(dest)

    out = await tc.request(
        ctx, "POST", f"lists/{source['id']}/moveAllCards", creds,
        params={"idBoard": dest_board["id"], "idList": dest["id"]})
    if not out.get("ok"):
        return _from_envelope(out)

    moved = out.get("data")
    count = len(moved) if isinstance(moved, list) else 0
    return ActionResult.success(
        WriteResult(
            id=source["id"],
            name=source.get("name", ""),
            action="moved",
            detail=f"{count} card(s) to '{dest.get('name', '')}'",
        ),
        f"Moved {count} card(s) from '{source.get('name', '')}' to "
        f"'{dest.get('name', '')}'.")


# --------------------------- labels ---------------------------

@chat.function(
    "create_label",
    "Create a new label on a Trello board.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_label",
    effects=["trello.label.created"],
)
async def create_label(ctx, params: CreateLabelParams) -> ActionResult:
    """Create a board label with a name and colour."""
    if not (params.name or "").strip():
        return _error("A label needs a name.", tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    out = await tc.request(ctx, "POST", "labels", creds, data={
        "name": params.name.strip(),
        "color": (params.color or "green").strip().lower(),
        "idBoard": board["id"],
    })
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=to.name_of(made) or params.name.strip(),
            action="created",
            detail=f"{made.get('color', '')} label on "
                   f"'{board.get('name', 'the board')}'",
        ),
        f"Created label '{params.name.strip()}'.")


@chat.function(
    "update_label",
    "Rename a Trello label or change its colour.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.update_label",
    effects=["trello.label.updated"],
)
async def update_label(ctx, params: UpdateLabelParams) -> ActionResult:
    """Change a label's text or colour.

    This edits the label on the BOARD, so every card carrying it changes at once
    -- which is the point, and also why a no-op is refused rather than reported
    as done.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    target = await _resolve_label(ctx, creds, board["id"], params.label)
    if not target.get("ok"):
        return _from_envelope(target)

    body: dict = {}
    if (params.name or "").strip():
        body["name"] = params.name.strip()
    if (params.color or "").strip():
        body["color"] = params.color.strip().lower()
    if not body:
        return _error("Nothing to change: give a new name or a new colour.",
                      tc.TRELLO_VALIDATION_FAILED)

    out = await tc.request(ctx, "PUT", f"labels/{target['id']}", creds,
                           data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=to.name_of(made) or target.get("name", ""),
            action="updated",
            detail="every card carrying this label is affected",
        ),
        f"Updated label '{to.name_of(made) or target.get('name', '')}'.")


@chat.function(
    "delete_label",
    "Delete a label from a Trello board, removing it from every card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_label",
    effects=["trello.label.deleted"],
)
async def delete_label(ctx, params: DeleteLabelParams) -> ActionResult:
    """Delete a board label.

    This is board-wide: the label disappears from every card that had it. That
    is a different act from taking a label off one card, which is what
    `set_card_labels(remove=True)` does -- so the message says which happened.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    target = await _resolve_label(ctx, creds, board["id"], params.label)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(ctx, "DELETE", f"labels/{target['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=target.get("name", ""),
            action="deleted",
            detail="removed from the board and from every card that had it",
        ),
        f"Deleted label '{target.get('name', '')}' from the board -- it is gone "
        f"from every card that had it.")


# --------------------------- boards ---------------------------

@chat.function(
    "update_board",
    "Rename a Trello board, change its description, or close and reopen it.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.update_board",
    effects=["trello.board.updated"],
)
async def update_board(ctx, params: UpdateBoardParams) -> ActionResult:
    """Change a board's name, description or closed state.

    CLOSING is reversible -- Trello keeps the board and everything on it, and
    `closed=false` brings it back. That is why this lives beside a rename rather
    than behind the confirmation gate that `delete_board` carries: the two look
    similar in a menu and could not be more different in consequence.
    """
    picked = await acct.resolve_board(ctx, params.board)
    if not picked.get("ok"):
        return _from_envelope(picked)
    board = picked.get("board", {})
    creds = (picked.get("key", ""), picked.get("token", ""))

    body: dict = {}
    changed: list[str] = []
    if (params.name or "").strip():
        body["name"] = params.name.strip()
        changed.append("name")
    if (params.desc or "").strip():
        body["desc"] = params.desc.strip()
        changed.append("description")
    if params.closed is not None:
        body["closed"] = bool(params.closed)
        changed.append("closed" if params.closed else "reopened")

    if not body:
        return _error(
            "Nothing to change: give a new name, a description, or closed "
            "true/false.", tc.TRELLO_VALIDATION_FAILED)

    out = await tc.request(ctx, "PUT", f"boards/{board['id']}", creds,
                           data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made) or board.get("id", ""),
            name=to.name_of(made) or board.get("name", ""),
            action="updated",
            detail=f"changed: {', '.join(changed)}",
            url=str(made.get("shortUrl") or made.get("url") or ""),
        ),
        f"Updated board '{to.name_of(made) or board.get('name', '')}' "
        f"({', '.join(changed)}).")


@chat.function(
    "delete_board",
    "Permanently delete a Trello board and everything on it. Cannot be undone.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_board",
    effects=["trello.board.deleted"],
)
async def delete_board(ctx, params: DeleteBoardParams) -> ActionResult:
    """Delete a board permanently.

    Gated on an explicit `confirm`, unlike `delete_card`: a card is one item,
    while a board takes every list, card, comment and attachment on it, and
    Trello has no undo and no trash for boards. Closing (`update_board` with
    closed=true) does what most people mean and is reversible, so the refusal
    below names it.
    """
    if not params.confirm:
        return _error(
            "Deleting a board is permanent -- every list, card, comment and "
            "attachment on it goes too, and Trello offers no undo. Pass "
            "confirm=true if that is really the intent. To hide a board "
            "reversibly instead, close it: update_board with closed=true.",
            tc.TRELLO_VALIDATION_FAILED)

    picked = await acct.resolve_board(ctx, params.board)
    if not picked.get("ok"):
        return _from_envelope(picked)
    board = picked.get("board", {})
    creds = (picked.get("key", ""), picked.get("token", ""))

    out = await tc.request(ctx, "DELETE", f"boards/{board['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=board.get("id", ""),
            name=board.get("name", ""),
            action="deleted",
            detail="permanently -- the board and all its contents are gone",
        ),
        f"Deleted board '{board.get('name', '')}' permanently.")


@chat.function(
    "set_board_member",
    "Add someone to a Trello board, change their role, or remove them.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_board_member",
    effects=["trello.board.member_changed"],
)
async def set_board_member(ctx, params: BoardMemberParams) -> ActionResult:
    """Invite, re-role or remove a board member.

    Adding is by EMAIL through `PUT /boards/{id}/members`, which invites someone
    who has no Trello account yet. Removing needs a member ID, so a name is
    resolved against the board's current members first -- an email cannot be
    used to remove, because Trello matches removals by id only.
    """
    picked = await acct.resolve_board(ctx, params.board)
    if not picked.get("ok"):
        return _from_envelope(picked)
    board = picked.get("board", {})
    creds = (picked.get("key", ""), picked.get("token", ""))

    reference = (params.member or "").strip()
    if not reference:
        return _error("Name the person to add or remove.",
                      tc.TRELLO_VALIDATION_FAILED)

    role = (params.role or "normal").strip().lower()
    if role not in ("normal", "admin", "observer"):
        return _error(
            f"'{params.role}' is not a Trello board role. Use 'normal', "
            "'admin' or 'observer'.", tc.TRELLO_VALIDATION_FAILED)

    if params.remove:
        found = await shared.resolve_member(ctx, creds, board["id"], reference)
        if not found.get("ok"):
            return _from_envelope(found)
        out = await tc.request(
            ctx, "DELETE", f"boards/{board['id']}/members/{found['id']}", creds)
        if not out.get("ok"):
            return _from_envelope(out)
        return ActionResult.success(
            WriteResult(
                id=found["id"],
                name=found.get("name", reference),
                action="removed",
                detail=f"from board '{board.get('name', '')}'",
            ),
            f"Removed {found.get('name', reference)} from "
            f"'{board.get('name', '')}'.")

    if "@" in reference:
        out = await tc.request(
            ctx, "PUT", f"boards/{board['id']}/members", creds,
            params={"email": reference, "type": role})
    else:
        # No email: this must be someone already reachable, so resolve to an id
        # and set the role directly. Trello's add-by-email route needs an email.
        found = await shared.resolve_member(ctx, creds, board["id"], reference)
        if not found.get("ok"):
            return _from_envelope(found)
        out = await tc.request(
            ctx, "PUT", f"boards/{board['id']}/members/{found['id']}", creds,
            params={"type": role})

    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=board.get("id", ""),
            name=reference,
            action="invited" if "@" in reference else "role_changed",
            detail=f"as {role} on board '{board.get('name', '')}'",
        ),
        f"{reference} is now {role} on '{board.get('name', '')}'.")


# --------------------------- copy a card ---------------------------

@chat.function(
    "copy_card",
    "Copy a Trello card -- to the same list or another one, optionally onto a "
    "different board.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.copy_card",
    effects=["trello.card.created"],
)
async def copy_card(ctx, params: CopyCardParams) -> ActionResult:
    """Duplicate a card via `POST /cards` with `idCardSource`.

    Trello has no /copy route: a copy is a CREATE that names a source. What
    comes along is controlled by `keepFromSource`, which defaults to `all` here
    because a "copy" that silently dropped the checklists and attachments would
    not be one.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    dest_board = board
    if (params.to_board or "").strip():
        found = await acct.resolve_board(ctx, params.to_board)
        if not found.get("ok"):
            return _from_envelope(found)
        dest_board = found.get("board", {})

    destination = (params.to_list or "").strip()
    if not destination and dest_board.get("id") != board.get("id"):
        return _error(
            f"Copying onto board '{dest_board.get('name', '')}' also needs the "
            "destination list -- lists belong to one board, so the card's "
            "current list does not exist over there.",
            tc.TRELLO_VALIDATION_FAILED)

    if destination:
        target = await _resolve_list(ctx, creds, dest_board["id"], destination)
        if not target.get("ok"):
            return _from_envelope(target)
        list_id = target["id"]
        list_name = target.get("name", destination)
    else:
        # Same list as the source: read it off the card rather than making the
        # user name where the card already is.
        list_id = str(card.get("idList") or "")
        list_name = ""
        if not list_id:
            detail = await tc.request(ctx, "GET", f"cards/{card['id']}", creds,
                                      params={"fields": "idList"})
            if not detail.get("ok"):
                return _from_envelope(detail)
            list_id = str((detail.get("data") or {}).get("idList") or "")
        if not list_id:
            return _error(
                "Could not tell which list the source card is in, so there is "
                "nowhere to put the copy. Name a destination list.",
                tc.TRELLO_RESPONSE_UNEXPECTED)

    body = {
        "idList": list_id,
        "idCardSource": card["id"],
        "keepFromSource": (params.keep or "all").strip() or "all",
        "pos": _position_value(params.position),
    }
    if (params.name or "").strip():
        body["name"] = params.name.strip()

    out = await tc.request(ctx, "POST", "cards", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    where = f" in '{list_name}'" if list_name else ""
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=to.name_of(made),
            action="copied",
            detail=f"from '{card.get('name', 'card')}'{where}",
            url=str(made.get("shortUrl") or made.get("url") or ""),
        ),
        f"Copied '{card.get('name', 'card')}' to '{to.name_of(made)}'{where}.")


# --------------------------- custom fields ---------------------------
# THE WRITE SHAPE IS DECIDED BY THE FIELD TYPE, per Trello's custom fields
# guide: a scalar goes in as {"value": {"text"|"number"|"date"|"checked": "..."}}
# with every value a STRING, while a dropdown takes {"idValue": "<option id>"}
# and no `value` key at all. `shared.custom_field_body` owns that mapping so the
# user passes one `value` and never has to know which JSON key Trello wants.

@chat.function(
    "create_custom_field",
    "Add a custom field to a Trello board -- text, number, date, checkbox or a "
    "dropdown.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_custom_field",
    effects=["trello.custom_field.created"],
)
async def create_custom_field(ctx, params: CreateCustomFieldParams) -> ActionResult:
    """Create a custom field definition on a board.

    A dropdown without options is refused: Trello accepts it, and the result is
    a field nobody can set a value on -- which looks like a broken field rather
    than an incomplete request.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    kind = (params.field_type or "text").strip().lower()
    aliases = {"checkbox": "checkbox", "check": "checkbox", "bool": "checkbox",
               "dropdown": "list", "select": "list", "list": "list",
               "text": "text", "string": "text",
               "number": "number", "num": "number",
               "date": "date"}
    resolved = aliases.get(kind)
    if not resolved:
        return _error(
            f"'{params.field_type}' is not a Trello custom field type. Use one "
            "of: text, number, date, checkbox, list (dropdown).",
            tc.TRELLO_VALIDATION_FAILED)

    options = _split_names(params.options)
    if resolved == "list" and not options:
        return _error(
            "A dropdown field needs its choices: pass options as a "
            "comma-separated list. Trello would accept the field without them, "
            "but no value could ever be set on it.",
            tc.TRELLO_VALIDATION_FAILED)

    body: dict = {
        "idModel": board["id"],
        "modelType": "board",
        "name": params.name,
        "type": resolved,
        "display_cardFront": "true" if params.show_on_card else "false",
    }
    if resolved == "list":
        # Options ride along at creation as pos/value pairs.
        body["options"] = [
            {"value": {"text": opt}, "pos": (i + 1) * 1024}
            for i, opt in enumerate(options)
        ]

    out = await tc.request(ctx, "POST", "customFields", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    detail = f"{resolved} field on '{board.get('name', '')}'"
    if options:
        detail += f" with {len(options)} option(s)"
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=to.name_of(made) or params.name,
            action="created",
            detail=detail,
        ),
        f"Created custom field '{params.name}' ({resolved}).")


@chat.function(
    "set_custom_field",
    "Set or clear the value of a custom field on a Trello card.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_custom_field",
    effects=["trello.custom_field.updated"],
)
async def set_custom_field(ctx, params: SetCustomFieldParams) -> ActionResult:
    """Set one custom field value on one card, or clear it.

    CLEARING IS AN EMPTY PUT, not a DELETE -- Trello has no delete route for a
    card's field value, and sending one 404s in a way that reads like the card
    is missing.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    field = await shared.resolve_custom_field(
        ctx, creds, board["id"], params.field)
    if not field.get("ok"):
        return _from_envelope(field)

    path = f"cards/{card['id']}/customField/{field['id']}/item"

    if params.clear:
        # An empty value object is how Trello removes a value.
        out = await tc.request(ctx, "PUT", path, creds, data={"value": {}})
        if not out.get("ok"):
            return _from_envelope(out)
        return ActionResult.success(
            WriteResult(
                id=field["id"],
                name=field.get("name", ""),
                action="cleared",
                detail=f"on '{card.get('name', 'card')}'",
            ),
            f"Cleared '{field.get('name', '')}' on "
            f"'{card.get('name', 'card')}'.")

    if not (params.value or "").strip():
        return _error(
            "No value given. Pass a value, or clear=true to empty the field.",
            tc.TRELLO_VALIDATION_FAILED)

    body = shared.custom_field_body(
        field.get("field_type", ""), params.value, field.get("options_raw") or [])
    # An EMPTY body means a dropdown value matched none of its options. Sending
    # it would be a 200 that changed nothing -- reported as "set" to whoever
    # asked. `body.get("ok", True)` did not catch this: {} has no "ok" key, so
    # the default said True and the empty write went out.
    if not body:
        available = ", ".join(o for o in (field.get("options") or []) if o) or "-"
        return _error(
            f"'{params.value}' is not one of the choices on "
            f"'{field.get('name', '')}'. Available: {available}.",
            tc.TRELLO_TARGET_NOT_FOUND)

    out = await tc.request(ctx, "PUT", path, creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=field["id"],
            name=field.get("name", ""),
            action="updated",
            detail=f"set to '{params.value}' on '{card.get('name', 'card')}'",
        ),
        f"Set '{field.get('name', '')}' to '{params.value}' on "
        f"'{card.get('name', 'card')}'.")


@chat.function(
    "set_custom_field_option",
    "Add or remove a choice on a Trello dropdown custom field.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_custom_field_option",
    effects=["trello.custom_field.updated"],
)
async def set_custom_field_option(ctx, params: CustomFieldOptionParams) -> ActionResult:
    """Add a dropdown choice, or remove one."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    field = await shared.resolve_custom_field(
        ctx, creds, board["id"], params.field)
    if not field.get("ok"):
        return _from_envelope(field)

    if field.get("field_type") != "list":
        return _error(
            f"'{field.get('name', '')}' is a {field.get('field_type') or 'plain'} "
            "field, not a dropdown -- only dropdown fields have options.",
            tc.TRELLO_VALIDATION_FAILED)

    wanted = (params.option or "").strip()
    if not wanted:
        return _error("Name the option.", tc.TRELLO_VALIDATION_FAILED)

    if params.remove:
        match_id = ""
        for opt in field.get("options_raw") or []:
            text = str(((opt or {}).get("value") or {}).get("text") or "")
            if text.strip().lower() == wanted.lower():
                match_id = to.id_of(opt)
                break
        if not match_id:
            available = ", ".join(field.get("options") or []) or "-"
            return _error(
                f"'{wanted}' is not an option on '{field.get('name', '')}'. "
                f"Available: {available}.",
                tc.TRELLO_TARGET_NOT_FOUND)
        out = await tc.request(
            ctx, "DELETE", f"customFields/{field['id']}/options/{match_id}",
            creds)
        if not out.get("ok"):
            return _from_envelope(out)
        return ActionResult.success(
            WriteResult(
                id=match_id,
                name=wanted,
                action="deleted",
                detail=f"option removed from '{field.get('name', '')}' -- and "
                       "from every card that had it selected",
            ),
            f"Removed option '{wanted}' from '{field.get('name', '')}'.")

    out = await tc.request(
        ctx, "POST", f"customFields/{field['id']}/options", creds,
        data={"value": {"text": wanted}})
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=wanted,
            action="created",
            detail=f"option added to '{field.get('name', '')}'",
        ),
        f"Added option '{wanted}' to '{field.get('name', '')}'.")


@chat.function(
    "delete_custom_field",
    "Delete a custom field from a Trello board, with its value on every card.",
    action_type="destructive", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_custom_field",
    effects=["trello.custom_field.deleted"],
)
async def delete_custom_field(ctx, params: DeleteCustomFieldParams) -> ActionResult:
    """Delete a custom field definition. Gated, because the values go too."""
    if not params.confirm:
        return _error(
            "Deleting a custom field also deletes its value on every card on "
            "the board, and Trello offers no undo. Pass confirm=true if that is "
            "really the intent.",
            tc.TRELLO_VALIDATION_FAILED)

    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    field = await shared.resolve_custom_field(
        ctx, creds, board["id"], params.field)
    if not field.get("ok"):
        return _from_envelope(field)

    out = await tc.request(ctx, "DELETE", f"customFields/{field['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=field["id"],
            name=field.get("name", ""),
            action="deleted",
            detail="removed from the board with its value on every card",
        ),
        f"Deleted custom field '{field.get('name', '')}' -- its value is gone "
        "from every card that had one.")


# --------------------------- stickers and votes ---------------------------

@chat.function(
    "set_sticker",
    "Put a sticker on a Trello card, or take one off.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_sticker",
    effects=["trello.sticker.updated"],
)
async def set_sticker(ctx, params: StickerParams) -> ActionResult:
    """Add or remove a sticker.

    Trello's free stickers are named images (`taco-cool`, `thumbsup`, ...). An
    unknown name is rejected by Trello itself rather than pre-validated here:
    the set differs per account -- paid boards carry custom stickers -- so a
    hardcoded allow-list would refuse stickers the user actually has.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    wanted = (params.sticker or "").strip()
    if not wanted:
        return _error("Name the sticker.", tc.TRELLO_VALIDATION_FAILED)

    if params.remove:
        listing = await tc.request(
            ctx, "GET", f"cards/{card['id']}/stickers", creds)
        if not listing.get("ok"):
            return _from_envelope(listing)
        rows = [r for r in (listing.get("data") or []) if isinstance(r, dict)]
        match_id = ""
        for row in rows:
            if str(row.get("image") or "").strip().lower() == wanted.lower():
                match_id = to.id_of(row)
                break
        if not match_id:
            present = ", ".join(str(r.get("image") or "") for r in rows) or "-"
            return _error(
                f"No '{wanted}' sticker on this card. Present: {present}.",
                tc.TRELLO_TARGET_NOT_FOUND)
        out = await tc.request(
            ctx, "DELETE", f"cards/{card['id']}/stickers/{match_id}", creds)
        if not out.get("ok"):
            return _from_envelope(out)
        return ActionResult.success(
            WriteResult(id=match_id, name=wanted, action="deleted",
                        detail=f"sticker removed from '{card.get('name', 'card')}'"),
            f"Removed the '{wanted}' sticker.")

    out = await tc.request(
        ctx, "POST", f"cards/{card['id']}/stickers", creds,
        params={"image": wanted, "top": 0, "left": 0, "zIndex": 1})
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(id=to.id_of(made), name=wanted, action="created",
                    detail=f"sticker added to '{card.get('name', 'card')}'"),
        f"Put the '{wanted}' sticker on '{card.get('name', 'card')}'.")


@chat.function(
    "set_vote",
    "Vote on a Trello card, or take a vote back.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_vote",
    effects=["trello.vote.updated"],
)
async def set_vote(ctx, params: VoteParams) -> ActionResult:
    """Cast or withdraw a vote.

    Voting needs the Voting Power-Up enabled on the board; without it Trello
    refuses the write, and that refusal is passed through rather than dressed up
    as success.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await _resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    who = (params.member or "me").strip() or "me"
    member = await shared.resolve_member(ctx, creds, board["id"], who)
    if not member.get("ok"):
        return _from_envelope(member)

    if params.remove:
        out = await tc.request(
            ctx, "DELETE",
            f"cards/{card['id']}/membersVoted/{member['id']}", creds)
        action, word = "deleted", "Withdrew"
    else:
        out = await tc.request(
            ctx, "POST", f"cards/{card['id']}/membersVoted", creds,
            params={"value": member["id"]})
        action, word = "created", "Cast"
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=card["id"],
            name=card.get("name", ""),
            action=action,
            detail=f"vote by {member.get('name', 'member')}",
        ),
        f"{word} {member.get('name', 'member')}'s vote on "
        f"'{card.get('name', 'card')}'.")


# --------------------------- workspaces ---------------------------
# Trello calls these ORGANIZATIONS in the API and WORKSPACES in the interface.
# The tools use the interface word, since that is what the user sees, and the
# routes use the API word underneath.

@chat.function(
    "create_workspace",
    "Create a Trello workspace.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.create_workspace",
    effects=["trello.workspace.created"],
)
async def create_workspace(ctx, params: CreateWorkspaceParams) -> ActionResult:
    """Create a workspace (an organization, in API terms)."""
    creds, _board, err = await _resolve(ctx, "")
    if err and not creds:
        return err

    body = {"displayName": params.name}
    if (params.desc or "").strip():
        body["desc"] = params.desc.strip()
    if (params.website or "").strip():
        body["website"] = params.website.strip()

    out = await tc.request(ctx, "POST", "organizations", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=str(made.get("displayName") or params.name),
            action="created",
            detail="workspace created",
            url=str(made.get("url") or ""),
        ),
        f"Created workspace '{params.name}'.")


@chat.function(
    "update_workspace",
    "Rename a Trello workspace or change its description or website.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.update_workspace",
    effects=["trello.workspace.updated"],
)
async def update_workspace(ctx, params: UpdateWorkspaceParams) -> ActionResult:
    """Change a workspace's name, description or website.

    THE EMPTY-CHANGE GUARD RUNS FIRST, before credentials are even looked up. A
    no-op does not depend on being connected, and checking connection first
    reported "no credentials" for a request that was malformed anyway -- the
    user would go fix the wrong thing.
    """
    body: dict = {}
    changed: list[str] = []
    if (params.name or "").strip():
        body["displayName"] = params.name.strip()
        changed.append("name")
    if (params.desc or "").strip():
        body["desc"] = params.desc.strip()
        changed.append("description")
    if (params.website or "").strip():
        body["website"] = params.website.strip()
        changed.append("website")

    if not body:
        return _error("Nothing to change: give a new name, description or "
                      "website.", tc.TRELLO_VALIDATION_FAILED)

    creds, _board, err = await _resolve(ctx, "")
    if err and not creds:
        return err

    target = await shared.resolve_workspace(ctx, creds, params.workspace)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(
        ctx, "PUT", f"organizations/{target['id']}", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=target["id"],
            name=params.name.strip() or target.get("name", ""),
            action="updated",
            detail=f"changed: {', '.join(changed)}",
        ),
        f"Updated workspace '{target.get('name', '')}'.")


@chat.function(
    "set_workspace_member",
    "Add someone to a Trello workspace, change their role, or remove them.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.set_workspace_member",
    effects=["trello.workspace.updated"],
)
async def set_workspace_member(ctx, params: WorkspaceMemberParams) -> ActionResult:
    """Add, promote or remove a workspace member.

    Removing from the WORKSPACE does not remove someone from its boards -- Trello
    keeps board membership separate -- so the result says so, because "removed"
    otherwise implies an access revocation that has not happened.
    """
    creds, _board, err = await _resolve(ctx, "")
    if err and not creds:
        return err

    target = await shared.resolve_workspace(ctx, creds, params.workspace)
    if not target.get("ok"):
        return _from_envelope(target)

    who = (params.member or "").strip()
    if not who:
        return _error("Name the person.", tc.TRELLO_VALIDATION_FAILED)

    if params.remove:
        listing = await tc.request(
            ctx, "GET", f"organizations/{target['id']}/members", creds,
            params={"fields": "fullName,username"})
        if not listing.get("ok"):
            return _from_envelope(listing)
        rows = [r for r in (listing.get("data") or []) if isinstance(r, dict)]
        match_id = ""
        lowered = who.lower()
        for row in rows:
            if lowered in (str(row.get("fullName") or "").lower(),
                           str(row.get("username") or "").lower()):
                match_id = to.id_of(row)
                break
        if not match_id and to.looks_like_id(who):
            match_id = who
        if not match_id:
            names = ", ".join(str(r.get("fullName") or r.get("username") or "")
                              for r in rows) or "-"
            return _error(
                f"'{who}' is not in this workspace. Members: {names}.",
                tc.TRELLO_TARGET_NOT_FOUND)
        out = await tc.request(
            ctx, "DELETE", f"organizations/{target['id']}/members/{match_id}",
            creds)
        if not out.get("ok"):
            return _from_envelope(out)
        return ActionResult.success(
            WriteResult(
                id=match_id, name=who, action="deleted",
                detail="removed from the workspace; board access is separate "
                       "and is unchanged",
            ),
            f"Removed {who} from '{target.get('name', '')}'. Their access to "
            "individual boards is separate and unchanged.")

    role = (params.role or "normal").strip().lower()
    if role not in ("normal", "admin"):
        return _error("A workspace role is 'normal' or 'admin'.",
                      tc.TRELLO_VALIDATION_FAILED)

    body = {"type": role}
    if "@" in who:
        body["email"] = who
    else:
        body["fullName"] = who

    out = await tc.request(
        ctx, "PUT", f"organizations/{target['id']}/members", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(id=target["id"], name=who, action="updated",
                    detail=f"invited to '{target.get('name', '')}' as {role}"),
        f"Added {who} to '{target.get('name', '')}' as {role}.")


@chat.function(
    "delete_workspace",
    "Permanently delete a Trello workspace.",
    action_type="destructive", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.delete_workspace",
    effects=["trello.workspace.deleted"],
)
async def delete_workspace(ctx, params: DeleteWorkspaceParams) -> ActionResult:
    """Delete a workspace. Gated: its boards outlive it, but it does not."""
    if not params.confirm:
        return _error(
            "Deleting a workspace cannot be undone. Its boards are not deleted "
            "-- they become personal boards of their owners -- but the "
            "workspace, its members list and its settings are gone. Pass "
            "confirm=true if that is really the intent.",
            tc.TRELLO_VALIDATION_FAILED)

    creds, _board, err = await _resolve(ctx, "")
    if err and not creds:
        return err

    target = await shared.resolve_workspace(ctx, creds, params.workspace)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await tc.request(
        ctx, "DELETE", f"organizations/{target['id']}", creds)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=target["id"], name=target.get("name", ""), action="deleted",
            detail="workspace deleted; its boards became personal boards",
        ),
        f"Deleted workspace '{target.get('name', '')}'. Its boards were not "
        "deleted -- they are now personal boards.")


# --------------------------- board copy, list move ---------------------------

@chat.function(
    "copy_board",
    "Copy a Trello board, with or without its cards.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.copy_board",
    effects=["trello.board.created"],
)
async def copy_board(ctx, params: CopyBoardParams) -> ActionResult:
    """Copy a board.

    `keep_cards=false` copies the STRUCTURE only -- lists, labels and custom
    fields without the cards, which is how a board becomes a template.
    """
    picked = await acct.resolve_board(ctx, params.board)
    if not picked.get("ok"):
        return _from_envelope(picked)
    source = picked.get("board", {})
    creds = (picked.get("key", ""), picked.get("token", ""))

    body = {
        "name": params.name,
        "idBoardSource": source["id"],
        # Trello's own vocabulary: "cards" copies them, "none" leaves them out.
        "keepFromSource": "cards" if params.keep_cards else "none",
    }
    if (params.workspace or "").strip():
        target = await shared.resolve_workspace(ctx, creds, params.workspace)
        if not target.get("ok"):
            return _from_envelope(target)
        body["idOrganization"] = target["id"]

    out = await tc.request(ctx, "POST", "boards", creds, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    made = out.get("data") or {}
    what = "with its cards" if params.keep_cards else "structure only, no cards"
    return ActionResult.success(
        WriteResult(
            id=to.id_of(made),
            name=to.name_of(made) or params.name,
            action="created",
            detail=f"copied from '{source.get('name', '')}' ({what})",
            url=str(made.get("url") or ""),
        ),
        f"Copied '{source.get('name', '')}' to '{params.name}' ({what}).")


@chat.function(
    "move_list_to_board",
    "Move a whole Trello list (column), with its cards, to another board.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="trello-connector.move_list_to_board",
    effects=["trello.list.updated"],
)
async def move_list_to_board(ctx, params: MoveListParams) -> ActionResult:
    """Move a list and everything on it to a different board.

    Distinct from `move_all_cards`, which sends the cards and leaves the column
    behind. This moves the column itself.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    source = await _resolve_list(ctx, creds, board["id"], params.list_name)
    if not source.get("ok"):
        return _from_envelope(source)

    found = await acct.resolve_board(ctx, params.to_board)
    if not found.get("ok"):
        return _from_envelope(found)
    destination = found.get("board", {})

    if destination.get("id") == board["id"]:
        return _error(
            f"'{destination.get('name', '')}' is the board the list is already "
            "on. To reorder it there, use update_list with a position.",
            tc.TRELLO_VALIDATION_FAILED)

    out = await tc.request(
        ctx, "PUT", f"lists/{source['id']}/idBoard", creds,
        params={"value": destination["id"]})
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        WriteResult(
            id=source["id"],
            name=source.get("name", ""),
            action="moved",
            detail=f"to board '{destination.get('name', '')}' with its cards",
        ),
        f"Moved '{source.get('name', '')}' to "
        f"'{destination.get('name', '')}'.")
