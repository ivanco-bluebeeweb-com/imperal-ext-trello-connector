"""Read tools: accounts, boards, lists, cards, comments, checklists, labels,
members, search, access report.

The spec is "readable first": reading has to be genuinely useful before any
write flow matters. So `get_card` returns the card's ACTUAL content -- its
description, its checklists, its members -- and `check_access` exists purely to
explain why something the user can see in their browser is not visible here.

Two Trello facts shape almost every handler in this file:

* COMMENTS ARE ACTIONS. There is no comment resource. A comment is an action of
  type `commentCard` whose text lives at `data.text`, read from
  `/cards/{id}/actions?filter=commentCard`.
* NESTED READS BEAT N+1. `/boards/{id}?lists=open&cards=open` returns the board
  WITH its lists and cards in one round trip. Where a handler needs several
  layers at once it asks for them together rather than looping.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acct
import shared
import trello_client as tc
import trello_objects as to
from app import chat
from models import (
    AccessReport,
    CheckAccessParams,
    GetCardParams,
    GetTokenLinkParams,
    ListAccountsParams,
    ListBoardsParams,
    ListCardsParams,
    ListChecklistsParams,
    ListCommentsParams,
    ListLabelsParams,
    ListListsParams,
    ListMembersParams,
    SearchParams,
    TrelloAccount,
    TrelloAccountList,
    TrelloBoard,
    TrelloBoardList,
    TrelloCard,
    TrelloCardList,
    TrelloChecklist,
    TrelloChecklistList,
    TokenLink,
    TrelloComment,
    TrelloCommentList,
    TrelloLabel,
    TrelloLabelList,
    TrelloList,
    TrelloListList,
    TrelloMember,
    TrelloMemberList,
    TrelloSearchHit,
    TrelloSearchHitList,
)

# Shared with handlers_write via `shared` so neither tool layer depends on the
# other. Re-exported under short private names to keep call sites readable.
ACCESS_NOTE = shared.ACCESS_NOTE
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_card_entity = shared.card_entity
_list_entity = shared.list_entity
_board_entity = shared.board_entity


@chat.function(
    "list_accounts",
    "List the connected Trello accounts and whether each credential pair still "
    "works.",
    action_type="read", chain_callable=True,
    data_model=TrelloAccount,
)
async def list_accounts(ctx, params: ListAccountsParams) -> ActionResult:
    """List connected Trello accounts and verify each pair still works."""
    entries = await acct.list_accounts(ctx, refresh=params.refresh)
    if not entries:
        return _error(
            "No Trello credentials are configured yet. Trello needs a PAIR: an "
            "API key from trello.com/apps/admin and a token from Trello's "
            "Allow prompt for that key. Paste them on the Connect screen.",
            tc.TRELLO_CREDENTIALS_MISSING)

    items = [
        TrelloAccount(
            slot=e.get("slot", 0),
            account_name=e.get("account_name", ""),
            username=e.get("username", ""),
            email=e.get("email", ""),
            board_count=e.get("board_count", 0),
            status=e.get("status", ""),
            detail=e.get("detail", ""),
        )
        for e in entries
    ]
    working = sum(1 for e in entries if e.get("status") == "ok")
    if working == len(items):
        note = f"{len(items)} Trello account(s) connected."
    else:
        note = (f"{working} of {len(items)} Trello account(s) working -- the "
                f"rest need their credentials re-pasted.")
    return ActionResult.success(
        TrelloAccountList(items=items, total=len(items)), note)


@chat.function(
    "list_boards",
    "List the Trello boards the connected accounts can reach, with their "
    "organization and whether they are closed.",
    action_type="read", chain_callable=True,
    data_model=TrelloBoard,
)
async def list_boards(ctx, params: ListBoardsParams) -> ActionResult:
    """List every board reachable by the configured credentials.

    The credentials check comes FIRST and is separate from the empty-list case.
    Without it, an unconfigured connector answers "no boards visible -- they may
    be closed, or the account may not be a member", which sends the user hunting
    through Trello permissions for a connector that was simply never connected.
    """
    _creds, missing = await shared.any_credentials(ctx)
    if missing:
        return missing

    boards = await acct.list_boards(ctx, refresh=params.refresh,
                                   include_closed=params.include_closed)
    if isinstance(boards, dict) and not boards.get("ok", True):
        return _from_envelope(boards)

    if not boards:
        return ActionResult.success(
            TrelloBoardList(items=[], total=0),
            f"No boards visible. {ACCESS_NOTE}")

    items = [
        TrelloBoard(
            id=b.get("id", ""),
            name=b.get("name", ""),
            closed=bool(b.get("closed")),
            url=b.get("url", ""),
            account_name=b.get("account_name", ""),
        )
        for b in boards
    ]
    return ActionResult.success(
        TrelloBoardList(items=items, total=len(items)),
        f"{len(items)} board(s) reachable.")


@chat.function(
    "list_lists",
    "List the lists (columns) of a Trello board, in board order.",
    action_type="read", chain_callable=True,
    data_model=TrelloList,
)
async def list_lists(ctx, params: ListListsParams) -> ActionResult:
    """List a board's columns."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    query = {"fields": to.LIST_FIELDS}
    # Trello's `filter` on this route takes all|closed|none|open -- not a
    # boolean. Sending `closed=false` is silently ignored, which is how a
    # request for open lists comes back including the archived ones.
    query["filter"] = "all" if params.include_closed else "open"

    out = await tc.request(ctx, "GET", f"boards/{board['id']}/lists", creds,
                           params=query)
    if not out.get("ok"):
        return _from_envelope(out)

    rows = out.get("data") or []
    if not rows:
        return ActionResult.success(
            TrelloListList(items=[], total=0),
            f"Board '{board.get('name', '')}' has no lists yet.")

    items = [_list_entity(r) for r in rows]
    return ActionResult.success(
        TrelloListList(items=items, total=len(items)),
        f"{len(items)} list(s) on '{board.get('name', '')}'.")


@chat.function(
    "list_cards",
    "List cards on a Trello board or in one of its lists, with due dates, "
    "members and labels.",
    action_type="read", chain_callable=True,
    data_model=TrelloCard,
)
async def list_cards(ctx, params: ListCardsParams) -> ActionResult:
    """List cards on a board, or just those in one named list."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    path = f"boards/{board['id']}/cards"
    where = f"board '{board.get('name', '')}'"

    if params.list_name:
        target = await shared.resolve_list(ctx, creds, board["id"], params.list_name)
        if not target.get("ok"):
            return _from_envelope(target)
        path = f"lists/{target['id']}/cards"
        where = f"list '{target.get('name') or params.list_name}'"

    query = {
        "fields": to.CARD_FIELDS,
        # Names, not ids: a card that shows `idMembers: [...]` forces a second
        # lookup per card just to say who is on it.
        "members": "true",
        "member_fields": to.MEMBER_FIELDS,
        "labels": "true",
    }
    if params.include_closed:
        query["filter"] = "all"

    out = await tc.paginate(ctx, path, creds, params=query, limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    cards = out["results"]
    if not cards:
        return ActionResult.success(
            TrelloCardList(items=[], total=0),
            f"No cards in {where}. {ACCESS_NOTE}")

    items = [_card_entity(c) for c in cards]
    more = " (more available)" if out.get("maybe_more") else ""
    return ActionResult.success(
        TrelloCardList(items=items, total=len(items)),
        f"{len(items)} card(s) in {where}{more}.")


@chat.function(
    "get_card",
    "Read one Trello card in full: description, due date, members, labels, "
    "checklists and which list it sits in.",
    action_type="read", chain_callable=True,
    data_model=TrelloCard,
)
async def get_card(ctx, params: GetCardParams) -> ActionResult:
    """Read a single card with its nested content."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await shared.resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    query = {
        "fields": to.CARD_FIELDS,
        "members": "true",
        "member_fields": to.MEMBER_FIELDS,
        "labels": "true",
        # One round trip instead of three: the checklists and their items come
        # back nested rather than needing a call per checklist.
        "checklists": "all",
        "checklist_fields": "name",
        "list": "true",
    }
    out = await tc.request(ctx, "GET", f"cards/{card['id']}", creds,
                           params=query)
    if not out.get("ok"):
        return _from_envelope(out)

    raw = out.get("data") or {}
    entity = _card_entity(raw)
    # The nested `list` object is the only place the LIST NAME is available
    # without a second call, so it is folded into the card the user sees.
    entity.list_name = to.name_of(raw.get("list")) or entity.list_name

    checklists = to.checklist_summary(raw.get("checklists"))
    summary = f"Card '{entity.name}'"
    if entity.list_name:
        summary += f" in list '{entity.list_name}'"
    if checklists:
        summary += f"; {checklists}"
    return ActionResult.success(entity, summary + ".")


@chat.function(
    "list_comments",
    "Read the comments on a Trello card, newest first.",
    action_type="read", chain_callable=True,
    data_model=TrelloComment,
)
async def list_comments(ctx, params: ListCommentsParams) -> ActionResult:
    """Read a card's comments.

    Trello has no comment resource: a comment is an ACTION of type
    `commentCard`, and its text lives at `data.text`. Filtering server-side
    keeps board-activity noise (moves, label changes) out of the result.
    """
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await shared.resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    out = await tc.paginate(
        ctx, f"cards/{card['id']}/actions", creds,
        params={"filter": "commentCard"}, limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    actions = out["results"]
    if not actions:
        return ActionResult.success(
            TrelloCommentList(items=[], total=0),
            f"No comments on '{card.get('name') or params.card}' yet.")

    items = [
        TrelloComment(
            id=to.id_of(a),
            author=to.comment_author(a),
            text=to.comment_text(a),
            created=to.created_at(a),
        )
        for a in actions
    ]
    more = " (more available)" if out.get("maybe_more") else ""
    return ActionResult.success(
        TrelloCommentList(items=items, total=len(items)),
        f"{len(items)} comment(s) on '{card.get('name') or params.card}'{more}.")


@chat.function(
    "list_checklists",
    "List the checklists on a Trello card with their items and which are done.",
    action_type="read", chain_callable=True,
    data_model=TrelloChecklist,
)
async def list_checklists(ctx, params: ListChecklistsParams) -> ActionResult:
    """List a card's checklists and their items."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    card = await shared.resolve_card(ctx, creds, board["id"], params.card)
    if not card.get("ok"):
        return _from_envelope(card)

    out = await tc.request(ctx, "GET", f"cards/{card['id']}/checklists", creds,
                           params={"fields": "name,pos"})
    if not out.get("ok"):
        return _from_envelope(out)

    rows = out.get("data") or []
    if not rows:
        return ActionResult.success(
            TrelloChecklistList(items=[], total=0),
            f"No checklists on '{card.get('name') or params.card}'.")

    items = []
    for r in rows:
        done, total = to.checkitem_counts(r)
        items.append(TrelloChecklist(
            id=to.id_of(r),
            name=to.name_of(r),
            done_count=done,
            total_count=total,
            items=to.checkitem_lines(r),
        ))
    return ActionResult.success(
        TrelloChecklistList(items=items, total=len(items)),
        f"{len(items)} checklist(s) on '{card.get('name') or params.card}'.")


@chat.function(
    "list_labels",
    "List the labels defined on a Trello board -- their names and colours.",
    action_type="read", chain_callable=True,
    data_model=TrelloLabel,
)
async def list_labels(ctx, params: ListLabelsParams) -> ActionResult:
    """List a board's labels."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    out = await tc.request(ctx, "GET", f"boards/{board['id']}/labels", creds,
                           params={"fields": "name,color"})
    if not out.get("ok"):
        return _from_envelope(out)

    rows = out.get("data") or []
    if not rows:
        return ActionResult.success(
            TrelloLabelList(items=[], total=0),
            f"Board '{board.get('name', '')}' has no labels.")

    items = [
        TrelloLabel(
            id=to.id_of(r),
            name=to.name_of(r),
            color=str(r.get("color") or ""),
        )
        for r in rows
    ]
    # A Trello board ships with six coloured labels that have NO name. Saying
    # so prevents "the list looks broken" when most rows show a colour only.
    unnamed = sum(1 for i in items if not i.name)
    note = f"{len(items)} label(s) on '{board.get('name', '')}'"
    if unnamed:
        note += f"; {unnamed} unnamed (colour only)"
    return ActionResult.success(
        TrelloLabelList(items=items, total=len(items)), note + ".")


@chat.function(
    "list_members",
    "List the people on a Trello board, with their usernames.",
    action_type="read", chain_callable=True,
    data_model=TrelloMember,
)
async def list_members(ctx, params: ListMembersParams) -> ActionResult:
    """List a board's members."""
    creds, board, err = await _resolve(ctx, params.board)
    if err:
        return err

    out = await tc.request(ctx, "GET", f"boards/{board['id']}/members", creds,
                           params={"fields": to.MEMBER_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    rows = out.get("data") or []
    if not rows:
        return ActionResult.success(
            TrelloMemberList(items=[], total=0),
            f"No members visible on '{board.get('name', '')}'. {ACCESS_NOTE}")

    items = [
        TrelloMember(
            id=to.id_of(r),
            name=to.name_of(r),
            username=str(r.get("username") or ""),
        )
        for r in rows
    ]
    return ActionResult.success(
        TrelloMemberList(items=items, total=len(items)),
        f"{len(items)} member(s) on '{board.get('name', '')}'.")


@chat.function(
    "search",
    "Search Trello for cards and boards by text.",
    action_type="read", chain_callable=True,
    data_model=TrelloSearchHit,
)
async def search(ctx, params: SearchParams) -> ActionResult:
    """Search across everything the credentials can see.

    `/search` needs a non-empty `query`; an empty one is a 400 that reads like a
    server fault, so it is refused here with an actionable message instead.
    """
    if not params.query.strip():
        return _error(
            "Give something to search for -- Trello has no 'list everything' "
            "search. Use list_cards to browse a board instead.",
            tc.TRELLO_VALIDATION_FAILED)

    creds, err = await shared.any_credentials(ctx)
    if err:
        return err

    kind = (params.kind or "").strip().lower()
    model_types = {"card": "cards", "board": "boards"}.get(kind, "cards,boards")

    query = {
        "query": params.query.strip(),
        "modelTypes": model_types,
        "cards_limit": min(params.limit, 100),
        "boards_limit": min(params.limit, 100),
        "card_fields": to.CARD_FIELDS,
        "board_fields": to.BOARD_FIELDS,
        "partial": "true" if params.partial else "false",
    }
    out = await tc.request(ctx, "GET", "search", creds, params=query)
    if not out.get("ok"):
        return _from_envelope(out)

    raw = out.get("data") or {}
    hits: list[TrelloSearchHit] = []
    for c in (raw.get("cards") or []):
        hits.append(TrelloSearchHit(
            id=to.id_of(c), name=to.name_of(c), kind="card",
            url=str(c.get("shortUrl") or c.get("url") or ""),
        ))
    for b in (raw.get("boards") or []):
        hits.append(TrelloSearchHit(
            id=to.id_of(b), name=to.name_of(b), kind="board",
            url=str(b.get("url") or ""),
        ))

    if not hits:
        # Trello's search matches whole words from the START of a word, so a
        # fragment finds nothing unless `partial` is on. Saying that turns a
        # dead end into a next step.
        extra = ""
        if not params.partial:
            extra = (" Trello matches from the start of words -- set partial "
                     "to true to match fragments.")
        return ActionResult.success(
            TrelloSearchHitList(items=[], total=0),
            f"Nothing matched '{params.query}'.{extra}")

    return ActionResult.success(
        TrelloSearchHitList(items=hits, total=len(hits)),
        f"{len(hits)} match(es) for '{params.query}'.")


@chat.function(
    "get_token_link",
    "Turn your Trello API key into a ready-made authorize link, so you can get "
    "a token without hunting for anything on Trello's page.",
    action_type="read", chain_callable=True,
    data_model=TokenLink,
)
async def get_token_link(ctx, params: GetTokenLinkParams) -> ActionResult:
    """Hand back the link that produces a token for a given key.

    Why this exists. The Connect screen asks for a token, but Trello's admin
    page has no visible control that issues one -- the manual link is buried in
    a paragraph below the key, and Atlassian moves it. So the token field was a
    field with no reachable source: the user is told to paste something they
    cannot obtain. Explaining the page in prose failed twice, because prose
    about someone else's UI goes stale the moment they redesign it.

    A link cannot go stale the same way: it is Trello's documented authorize
    endpoint, it needs nothing but the key, and clicking Allow is the ONLY
    thing that mints a token. This makes the missing half obtainable in one
    step instead of describing where to click.

    The key is verified first. A dead key still renders an Allow prompt for
    nothing useful, so catching it here saves the user a pointless round trip.
    An unavailable check is reported as unverified -- never as a dead key,
    because not knowing is not evidence.
    """
    key = (params.key or "").strip()

    if not key:
        return _error(
            "Paste your Trello API key and I will build the link that gives "
            "you a token for it. The key is on the API Key tab of your Power-Up "
            "at trello.com/apps/admin.",
            tc.TRELLO_KEY_MISSING)

    # A wrong-shaped key cannot produce a usable link, and shipping one would
    # send the user to an Allow prompt that fails after they grant access.
    if len(key) != 32 or not all(c in "0123456789abcdefABCDEF" for c in key):
        note, _ = acct._shape_note(key, "", tc.TRELLO_KEY_REJECTED)
        return _error(note, tc.TRELLO_KEY_REJECTED)

    live = await acct.key_is_live(ctx, key)
    url = acct._authorize_url(key)

    if live is False:
        return _error(
            "Trello does not recognise this key, so a token for it cannot be "
            "created. Keys from the retired trello.com/app-key page are not "
            "tied to a Power-Up and stop working. Generate a new key at "
            "trello.com/apps/admin -- open (or create) a Power-Up, API Key tab, "
            "'Generate a new API Key' -- then ask me for the link again.",
            tc.TRELLO_KEY_REJECTED)

    status = "verified -- Trello accepts this key" if live else (
        "not verified -- the check could not be made, the link is still correct")

    return ActionResult.success(
        TokenLink(
            title="Your Trello token link",
            subtitle="Open it, click Allow, then paste the token you get",
            authorize_url=url,
            key_status=status,
            expiration="never -- the token will not silently die",
            scope="read and write -- reading boards and editing cards",
            url=url,
            next_step=(
                "Open the link and click Allow. Trello then shows the token -- "
                "paste it together with this same key to connect. The Secret "
                "under your key is NOT a token and cannot be used here."),
        ),
        "Here is your authorize link -- click Allow and Trello gives you the "
        "token.")


@chat.function(
    "check_access",
    "Report what this connector can currently reach in Trello, and explain "
    "anything missing.",
    action_type="read", chain_callable=True,
    data_model=AccessReport,
)
async def check_access(ctx, params: CheckAccessParams) -> ActionResult:
    """Explain the connection: which accounts work, what they reach, and why
    something visible in the browser might not be visible here.

    This exists because the commonest Trello confusion is not an error at all:
    the token belongs to a different Trello account than the browser session, so
    everything looks connected and nothing familiar shows up.
    """
    entries = await acct.list_accounts(ctx, refresh=True)
    if not entries:
        return ActionResult.success(
            AccessReport(
                accounts_configured=0,
                accounts_working=0,
                boards_reachable=0,
                detail="No credentials configured.",
                next_step=(
                    "Paste an API key and token on the Connect screen. The key "
                    "comes from the API Key tab of your Power-Up at "
                    "trello.com/apps/admin; the token comes from Trello's "
                    "Allow prompt for that same key."),
            ),
            "Trello is not connected yet.")

    working = [e for e in entries if e.get("status") == "ok"]
    boards = await acct.list_boards(ctx, refresh=True)
    board_count = len(boards) if isinstance(boards, list) else 0

    names = ", ".join(
        f"{e.get('account_name') or 'unnamed'}"
        f"{' (@' + e['username'] + ')' if e.get('username') else ''}"
        for e in working) or "none"

    if not working:
        detail = ("Credentials are stored but none of them work right now. "
                  "Trello answers 401 both for a revoked token and for one "
                  "pasted incompletely.")
        next_step = ("Re-issue the token from the 'Token' link beside your API "
                     "key and paste the pair again.")
    elif board_count == 0:
        detail = (f"Connected as {names}, but no boards are visible. "
                  f"{ACCESS_NOTE}")
        next_step = ("Check the account shown above is the one that owns your "
                     "boards -- a token from a second Trello account is the "
                     "usual cause.")
    else:
        detail = f"Connected as {names}; {board_count} board(s) reachable."
        next_step = ""

    return ActionResult.success(
        AccessReport(
            accounts_configured=len(entries),
            accounts_working=len(working),
            boards_reachable=board_count,
            detail=detail,
            next_step=next_step,
        ),
        detail)
