"""Account resolution: credential pairs -> accounts -> boards, and name -> id.

Two jobs, both about never making the user handle a 24-character hex id.

1. A Trello credential is a PAIR: an API key that identifies the app and a
   token that identifies the user. So `split_pairs` parses "key:token" lines,
   and every downstream call passes both halves -- there is no single-string
   form of Trello access. This is the structural difference from the Asana
   connector (one token per account) and the Notion connector (one token per
   workspace).

2. Trello has no workspace layer that a token is scoped to: a token reaches
   every board its owner can see, across every organization. So the addressable
   unit here is the BOARD, discovered from `/members/me/boards`, and
   `resolve_board` matches a name against that list. `resolve_target` refuses to
   guess when several things match, because silently picking one and then
   WRITING to it is the expensive kind of wrong.

Credentials live only in the Vault secret. The store caches account and board
NAMES and IDS so panels render without hitting Trello; never a key or a token.
"""

from __future__ import annotations

import trello_client as tc
import trello_objects as to

ACCOUNTS_COLLECTION = "accounts"

SECRET_NAME = "trello_credentials"

# Mirrors the platform default for a user-scoped secret.
MAX_SECRET_BYTES = 4096


def split_pairs(raw: str) -> list[tuple[str, str]]:
    """One 'key:token' pair per line, blanks dropped, duplicates removed.

    Only the FIRST colon splits: a Trello key and token are hex strings that
    contain no colon, but splitting on all of them would silently truncate a
    token if Trello ever widened its alphabet. Lines missing either half are
    dropped rather than half-accepted -- a key with no token cannot authorise
    anything, and keeping it would make a broken line look like an account.

    Blank lines and stray whitespace are tolerated: the user is pasting into a
    textarea, and a trailing newline should not create a phantom account.
    """
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        key, sep, token = candidate.partition(":")
        key, token = key.strip(), token.strip()
        if not sep or not key or not token:
            continue
        if (key, token) in seen:
            continue
        seen.add((key, token))
        pairs.append((key, token))
    return pairs


def join_pairs(pairs: list[tuple[str, str]]) -> str:
    """Render pairs back into the stored one-per-line form."""
    return "\n".join(f"{key}:{token}" for key, token in pairs)


async def read_pairs(ctx) -> dict:
    """Read credential pairs, distinguishing "none set" from "cannot read".

    Returning an empty list for an unreadable secret would give the user setup
    advice if the store is simply unavailable, and it hides a real outage
    behind a setup message. Both states travel with their own code instead.
    """
    try:
        raw = await ctx.secrets.get(SECRET_NAME)
    except Exception as exc:
        # No plaintext can appear here: only the exception TYPE is recorded.
        return tc.fail(tc.TRELLO_SECRET_UNAVAILABLE,
                       f"{tc.message_for(tc.TRELLO_SECRET_UNAVAILABLE)} "
                       f"({type(exc).__name__})")
    return {"ok": True, "pairs": split_pairs(raw or "")}


async def load_pairs(ctx) -> list[tuple[str, str]]:
    """Pairs only, for callers that treat unreadable as not-configured.

    Anything that needs to explain WHY there are no credentials should call
    `read_pairs` instead.
    """
    out = await read_pairs(ctx)
    return out.get("pairs", []) if out.get("ok") else []


async def describe_pair(ctx, key: str, token: str) -> dict:
    """Identify the account behind one pair via `/members/me`.

    `fields` is requested explicitly: the default member object is large and
    carries dozens of preference sub-objects that would be fetched and thrown
    away on every panel render.

    Returns a plain dict either way -- a bad pair yields a describable entry
    (with its structured code) instead of an exception, so ONE broken
    credential cannot blank out the whole account list.
    """
    out = await tc.request(
        ctx, "GET", "members/me", (key, token),
        params={"fields": "id,fullName,username,email"})
    if not out.get("ok"):
        return {"ok": False, "code": out.get("code", ""),
                "error": out.get("error", "")}

    member = out["data"]
    if not isinstance(member, dict):
        return {"ok": False, "code": tc.TRELLO_RESPONSE_UNEXPECTED,
                "error": tc.message_for(tc.TRELLO_RESPONSE_UNEXPECTED)}

    return {
        "ok": True,
        "member_id": to.id_of(member),
        "member_name": to.name_of(member) or "Trello user",
        "username": str(member.get("username") or ""),
        "email": str(member.get("email") or ""),
    }


async def fetch_boards(ctx, key: str, token: str) -> dict:
    """The boards one credential pair can reach.

    `filter=open` on purpose: closed (archived) boards are not somewhere a user
    means when they name a board, and including them makes the picker a graveyard.
    A tool that specifically wants archived boards asks for it.
    """
    out = await tc.request(
        ctx, "GET", "members/me/boards", (key, token),
        params={"filter": "open", "fields": "id,name,closed,idOrganization,url"})
    if not out.get("ok"):
        return out

    rows = out["data"]
    if not isinstance(rows, list):
        return tc.fail(tc.TRELLO_RESPONSE_UNEXPECTED)

    boards = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        boards.append({
            "id": to.id_of(item),
            "name": to.name_of(item),
            "closed": bool(item.get("closed")),
            "organization_id": str(item.get("idOrganization") or ""),
            "url": str(item.get("url") or ""),
        })
    return {"ok": True, "boards": boards}


def _shape_complaint(key: str, token: str):
    """Catch the two paste mistakes the admin page invites, before spending a call.

    `trello.com/apps/admin` shows a 32-hex **API Key** and a 64-hex **Secret**
    side by side, and the Secret is NOT the token -- the token comes from a
    separate authorise prompt. So two wrong pastes are common and both end in
    Trello's flat "invalid key", which sends the user back to re-copy a key that
    was never the problem:

      * the Secret pasted into the key field (64 hex where 32 belongs), and
      * the halves swapped (long value in key, short one in token).

    Both are decidable from the strings alone, so they are named here rather
    than after a round trip. Deliberately CONSERVATIVE: it only complains when
    the shape is unambiguous, because Trello has changed credential formats
    before and a strict validator that outlives its assumptions would reject
    working credentials. Anything it is not sure about goes to Trello, whose
    verdict remains the authority.
    """
    key_hex = all(c in "0123456789abcdefABCDEF" for c in key)
    token_hex = all(c in "0123456789abcdefABCDEF" for c in token)

    # Halves swapped: the key field holds a token-length value AND the token
    # field holds a key-length one. Both conditions together, so a single
    # oddly-sized value is not mistaken for a swap.
    if len(key) == 64 and len(token) == 32 and key_hex and token_hex:
        return tc.fail(
            tc.TRELLO_KEY_REJECTED,
            "These look swapped: the API key is the SHORTER value (32 "
            "characters) and it belongs in the key field. Put the longer value "
            "in the token field. If you have no token yet, open the Allow "
            "prompt for the 32-character value: "
            + _authorize_url(token))

    # The Secret pasted as the key. The Secret is for OAuth signing and will
    # never authorise a REST call, so no amount of retrying helps.
    if len(key) == 64 and key_hex:
        return tc.fail(
            tc.TRELLO_KEY_REJECTED,
            "That is 64 characters long, which is the Secret shown next to the "
            "API key -- not the key itself. The API key is the 32-character "
            "value on the API Key tab of your Power-Up at "
            "trello.com/apps/admin. The Secret is never used here.")

    return None


async def key_is_live(ctx, key: str) -> bool | None:
    """Ask Trello whether the KEY ALONE is real, independent of any token.

    Why this exists. `api.trello.com` answers a bare `invalid key` for every
    failing combination -- verified against the live API, including a request
    with a GOOD key and a bad token. So the REST error cannot tell "your key is
    dead" from "your key is fine, the token is wrong", and advice based on it
    talks people into throwing away a working key.

    The authorize PAGE takes the key on its own -- there is no token in the
    request -- so its verdict is about the key and nothing else:
      * HTTP 200 -> Trello renders the Allow prompt, so the key is LIVE
      * HTTP 404 -> Trello does not know this key, so it is DEAD

    Returns True/False, or None when the check itself could not be made. None
    is deliberately NOT False: an unreachable page or an unexpected status is
    ignorance, not evidence, and must never be reported as a dead key.
    """
    url = ("https://trello.com/1/authorize?expiration=1day&scope=read"
           f"&response_type=token&key={key}")
    try:
        resp = await ctx.http.get(url, timeout=15,
                                  headers={"Accept": "text/html"})
    except Exception:
        # The pair verdict still stands on its own; this is only enrichment.
        return None

    status = getattr(resp, "status_code", 0)
    if status == 200:
        return True
    if status == 404:
        return False
    return None


def _authorize_url(key: str) -> str:
    """Build the ready-made authorize link for one key.

    Why a link and not instructions. The admin page has
    no button beside the key -- the manual link is buried in a paragraph
    below it, and Atlassian rearranges that page. Describing furniture that
    moves is how the connector ended up sending people to a control that was
    not there, and a user who cannot find it concludes their page is broken.

    This URL needs nothing but the key: opening it renders Trello's Allow
    prompt and hands back a token bound to THIS key. `expiration=never` because
    a stored credential that silently dies in 30 days is a support ticket, and
    `scope=read,write` because a read-only token would connect happily and then
    fail on the first card edit -- the worst moment to discover a scope.
    """
    return ("https://trello.com/1/authorize?expiration=never"
            "&scope=read,write&response_type=token"
            "&name=Imperal%20Trello%20Connector"
            f"&key={key}")


def _shape_note(key: str, token: str, code: str = "",
                key_live: bool | None = None) -> tuple[str, bool]:
    """Describe what ARRIVED, so a rejection is diagnosable without a guess.

    Returns (note, supersedes). `supersedes` is True when the note has
    established the PASTE is fine -- there the stock advice ("copy the key from
    the API Key tab") contradicts the conclusion, so the caller drops it.

    WHICH HALF Trello refused decides the advice, and `code` carries that.
    Verified against the live API: Trello validates the KEY FIRST and answers
    `invalid key` for every bad combination -- including a request with no key
    at all but a bad token. Atlassian documents the other side: a revoked TOKEN
    answers `invalid token`. So `invalid key` on a correctly-shaped key means
    the KEY itself is not accepted, and advising the user to regenerate their
    token sends them to fix the half that still works.

    Only LENGTH and CHARACTER CLASS are reported -- never the values, not even a
    prefix. A credential must not become readable through an error message, and
    a length is enough to identify every realistic paste mistake.
    """
    key_hex = all(c in "0123456789abcdefABCDEF" for c in key)
    token_hex = all(c in "0123456789abcdefABCDEF" for c in token)

    parts = [f"What arrived: key {len(key)} characters"
             f"{'' if key_hex else ' (contains non-hex characters)'}, "
             f"token {len(token)} characters"
             f"{'' if token_hex else ' (contains non-hex characters)'}."]

    # A Trello API key is 32 hex characters. Any other length cannot be one, so
    # say so plainly -- that is the difference between "fix your paste" and
    # "your credential was revoked".
    if len(key) != 32:
        parts.append(
            f"A Trello API key is exactly 32 characters, so a {len(key)}-"
            "character value is not one -- check you copied the API Key field "
            "itself and not a surrounding label, URL or partial selection.")
        return " ".join(parts), False
    if not key_hex:
        parts.append(
            "The key is the right length but contains characters outside 0-9 "
            "and a-f, which a Trello key never does -- something extra was "
            "picked up in the copy.")
        return " ".join(parts), False

    # The key is exactly the right shape, so the paste is not the problem. What
    # to do next depends on which half Trello refused -- and it says so.
    parts.append(
        "The key is exactly the right shape, so the paste is fine -- this "
        "is a credential problem, not a typing one.")

    if code == tc.TRELLO_KEY_REJECTED and key_live is False:
        # The authorize page does not know this key, so it really is dead. The
        # commonest cause is a key from the retired trello.com/app-key page:
        # not tied to a Power-Up, cannot be migrated, and no token rescues one.
        parts.append(
            "Trello does not recognise this KEY at all -- checked against the "
            "authorize page, which validates the key on its own. A well-formed "
            "key gets refused when it came from the retired "
            "trello.com/app-key page: those keys are not tied to a Power-Up and "
            "cannot be migrated, so a new token will not help. Generate a NEW "
            "key at trello.com/apps/admin -- open (or create) a Power-Up, API "
            "Key tab, 'Generate a new API Key' -- then create a token from the "
            "'Token' link in the paragraph BELOW the key -- the one about "
            "generating a token manually -- and paste both.")
    elif code == tc.TRELLO_KEY_REJECTED and key_live is True:
        # The decisive case. Trello's REST error says "invalid key" even when
        # the key is perfectly good and only the TOKEN is bad -- verified
        # against the live API. Taking that wording at face value tells people
        # to throw away a working key, so the authorize page is asked directly
        # and its answer overrides the misleading REST text.
        parts.append(
            "Your KEY IS VALID -- Trello's authorize page accepts it, even "
            "though the REST error says 'invalid key'. That wording is what "
            "Trello returns for the whole pair, so the TOKEN is the half at "
            "fault: it was never created, was revoked, has expired, or belongs "
            "to a different key. Keep this key. Note the SECRET shown under it "
            "is NOT the token: it signs OAuth and can never authorise a REST "
            "call. Open this link to create a real token for THIS key, click "
            "Allow, and paste what Trello shows: "
            + _authorize_url(key))
    elif code == tc.TRELLO_KEY_REJECTED:
        # key_live is None: the check could not be made. Ignorance is not
        # evidence, so neither half is blamed -- both possibilities are named.
        parts.append(
            "Trello returns 'invalid key' for the pair as a whole, so either "
            "half could be at fault. Check the token first -- it expires and is "
            "revocable, and it only works with the key that created it. Get a "
            "fresh one for THIS key here before replacing the key itself: "
            + _authorize_url(key))
    else:
        parts.append(
            "Either the token was revoked or expired, or it was generated with "
            "a DIFFERENT key (a token only works with the key that created "
            "it). Open this link to get a fresh token for THIS key, then "
            "reconnect: " + _authorize_url(key))
    return " ".join(parts), True


async def add_pair(ctx, key: str, token: str) -> dict:
    """Validate a credential pair against Trello, then store it.

    Deliberately verify-BEFORE-write. A store-then-check flow is what makes a
    bad paste feel like a silent failure: the value lands, the panel clears, and
    the user only learns it was wrong the next time they ask for something. Here
    an unusable pair is rejected with Trello's own reason and NOTHING is
    written, so the app never holds a credential it knows is broken.

    Appends rather than replaces: a user may hold separate access for a personal
    and a client account, and connecting the second must not silently destroy
    the first.
    """
    key = (key or "").strip()
    token = (token or "").strip()

    # Which half is missing changes what the user has to go do, so the two are
    # reported separately instead of as one "credentials missing".
    if not key and not token:
        return tc.fail(tc.TRELLO_CREDENTIALS_MISSING)
    if not key:
        return tc.fail(tc.TRELLO_KEY_MISSING)
    if not token:
        return tc.fail(tc.TRELLO_TOKEN_MISSING)

    shape = _shape_complaint(key, token)
    if shape:
        return shape

    # Trello's own verdict first -- identifies the account as a side effect.
    info = await describe_pair(ctx, key, token)
    if not info.get("ok"):
        # Trello's rejection names WHICH half it disliked but says nothing about
        # what actually arrived, so the same sentence comes back whether the key
        # was 8 characters, 40, or a perfect 32 that has since been revoked.
        # That is a dead end: re-copying a correct key changes nothing and the
        # user has no way to tell which case they are in. Describing the
        # OBSERVED SHAPE turns the second attempt into a diagnosis.
        # `invalid key` is Trello's answer for a bad PAIR -- a good key with a
        # bad token gets it too. So when the key is well formed, ask the
        # authorize page (which takes the key alone) which half is really
        # wrong, rather than repeating wording that blames the key.
        code = info.get("code", "")
        key_live = None
        if code == tc.TRELLO_KEY_REJECTED and len(key) == 32:
            key_live = await key_is_live(ctx, key)
        note, supersedes = _shape_note(key, token, code, key_live)
        stock = info.get("error") or tc.message_for(tc.TRELLO_TOKEN_REJECTED)
        # When the note has established the paste is fine, the stock line
        # ("copy the key from the API Key tab") contradicts it -- so it goes.
        return tc.fail(info.get("code") or tc.TRELLO_TOKEN_REJECTED,
                       note if supersedes else f"{stock} {note}")

    existing = await read_pairs(ctx)
    if not existing.get("ok"):
        return existing
    pairs = existing["pairs"]

    boards_out = await fetch_boards(ctx, key, token)
    board_names = [b["name"] for b in boards_out.get("boards", []) if b.get("name")]

    if (key, token) in pairs:
        return {"ok": True, "already_connected": True,
                "member_name": info.get("member_name", ""),
                "username": info.get("username", ""),
                "email": info.get("email", ""),
                "board_names": board_names,
                "count": len(pairs)}

    combined = pairs + [(key, token)]
    payload = join_pairs(combined)
    if len(payload.encode("utf-8")) > MAX_SECRET_BYTES:
        return tc.fail(
            tc.TRELLO_VALIDATION_FAILED,
            f"Adding this credential would exceed the {MAX_SECRET_BYTES}-byte "
            f"limit for the stored value ({len(pairs)} already saved). Remove "
            "an unused credential in the Secrets manager first.")

    try:
        await ctx.secrets.set(SECRET_NAME, payload)
    except Exception as exc:
        # Only the exception TYPE -- never the value -- is surfaced.
        return tc.fail(tc.TRELLO_SECRET_WRITE_FAILED,
                       f"{tc.message_for(tc.TRELLO_SECRET_WRITE_FAILED)} "
                       f"({type(exc).__name__})")

    # Drop the cached account list so the new one appears immediately instead
    # of after the cache happens to expire.
    await _forget_cache(ctx)

    return {"ok": True, "already_connected": False,
            "member_name": info.get("member_name", ""),
            "username": info.get("username", ""),
            "email": info.get("email", ""),
            "board_names": board_names,
            "count": len(combined)}


async def _forget_cache(ctx) -> None:
    """Clear cached account rows; failure here is not worth failing a save."""
    try:
        page = await ctx.store.query(ACCOUNTS_COLLECTION, limit=100)
        for doc in page.data:
            await ctx.store.delete(ACCOUNTS_COLLECTION, doc.id)
    except Exception:
        pass


async def _cache_account(ctx, entry: dict) -> None:
    """Upsert one account record. Cache failures are never fatal."""
    try:
        page = await ctx.store.query(ACCOUNTS_COLLECTION,
                                     where={"slot": entry["slot"]}, limit=1)
        if page.data:
            await ctx.store.update(ACCOUNTS_COLLECTION, page.data[0].id, entry)
        else:
            await ctx.store.create(ACCOUNTS_COLLECTION, entry)
    except Exception:
        pass


async def list_accounts(ctx, *, refresh: bool = False) -> list[dict]:
    """All configured accounts, in the order their credentials were entered.

    Cached in the store so panels stay fast; `refresh=True` re-reads from
    Trello. The cache key is the slot index, never a key or token.
    """
    pairs = await load_pairs(ctx)
    if not pairs:
        return []

    cached: dict[str, dict] = {}
    if not refresh:
        try:
            page = await ctx.store.query(ACCOUNTS_COLLECTION, limit=100)
            for doc in page.data:
                data = doc.data or {}
                slot = data.get("slot")
                if isinstance(slot, int):
                    cached[str(slot)] = data
        except Exception:
            cached = {}

    out: list[dict] = []
    for index, (key, token) in enumerate(pairs):
        hit = cached.get(str(index))
        if hit and hit.get("member_name"):
            entry = dict(hit)
            entry["slot"] = index
            # MARK THE ROW AS CACHED so callers can tell a remembered answer
            # from a fresh one. Without this the resolver cannot know whether
            # re-reading would tell it anything new, and either never retries
            # (missing real boards) or always retries (a wasted round trip on
            # every genuine miss).
            entry["from_cache"] = True
            out.append(entry)
            continue

        info = await describe_pair(ctx, key, token)
        if not info.get("ok"):
            out.append({
                "slot": index,
                "member_name": f"Credential #{index + 1} (not usable)",
                "member_id": "",
                "username": "",
                "email": "",
                "boards": [],
                "status": "error",
                "error": info.get("error", ""),
                "code": info.get("code", ""),
            })
            continue

        boards_out = await fetch_boards(ctx, key, token)
        entry = {
            "slot": index,
            "member_name": info["member_name"],
            "member_id": info["member_id"],
            "username": info["username"],
            "email": info["email"],
            "boards": boards_out.get("boards", []),
            "status": "ok",
            "error": "",
            "code": "",
        }
        out.append(entry)
        await _cache_account(ctx, entry)

    return out


async def list_boards(ctx, *, refresh: bool = False,
                      include_closed: bool = False) -> list[dict]:
    """Every board reachable by any configured credential pair.

    Boards are what this connector addresses, so this is the list the picker and
    `list_boards` tool both render. Closed (archived) boards are hidden by
    default: Trello keeps them reachable forever, and showing them alongside
    live boards makes a stale board look like a current one.
    """
    accounts = await list_accounts(ctx, refresh=refresh)
    boards = flatten_boards(accounts)
    if include_closed:
        return boards
    return [b for b in boards if not b.get("closed")]


def flatten_boards(accounts: list[dict]) -> list[dict]:
    """Every (account, board) pair as one flat row.

    The picker and the resolver both need "all boards reachable by any
    configured credential", which is a flatten rather than a lookup because the
    same board can legitimately appear under two accounts.
    """
    rows: list[dict] = []
    for account in accounts:
        if account.get("status") == "error":
            continue
        for board in account.get("boards") or []:
            rows.append({
                "slot": account.get("slot", 0),
                "account_name": account.get("member_name", ""),
                "id": board.get("id", ""),
                "name": board.get("name", ""),
                "closed": bool(board.get("closed")),
                "url": board.get("url", ""),
            })
    return rows


async def resolve_board(ctx, name: str = "") -> dict:
    """Pick the board to act in.

    No name + exactly one reachable board -> that one. No name + several -> an
    error that LISTS them, because picking one at random and then writing to it
    is unrecoverable.

    A raw board id is accepted too: users paste them out of Trello URLs. Note
    that a board URL also carries an 8-character shortLink
    (trello.com/b/<shortLink>/<slug>) which is NOT an id -- `looks_like_id`
    rejects it, so it falls through to the name match and produces a real
    "no board matches" instead of a confusing 404 from Trello.
    """
    pairs = await load_pairs(ctx)
    if not pairs:
        return tc.fail(tc.TRELLO_CREDENTIALS_MISSING)

    accounts = await list_accounts(ctx)

    # A single configured credential that does not work at all should report
    # ITS reason (revoked, rate-limited), not "no board matches".
    usable = [a for a in accounts if a.get("status") != "error"]
    if accounts and not usable:
        broken = accounts[0]
        return tc.fail(broken.get("code") or tc.TRELLO_TOKEN_REJECTED,
                       broken.get("error") or tc.message_for(tc.TRELLO_TOKEN_REJECTED))

    rows = flatten_boards(accounts)
    if not rows:
        return tc.fail(
            tc.TRELLO_BOARD_UNKNOWN,
            "This credential reaches no open Trello boards. That usually means "
            "the account has no boards yet, or every board it can see is "
            "closed (archived).")

    wanted = (name or "").strip()

    if not wanted:
        if len(rows) == 1:
            row = rows[0]
            key, token = pairs[row["slot"]]
            return {"ok": True, "key": key, "token": token, "board": row}
        names = ", ".join(r.get("name", "?") for r in rows)
        # AMBIGUOUS, not UNKNOWN: every one of these boards is perfectly well
        # known -- what is missing is the CHOICE between them. The two codes
        # lead to different next steps (name one vs. check your access), so
        # conflating them would send the user to fix the wrong thing.
        return tc.fail(
            tc.TRELLO_BOARD_AMBIGUOUS,
            f"Several Trello boards are reachable -- name the one to use: {names}.")

    # A pasted id short-circuits the name match entirely.
    if to.looks_like_id(wanted):
        for row in rows:
            if row.get("id") == wanted:
                key, token = pairs[row["slot"]]
                return {"ok": True, "key": key, "token": token, "board": row}
        # Not in the reachable list, but it IS id-shaped: let Trello be the
        # judge rather than refusing something the user may legitimately reach
        # (a board they were just added to, not yet in the cache).
        key, token = pairs[0]
        return {"ok": True, "key": key, "token": token,
                "board": {"slot": 0, "id": wanted, "name": "", "url": "",
                          "account_name": accounts[0].get("member_name", ""),
                          "closed": False}}

    lowered = wanted.lower()
    exact = [r for r in rows if str(r.get("name", "")).strip().lower() == lowered]
    partial = [r for r in rows if lowered in str(r.get("name", "")).strip().lower()]
    matches = exact or partial

    if not matches:
        # NOT FOUND IN THE CACHE IS NOT THE SAME AS NOT EXISTING. The board list
        # is cached, so anything created since the last read is invisible here:
        # a board made in the Trello UI, by a teammate, or by this connector
        # before cache invalidation was added. Refusing on a stale list told the
        # user a board they were looking at did not exist -- verified live, where
        # a freshly copied board resolved as "no reachable board matches".
        #
        # The re-read happens ONLY when the answer came from the cache and ONLY
        # on the miss path: a live list is already authoritative, so retrying it
        # would just ask Trello the same question twice.
        if any(a.get("from_cache") for a in accounts):
            fresh = await list_accounts(ctx, refresh=True)
            fresh_rows = flatten_boards(fresh)
            exact = [r for r in fresh_rows
                     if str(r.get("name", "")).strip().lower() == lowered]
            partial = [r for r in fresh_rows
                       if lowered in str(r.get("name", "")).strip().lower()]
            matches = exact or partial
            if matches:
                rows = fresh_rows

    if not matches:
        names = ", ".join(r.get("name", "?") for r in rows) or "-"
        return tc.fail(
            tc.TRELLO_BOARD_UNKNOWN,
            f"No reachable Trello board matches '{name}'. Reachable: {names}.")
    if len(matches) > 1:
        names = ", ".join(r.get("name", "?") for r in matches)
        return tc.fail(tc.TRELLO_BOARD_AMBIGUOUS,
                       f"'{name}' matches several boards: {names}.")

    row = matches[0]
    slot = row.get("slot", 0)
    if not isinstance(slot, int) or slot >= len(pairs):
        return tc.fail(tc.TRELLO_BOARD_UNKNOWN,
                       "That board's credential is no longer configured.")
    key, token = pairs[slot]
    return {"ok": True, "key": key, "token": token, "board": row}


# Trello object types this connector resolves by name, mapped to the board
# sub-resource that lists them.
_BOARD_CHILDREN = {
    "list": ("lists", {"filter": "open", "fields": "id,name,closed,pos"}),
    "card": ("cards", {"filter": "open", "fields": "id,name,closed,idList,idBoard"}),
    "member": ("members", {"fields": "id,fullName,username"}),
    "label": ("labels", {"fields": "id,name,color"}),
}


async def list_board_children(ctx, key: str, token: str, board_id: str,
                              kind: str) -> dict:
    """List one kind of thing on a board (lists, cards, members, labels)."""
    spec = _BOARD_CHILDREN.get((kind or "").strip().lower())
    if not spec:
        allowed = ", ".join(sorted(_BOARD_CHILDREN))
        return tc.fail(tc.TRELLO_VALIDATION_FAILED,
                       f"'{kind}' is not a Trello board sub-resource. "
                       f"Use one of: {allowed}.")
    path, params = spec
    return await tc.request(ctx, "GET", f"boards/{board_id}/{path}",
                            (key, token), params=dict(params))


async def resolve_target(ctx, key: str, token: str, board_id: str,
                         reference: str, *, kind: str = "card") -> dict:
    """Resolve a name (or id) on a board to an id.

    Refuses to guess between several matches: silently picking one and then
    writing to it is the expensive kind of wrong. An exact name match wins over
    substring matches, so a card literally called "Bug" is reachable even when
    "Bug triage" also exists.
    """
    ref = (reference or "").strip()
    if not ref:
        return tc.fail(tc.TRELLO_VALIDATION_FAILED,
                       f"No {kind} was named.")

    if to.looks_like_id(ref):
        return {"ok": True, "id": ref, "name": "", "resolved_by": "id"}

    out = await list_board_children(ctx, key, token, board_id, kind)
    if not out.get("ok"):
        return out

    rows = out["data"]
    if not isinstance(rows, list):
        return tc.fail(tc.TRELLO_RESPONSE_UNEXPECTED)

    lowered = ref.lower()
    candidates = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = to.name_of(item)
        if not name:
            continue
        candidates.append((name, to.id_of(item)))

    exact = [c for c in candidates if c[0].strip().lower() == lowered]
    partial = [c for c in candidates if lowered in c[0].strip().lower()]
    matches = exact or partial

    if not matches:
        available = ", ".join(c[0] for c in candidates[:12]) or "-"
        return tc.fail(
            tc.TRELLO_TARGET_NOT_FOUND,
            f"No {kind} on this board matches '{reference}'. Available: {available}.")
    if len(matches) > 1:
        names = ", ".join(c[0] for c in matches[:12])
        return tc.fail(
            tc.TRELLO_TARGET_AMBIGUOUS,
            f"'{reference}' matches several {kind}s: {names}. Name one exactly, "
            "or pass its id.")

    name, found_id = matches[0]
    return {"ok": True, "id": found_id, "name": name, "resolved_by": "name"}


async def forget_cache(ctx) -> None:
    """Drop the cached account/board list.

    Public counterpart to `_forget_cache`, for callers OUTSIDE this module: a
    write that creates a board changes what the cache should say, and reaching
    into a private name from a handler layer is how that coupling gets missed
    when the private one is renamed.
    """
    await _forget_cache(ctx)
