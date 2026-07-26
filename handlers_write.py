"""Write tools: create/update/move/archive/delete cards, comments, members,
labels, lists, boards, checklists -- plus `connect_account`, which stores the
credential pair.

Four Trello shapes drive this file, all verified against the docs:

* WRITES USE PUT, NOT PATCH. `PUT /cards/{id}` updates a card; there is no
  PATCH route, and sending one is a 404 that reads like a missing card.
* NO REQUEST ENVELOPE. Unlike Asana's `{"data": {...}}`, a Trello write body is
  the fields themselves. `trello_client.request` sends `data` as the JSON body
  and always appends `key`/`token` to the query string.
* A COMMENT IS AN ACTION. Posting one is `POST /cards/{id}/actionsComments`
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
    AddCommentParams,
    ArchiveCardParams,
    ArchiveListParams,
    CardLabelsParams,
    CardMembersParams,
    CheckItemParams,
    ConnectAccountParams,
    ConnectResult,
    CreateBoardParams,
    CreateCardParams,
    CreateChecklistParams,
    CreateListParams,
    DeleteCardParams,
    MoveCardParams,
    UpdateCardParams,
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
    `commentCard` via `/cards/{id}/actionsComments`, which is why the response
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

    out = await tc.request(ctx, "POST", f"cards/{card['id']}/actionsComments",
                           creds, data={"text": params.comment})
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
