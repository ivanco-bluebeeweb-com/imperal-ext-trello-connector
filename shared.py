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
        checklist_summary=to.checklist_summary(data.get("checklists")),
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
