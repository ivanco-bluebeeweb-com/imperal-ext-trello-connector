"""Helpers shared by the read and write tool layers.

These deliberately do NOT live in `handlers_read.py`: putting them there would
make `handlers_write.py` import PRIVATE names from a sibling layer -- a
dependency that says "write is built on read" when the two are really peers.
That mistake was made once in the Notion connector and had to be undone; both
layers depend on this module instead.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acct
import trello_client as tc
import trello_objects as to
from models import TrelloBoard, TrelloCard, TrelloList

# These are imported AT MODULE LEVEL, deliberately. They were once imported
# lazily inside each function, which looked harmless -- `models` has no
# dependency back on this package, so there is no cycle to break. But an
# extension runs alongside its siblings, and a bare `from models import X`
# evaluated at CALL time resolves against whatever `models` module already sits
# in the interpreter's cache -- another extension's `models.py`. The failure was
# intermittent and blamed a stranger: "cannot import name 'TrelloCard' from
# 'models' (/opt/extensions/wp-site-connector-extension/models.py)". Resolving
# the name at IMPORT time, while this app's own path is what loaded it, is what
# makes the binding ours.


# The one sentence that explains Trello's access model. Reused verbatim wherever
# emptiness might otherwise read as a bug. Unlike Notion, Trello has no
# per-object sharing step -- but unlike Asana, its default board visibility is
# private-to-members, so "I can see it in my browser" and "the token can see it"
# come apart when the token belongs to a different account than the browser.
ACCESS_NOTE = (
    "Trello shows whatever the account behind the token can already see -- "
    "there is no per-board sharing step to perform. An empty result usually "
    "means the token belongs to a different Trello account than the one you "
    "are looking at, the board was closed, or the account is not a member of it."
)


def error(message: str, code: str, retryable: bool = False) -> ActionResult:
    """Error result carrying a structured code.

    `code` is mandatory on purpose. The kernel stamps EXT_UNSTRUCTURED_ERROR on
    any error emitted without one (I-EXT-ERROR-CODE-NORMALIZED), which turns a
    precise failure into un-actionable prose -- exactly the bug that made WP
    Publisher's failures unreadable. Validator rule V32 only flags literal
    `ActionResult.error(` call sites, so routing every error through a helper
    would hide this app from the rule; hence the positional argument, which
    makes a code-less error a TypeError at authoring time rather than a silent
    downgrade in production.
    """
    return ActionResult.error(message, retryable, code=code)


def from_envelope(out: dict) -> ActionResult:
    """Convert a trello_client error envelope into an ActionResult."""
    return error(out.get("error") or tc.message_for(out.get("code", "")),
                 out.get("code") or tc.TRELLO_HTTP_ERROR,
                 bool(out.get("retryable")))


async def resolve(ctx, board: str) -> tuple[tuple[str, str], dict,
                                            ActionResult | None]:
    """Resolve credentials and board, or hand back a ready-made error.

    Returns ((key, token), board_row, None) on success, or (("", ""), {},
    ActionResult) when the caller should return that error unchanged.

    The credentials travel as ONE pair-shaped value rather than two loose
    strings: `trello_client.request` already accepts a (key, token) pair, and
    keeping them fused means a call site cannot pass the key while forgetting
    the token -- the failure mode that a two-value return invites.
    """
    picked = await acct.resolve_board(ctx, board)
    if not picked.get("ok"):
        return ("", ""), {}, from_envelope(picked)
    creds = (picked["key"], picked["token"])
    return creds, picked.get("board", {}), None


async def any_credentials(ctx) -> tuple[tuple[str, str], ActionResult | None]:
    """The first working credential pair, for calls that are not board-scoped.

    `/search` and `/members/me` reach across every board a token can see, so
    demanding a board first would be a question with no purpose. Returns
    ((key, token), None) or (("", ""), error).
    """
    pairs = await acct.load_pairs(ctx)
    if not pairs:
        return ("", ""), error(
            tc.message_for(tc.TRELLO_CREDENTIALS_MISSING),
            tc.TRELLO_CREDENTIALS_MISSING)
    return (pairs[0][0], pairs[0][1]), None


def _creds_parts(creds) -> tuple[str, str]:
    """Split a pair-shaped credential into (key, token) for the accounts layer.

    `accounts` takes the halves separately because it also builds them; the
    handler layers pass the fused pair. This is the one place that bridges the
    two, so neither side has to know about the other's shape.
    """
    if isinstance(creds, (tuple, list)) and len(creds) == 2:
        return creds[0] or "", creds[1] or ""
    if isinstance(creds, dict):
        return creds.get("key") or "", creds.get("token") or ""
    return "", ""


async def resolve_card(ctx, creds, board_id: str, reference: str) -> dict:
    """Resolve a card reference to an id, or return an error envelope."""
    key, token = _creds_parts(creds)
    return await acct.resolve_target(ctx, key, token, board_id, reference,
                                     kind="card")


async def resolve_list(ctx, creds, board_id: str, reference: str) -> dict:
    """Resolve a list (column) reference to an id."""
    key, token = _creds_parts(creds)
    return await acct.resolve_target(ctx, key, token, board_id, reference,
                                     kind="list")


async def resolve_member(ctx, creds, board_id: str, reference: str) -> dict:
    """Resolve a member reference to an id.

    'me' is special-cased because it is the single most common member a user
    names. Unlike Asana, Trello does NOT accept the literal string `me` where a
    member id is expected in a card mutation -- `/members/me` is a lookup route,
    not an id -- so this resolves it to the real id of the token's owner rather
    than forwarding the word and getting a 400.
    """
    key, token = _creds_parts(creds)
    ref = (reference or "").strip()
    if ref.lower() == "me":
        out = await acct.describe_pair(ctx, key, token)
        if not out.get("ok"):
            return tc.fail(out.get("code") or tc.TRELLO_TOKEN_REJECTED,
                           out.get("error") or "")
        return {"ok": True, "id": out.get("member_id", ""),
                "name": out.get("member_name", "me"), "resolved_by": "self"}
    return await acct.resolve_target(ctx, key, token, board_id, ref,
                                     kind="member")


def board_entity(row) -> "object":
    """Flatten a Trello board into its display entity.

    Lives here rather than in either handler layer because both read and write
    return boards, and a divergent flattening is how the same board ends up
    looking different depending on which tool produced it.
    """
    return TrelloBoard(
        id=to.id_of(row),
        # `title` is what the card RENDERS; `name` is what the next tool in a
        # chain reads. Declaring one and filling the other hands the chain an
        # empty string, or the display a blank row.
        title=to.name_of(row) or "(unnamed board)",
        name=to.name_of(row),
        closed=bool(row.get("closed")) if isinstance(row, dict) else False,
        account_name=str(row.get("account_name") or "")
        if isinstance(row, dict) else "",
        url=to.board_url(row),
    )


def list_entity(row) -> "object":
    """Flatten a Trello list (column) into its display entity.

    `card_count` is filled only when the caller asked Trello to embed the cards
    (`cards=open` on the lists route). Without that, Trello says nothing about
    how full a column is -- and a hardcoded 0 is worse than an absent number,
    because "Today: 0 cards" reads as an empty column rather than as "not
    counted". So the count comes from the embedded array when it is there.
    """
    data = row if isinstance(row, dict) else {}
    cards = data.get("cards")
    return TrelloList(
        id=to.id_of(data),
        title=to.name_of(data) or "(unnamed list)",
        name=to.name_of(data),
        closed=bool(data.get("closed")),
        board=str(data.get("idBoard") or ""),
        card_count=len(cards) if isinstance(cards, list) else 0,
    )


def card_entity(row, list_names: dict | None = None) -> "object":
    """Flatten a Trello card into its display entity.

    Every field is read defensively: Trello only returns what `fields` asked
    for, and a nested `members`/`labels` array is present only when the caller
    requested it. A missing piece yields "" rather than a guess.

    `list_names` maps list id -> list name. It exists because Trello's "get
    cards on a board" route does NOT accept a `list=true` parameter -- that was
    checked against Atlassian's docs, not assumed -- so a card arrives carrying
    `idList` and nothing else about its column. Reading a nested `list` object
    (as this used to) therefore always yielded "", and every card displayed
    with a blank column: on a board whose whole meaning is Today / This Week /
    Later, that erases the only field that says what is urgent. The mapping is
    supplied by the caller so it costs ONE request per listing rather than one
    per card.
    """
    data = row if isinstance(row, dict) else {}
    nested = data.get("list")
    resolved = to.name_of(nested) if isinstance(nested, dict) else ""
    if not resolved and list_names:
        resolved = str(list_names.get(str(data.get("idList") or "")) or "")
    badges = data.get("badges") if isinstance(data.get("badges"), dict) else {}
    return TrelloCard(
        id=to.id_of(data),
        title=to.name_of(data) or "(unnamed card)",
        name=to.name_of(data),
        list_name=resolved,
        closed=bool(data.get("closed")),
        due=str(data.get("due") or ""),
        due_complete=bool(data.get("dueComplete")),
        members=to.member_names(data),
        labels=to.label_names(data),
        desc=to.text_of(data, "desc"),
        comment_count=int(badges.get("comments") or 0),
        # The CARD, not `data["checklists"]`: the progress lives in `badges`.
        checklist_summary=to.checklist_summary(data),
        attachment_count=int(badges.get("attachments") or 0),
        url=str(data.get("shortUrl") or data.get("url") or ""),
        modified=str(data.get("dateLastActivity") or ""),
        summary=to.card_summary(data),
    )


async def resolve_label(ctx, creds, board_id: str, reference: str) -> dict:
    """Resolve a label reference to an id.

    Trello labels may have an EMPTY name and be identified only by colour --
    they are created that way by default on every new board. So a colour word
    is accepted as a name here; `accounts.resolve_target` matches on the `name`
    field, which for an unnamed label is "", and would find nothing.
    """
    key, token = _creds_parts(creds)
    ref = (reference or "").strip()
    if not ref:
        return tc.fail(tc.TRELLO_VALIDATION_FAILED, "No label was named.")

    if to.looks_like_id(ref):
        return {"ok": True, "id": ref, "name": "", "resolved_by": "id"}

    out = await acct.list_board_children(ctx, key, token, board_id, "label")
    if not out.get("ok"):
        return out
    rows = out["data"]
    if not isinstance(rows, list):
        return tc.fail(tc.TRELLO_RESPONSE_UNEXPECTED)

    lowered = ref.lower()
    by_name, by_colour = [], []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        colour = str(item.get("color") or "")
        label_id = to.id_of(item)
        if name.strip().lower() == lowered:
            by_name.append((name or colour, label_id))
        elif colour.strip().lower() == lowered:
            by_colour.append((name or colour, label_id))
        elif lowered in name.strip().lower() and name:
            by_colour.append((name, label_id))

    matches = by_name or by_colour
    if not matches:
        available = ", ".join(
            (str(r.get("name") or "") or f"({r.get('color')})")
            for r in rows if isinstance(r, dict)) or "-"
        return tc.fail(
            tc.TRELLO_TARGET_NOT_FOUND,
            f"No label on this board matches '{reference}'. Available: {available}.")
    if len(matches) > 1:
        names = ", ".join(m[0] for m in matches)
        return tc.fail(tc.TRELLO_TARGET_AMBIGUOUS,
                       f"'{reference}' matches several labels: {names}.")
    name, label_id = matches[0]
    return {"ok": True, "id": label_id, "name": name, "resolved_by": "name"}


async def card_checklists(ctx, creds, card_id: str) -> dict:
    """Read a card's checklists WITH their items.

    One request serves both \"which checklist\" and \"which item\" questions, so
    the callers below do not each fetch the same thing. `checkItems=all` is
    required: without it Trello returns checklists as empty shells, and an item
    search over them finds nothing while reporting no error.
    """
    return await tc.request(
        ctx, "GET", f"cards/{card_id}/checklists", creds,
        params={"checkItems": "all", "checkItem_fields": "name,state,pos",
                "fields": "name,pos"})


def _pick_one(matches: list[tuple], kind: str, reference: str,
              available: list[str]) -> dict:
    """Turn a match list into an envelope: exactly one hit, or an explanation.

    Shared by the checklist and check-item resolvers because \"refuse to guess\"
    has to be phrased identically wherever it happens -- a tool that silently
    picks the first of two matches is how the wrong item gets ticked.
    """
    if not matches:
        listing = ", ".join(a for a in available[:12] if a) or "-"
        return tc.fail(
            tc.TRELLO_TARGET_NOT_FOUND,
            f"No {kind} matches '{reference}'. Available: {listing}.")
    if len(matches) > 1:
        names = ", ".join(str(m[1]) for m in matches[:6])
        return tc.fail(
            tc.TRELLO_TARGET_AMBIGUOUS,
            f"'{reference}' matches several {kind}s: {names}. "
            "Name one exactly, or pass its id.")
    return {"ok": True, "id": matches[0][0], "name": matches[0][1],
            "resolved_by": "name"}


async def resolve_checklist(ctx, creds, card_id: str, reference: str) -> dict:
    """Resolve a checklist on a card by name, or accept its id.

    An EMPTY reference is allowed and resolves to the card's only checklist.
    That is the common case -- most cards have one -- and demanding its name
    would make `add_check_item` require a lookup the user should not have to do.
    With several checklists present, emptiness is ambiguous and is refused.
    """
    ref = (reference or "").strip()
    if to.looks_like_id(ref):
        return {"ok": True, "id": ref, "name": "", "resolved_by": "id"}

    out = await card_checklists(ctx, creds, card_id)
    if not out.get("ok"):
        return out
    rows = [r for r in (out.get("data") or []) if isinstance(r, dict)]

    if not ref:
        if len(rows) == 1:
            return {"ok": True, "id": to.id_of(rows[0]),
                    "name": to.name_of(rows[0]), "resolved_by": "only-one"}
        if not rows:
            return tc.fail(tc.TRELLO_TARGET_NOT_FOUND,
                           "This card has no checklists.")
        names = ", ".join(to.name_of(r) for r in rows[:6])
        return tc.fail(
            tc.TRELLO_TARGET_AMBIGUOUS,
            f"This card has several checklists: {names}. Name which one.")

    lowered = ref.lower()
    exact = [(to.id_of(r), to.name_of(r)) for r in rows
             if to.name_of(r).strip().lower() == lowered]
    partial = [(to.id_of(r), to.name_of(r)) for r in rows
               if lowered in to.name_of(r).strip().lower()]
    return _pick_one(exact or partial, "checklist", reference,
                     [to.name_of(r) for r in rows])


async def resolve_check_item(ctx, creds, card_id: str, reference: str) -> dict:
    """Resolve a checklist ITEM on a card by its text.

    Returns the item id plus the checklist it belongs to, because Trello
    addresses an item under its CARD for updates but under its CHECKLIST for
    deletion -- callers need both. An exact match always wins over a substring
    one, so an item called \"Deploy\" stays reachable next to \"Deploy to prod\".
    """
    ref = (reference or "").strip()
    if not ref:
        return tc.fail(tc.TRELLO_VALIDATION_FAILED, "No checklist item was named.")

    out = await card_checklists(ctx, creds, card_id)
    if not out.get("ok"):
        return out

    lowered = ref.lower()
    exact: list[tuple] = []
    partial: list[tuple] = []
    available: list[str] = []
    for checklist in out.get("data") or []:
        if not isinstance(checklist, dict):
            continue
        for item in checklist.get("checkItems") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            available.append(name)
            entry = (to.id_of(item), name, to.id_of(checklist),
                     to.name_of(checklist))
            if name.strip().lower() == lowered:
                exact.append(entry)
            elif lowered in name.strip().lower():
                partial.append(entry)

    matches = exact or partial
    picked = _pick_one(matches, "checklist item", reference, available)
    if not picked.get("ok"):
        return picked
    winner = matches[0]
    picked["checklist_id"] = winner[2]
    picked["checklist_name"] = winner[3]
    return picked


async def resolve_attachment(ctx, creds, card_id: str, reference: str) -> dict:
    """Resolve an attachment on a card by name, or accept its id."""
    ref = (reference or "").strip()
    if not ref:
        return tc.fail(tc.TRELLO_VALIDATION_FAILED, "No attachment was named.")
    if to.looks_like_id(ref):
        return {"ok": True, "id": ref, "name": "", "resolved_by": "id"}

    out = await tc.request(ctx, "GET", f"cards/{card_id}/attachments", creds,
                           params={"fields": "name,url,isUpload"})
    if not out.get("ok"):
        return out
    rows = [r for r in (out.get("data") or []) if isinstance(r, dict)]

    lowered = ref.lower()
    exact = [(to.id_of(r), to.name_of(r)) for r in rows
             if to.name_of(r).strip().lower() == lowered]
    partial = [(to.id_of(r), to.name_of(r)) for r in rows
               if lowered in to.name_of(r).strip().lower()]
    picked = _pick_one(exact or partial, "attachment", reference,
                       [to.name_of(r) for r in rows])
    # Carry `isUpload` out with the match. The field was requested but dropped
    # here, so the caller could not tell a stored file from a link -- and the
    # deletion of the two is not the same act: one destroys the only copy.
    if picked.get("ok"):
        for r in rows:
            if to.id_of(r) == picked["id"]:
                picked["is_upload"] = bool(r.get("isUpload"))
                break
    return picked


async def board_custom_fields(ctx, creds, board_id: str) -> dict:
    """Read a board's custom field definitions.

    Trello returns an EMPTY ARRAY when the Custom Fields Power-Up is disabled --
    not an error. So "no fields" and "the feature is off" look identical here,
    and the callers say so rather than reporting an empty board as a fact.
    """
    return await tc.request(
        ctx, "GET", f"boards/{board_id}/customFields", creds)


async def resolve_custom_field(ctx, creds, board_id: str,
                               reference: str) -> dict:
    """Resolve a custom field by name, carrying its TYPE and options along.

    The type is not decoration: the write shape depends on it entirely
    ({"value": {"number": "42"}} vs {"idValue": "<option id>"}), so a resolver
    that returned only an id would force every caller to fetch the field again.
    """
    ref = (reference or "").strip()
    if not ref:
        return tc.fail(tc.TRELLO_VALIDATION_FAILED, "No custom field was named.")

    out = await board_custom_fields(ctx, creds, board_id)
    if not out.get("ok"):
        return out
    rows = [r for r in (out.get("data") or []) if isinstance(r, dict)]

    if not rows:
        return tc.fail(
            tc.TRELLO_TARGET_NOT_FOUND,
            "This board has no custom fields. Trello also returns an empty "
            "list when the Custom Fields Power-Up is switched off, so check "
            "that it is enabled on the board.")

    lowered = ref.lower()
    exact = [r for r in rows if to.name_of(r).strip().lower() == lowered]
    partial = [r for r in rows if lowered in to.name_of(r).strip().lower()]
    # An id may be pasted straight in.
    if to.looks_like_id(ref):
        exact = [r for r in rows if to.id_of(r) == ref] or exact

    matches = exact or partial
    if not matches:
        available = ", ".join(to.name_of(r) for r in rows) or "-"
        return tc.fail(
            tc.TRELLO_TARGET_NOT_FOUND,
            f"No custom field matches '{reference}'. Available: {available}.")
    if len(matches) > 1:
        names = ", ".join(to.name_of(r) for r in matches[:6])
        return tc.fail(
            tc.TRELLO_TARGET_AMBIGUOUS,
            f"'{reference}' matches several custom fields: {names}.")

    row = matches[0]
    raw = [o for o in (row.get("options") or []) if isinstance(o, dict)]
    # TWO views of the options, deliberately: `options_raw` keeps the dicts so a
    # caller can map a chosen text back to the option ID Trello wants, while
    # `options` is the plain texts for anything the user reads. Returning only
    # one of them forced every call site to re-derive the other.
    return {
        "ok": True,
        "id": to.id_of(row),
        "name": to.name_of(row),
        "field_type": str(row.get("type") or ""),
        "options_raw": raw,
        "options": [str(((o.get("value") or {}) or {}).get("text") or "")
                    for o in raw],
        "resolved_by": "name",
    }


def custom_field_body(field_type: str, value: str,
                      options: list[dict]) -> dict:
    """Build the PUT body for one custom field value.

    THE SHAPE IS TYPE-DEPENDENT, and every scalar goes in as a STRING even when
    it is a number or a boolean -- Trello's guide is explicit about that. A
    dropdown ("list") does not take a value at all: it takes the ID of one of
    its own options, which is why the options are passed in here.

    Returns {} when a dropdown value does not match any option, so the caller
    can refuse instead of silently writing nothing.
    """
    kind = (field_type or "").strip().lower()
    text = (value or "").strip()

    if kind == "list":
        for opt in options:
            opt_text = str(((opt.get("value") or {}) or {}).get("text") or "")
            if opt_text.strip().lower() == text.lower():
                return {"idValue": to.id_of(opt)}
        return {}

    if kind == "checkbox":
        # Anything a human would write for yes/no, normalised to Trello's
        # "true"/"false" strings.
        truthy = text.lower() in ("true", "yes", "1", "on", "checked", "да")
        return {"value": {"checked": "true" if truthy else "false"}}

    if kind == "number":
        return {"value": {"number": text}}

    if kind == "date":
        return {"value": {"date": text}}

    # text, and anything Trello adds later that behaves like text.
    return {"value": {"text": text}}


# The key inside `value` that each scalar field type is written through. Setting
# and CLEARING both go through this same key -- which is the whole point of
# having it in one place.
_CUSTOM_FIELD_VALUE_KEYS = {
    "checkbox": "checked",
    "number": "number",
    "date": "date",
    "text": "text",
}


def custom_field_clear_body(field_type: str) -> dict:
    """Build the PUT body that EMPTIES one custom field value.

    An empty `value` object -- {"value": {}} -- looks like the obvious way to say
    "no value" and is REJECTED LIVE with HTTP 400 "Invalid custom field item
    value". Found on a text and a date field after the dropdown case had already
    been fixed, which is exactly why the scalar case slipped: the dropdown fix
    proved clearing was type-dependent and then assumed one shape covered every
    scalar.

    Trello has no "empty" body. Clearing is setting the field's OWN key to an
    empty string, so it uses the same key as writing a value does -- the shared
    map above is what keeps those two from drifting apart. A dropdown has no
    `value` at all: it carries an option id, so it is emptied by unsetting that.
    """
    kind = (field_type or "").strip().lower()
    if kind == "list":
        return {"idValue": ""}
    # Unknown types fall back to `text`, matching how custom_field_body treats
    # anything Trello adds later that behaves like text.
    return {"value": {_CUSTOM_FIELD_VALUE_KEYS.get(kind, "text"): ""}}


async def resolve_workspace(ctx, creds, reference: str) -> dict:
    """Resolve a workspace (organization) by name or id.

    Matches on displayName first -- that is what the user sees in the UI -- then
    on the API's short `name`, which is a slug they may never have seen.
    """
    ref = (reference or "").strip()
    if not ref:
        return tc.fail(tc.TRELLO_VALIDATION_FAILED, "No workspace was named.")

    out = await tc.request(ctx, "GET", "members/me/organizations", creds,
                           params={"fields": "name,displayName,desc,website"})
    if not out.get("ok"):
        return out
    rows = [r for r in (out.get("data") or []) if isinstance(r, dict)]

    lowered = ref.lower()
    matches = [r for r in rows
               if str(r.get("displayName") or "").strip().lower() == lowered
               or str(r.get("name") or "").strip().lower() == lowered
               or to.id_of(r) == ref]
    if not matches:
        matches = [r for r in rows
                   if lowered in str(r.get("displayName") or "").lower()]
    if not matches:
        available = ", ".join(
            str(r.get("displayName") or r.get("name") or "?")
            for r in rows) or "-"
        return tc.fail(
            tc.TRELLO_TARGET_NOT_FOUND,
            f"No workspace matches '{reference}'. Available: {available}.")
    if len(matches) > 1:
        names = ", ".join(str(r.get("displayName") or "?")
                          for r in matches[:6])
        return tc.fail(tc.TRELLO_TARGET_AMBIGUOUS,
                       f"'{reference}' matches several workspaces: {names}.")

    row = matches[0]
    return {"ok": True, "id": to.id_of(row),
            "name": str(row.get("displayName") or row.get("name") or ""),
            "resolved_by": "name"}
