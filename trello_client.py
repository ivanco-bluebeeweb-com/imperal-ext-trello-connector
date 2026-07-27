"""Trello REST helpers: one request funnel, structured errors, pagination.

Four Trello shapes drive everything in this module, all verified against
developer.atlassian.com rather than recalled:

* AUTH IS A PAIR, NOT A TOKEN. Every call carries BOTH `key` (identifies the
  Power-Up / app) and `token` (identifies the user who granted access), passed
  as QUERY PARAMETERS. There is no Bearer header form of this -- the documented
  header alternative is OAuth1-shaped (`Authorization: OAuth
  oauth_consumer_key="..", oauth_token=".."`). Query params are the documented
  simple path, so that is what this uses.
* NO ENVELOPE. Unlike Asana, a Trello response IS the payload: a list endpoint
  returns a bare JSON array, an object endpoint a bare JSON object. Nothing to
  unwrap, and a `data` key would be a field of the object itself.
* ERRORS ARE OFTEN PLAIN TEXT. Trello answers many failures with a
  `text/plain` body such as `invalid token` or `unauthorized card permission
  requested` -- not JSON. So error classification must never assume a JSON
  body, and the raw text is the only detail available.
* PAGINATION IS KEYSET, NOT OFFSET. Long collections cap at 1000 and are paged
  with `before` / `since`, which operate on the CREATION DATE of the item --
  and accept the id of a card/action, from which Trello derives that date.
  There is no opaque cursor and no page number to increment.

Nothing in this module puts a key or a token into a message, a log line or an
error.
"""

from __future__ import annotations

TRELLO_API = "https://api.trello.com/1"

# Trello caps long collections (cards, actions) at 1000 items per request.
MAX_PAGE_SIZE = 1000

# --- structured error codes (I-EXT-ERROR-CODE-NORMALIZED) -------------------
# Every error that reaches the user carries a stable code: it is what the
# platform error taxonomy, self-diagnosis and honest narration key on. An
# error emitted without one is stamped EXT_UNSTRUCTURED_ERROR at the dispatch
# boundary, which degrades the user's diagnosis to prose parsing.
#
# Platform taxonomy codes (imperal_sdk.chat.error_codes) are reused where the
# meaning matches exactly: PERMISSION_DENIED, RATE_LIMITED, BACKEND_5XX,
# BACKEND_TIMEOUT. Everything Trello-specific gets an app-declared code
# matching ^[A-Z][A-Z0-9_]{2,63}$. The code never appears in the message prose
# -- the two travel as separate fields.
TRELLO_CREDENTIALS_MISSING = "TRELLO_CREDENTIALS_MISSING"
TRELLO_CREDENTIALS_MALFORMED = "TRELLO_CREDENTIALS_MALFORMED"
# WHICH half is missing decides where the user has to go: the key lives in the
# Power-Up admin page, the token behind an authorise prompt. Collapsing both
# into CREDENTIALS_MISSING would send half the users to the wrong screen.
TRELLO_KEY_MISSING = "TRELLO_KEY_MISSING"
TRELLO_TOKEN_MISSING = "TRELLO_TOKEN_MISSING"
TRELLO_TOKEN_REJECTED = "TRELLO_TOKEN_REJECTED"
TRELLO_KEY_REJECTED = "TRELLO_KEY_REJECTED"
TRELLO_NOT_FOUND = "TRELLO_NOT_FOUND"
TRELLO_VALIDATION_FAILED = "TRELLO_VALIDATION_FAILED"
TRELLO_UNREACHABLE = "TRELLO_UNREACHABLE"
TRELLO_RESPONSE_NOT_JSON = "TRELLO_RESPONSE_NOT_JSON"
TRELLO_RESPONSE_UNEXPECTED = "TRELLO_RESPONSE_UNEXPECTED"
TRELLO_HTTP_ERROR = "TRELLO_HTTP_ERROR"
TRELLO_ACCOUNT_UNKNOWN = "TRELLO_ACCOUNT_UNKNOWN"
TRELLO_BOARD_UNKNOWN = "TRELLO_BOARD_UNKNOWN"
TRELLO_BOARD_AMBIGUOUS = "TRELLO_BOARD_AMBIGUOUS"
TRELLO_TARGET_NOT_FOUND = "TRELLO_TARGET_NOT_FOUND"
TRELLO_TARGET_AMBIGUOUS = "TRELLO_TARGET_AMBIGUOUS"
# Trello answers 401 for BOTH "this token is not valid" and "this token is
# valid but lacks the scope for that write". Same status, opposite next steps:
# re-paste vs re-authorise with write scope. The plain-text body is what tells
# them apart, so the distinction gets its own code.
TRELLO_SCOPE_INSUFFICIENT = "TRELLO_SCOPE_INSUFFICIENT"
# Credential STORAGE failures -- deliberately distinct from "not configured".
# Without these, an unreadable secret store surfaces as CREDENTIALS_MISSING:
# "paste your key" advice for a problem no amount of pasting can fix.
TRELLO_SECRET_UNAVAILABLE = "TRELLO_SECRET_UNAVAILABLE"
TRELLO_SECRET_WRITE_FAILED = "TRELLO_SECRET_WRITE_FAILED"

_MESSAGES = {
    TRELLO_CREDENTIALS_MISSING: (
        "No Trello credentials are configured yet. Trello needs a PAIR: an API "
        "key from trello.com/apps/admin (API Key tab of your Power-Up) and a "
        "token generated with that key. Paste them into the Connect screen as "
        "key:token."
    ),
    TRELLO_CREDENTIALS_MALFORMED: (
        "A Trello credential line has to be 'key:token' -- both halves, "
        "separated by a colon. A key on its own cannot read anything, and a "
        "token on its own cannot identify the app that issued it."
    ),
    TRELLO_KEY_MISSING: (
        "The API key is missing. Find it at trello.com/apps/admin -- open your "
        "Power-Up and use the API Key tab."
    ),
    TRELLO_TOKEN_MISSING: (
        "The token is missing. On the same page as your API key, click the "
        "manual 'Token' link on that tab and allow access -- Trello then shows the "
        "token to copy."
    ),
    TRELLO_TOKEN_REJECTED: (
        "Trello rejected the credentials -- the token may have been revoked or "
        "expired, or the key and token may not belong together (a token only "
        "works with the key that generated it). Generate a fresh token and "
        "connect again."
    ),
    TRELLO_KEY_REJECTED: (
        "Trello did not recognise the API key. Copy it from the API Key tab of "
        "your Power-Up at trello.com/apps/admin."
    ),
    TRELLO_SCOPE_INSUFFICIENT: (
        "The token is valid but was not granted permission for that action. A "
        "read-only token cannot change boards or cards -- generate a new token "
        "with the write scope and connect it again."
    ),
    TRELLO_NOT_FOUND: (
        "Trello has no such item, or the account behind this token cannot see "
        "it. Check the name, and check that the board has not been closed or "
        "left."
    ),
    "PERMISSION_DENIED": (
        "The account behind this token is not allowed to do that in Trello. "
        "Its access to that board is read-only or absent."
    ),
    TRELLO_VALIDATION_FAILED: "Trello rejected the request as invalid.",
    "RATE_LIMITED": (
        "Trello is rate-limiting requests -- its limits are per API key across "
        "all of that key's tokens, so a busy integration can trip them. Try "
        "again shortly."
    ),
    "BACKEND_5XX": "Trello returned a server error -- try again shortly.",
    "BACKEND_TIMEOUT": "Trello took too long to respond -- try again shortly.",
    TRELLO_UNREACHABLE: "Could not reach the Trello API.",
    TRELLO_SECRET_UNAVAILABLE: (
        "The secure store holding your Trello credentials could not be read "
        "just now, so the connection state is unknown. This is not a problem "
        "with your token -- try again shortly."
    ),
    TRELLO_SECRET_WRITE_FAILED: (
        "The credentials could not be saved to the secure store, so nothing "
        "was changed. Try again shortly."
    ),
    TRELLO_ACCOUNT_UNKNOWN: (
        "That Trello account is not connected. Check the account list, or "
        "connect the credentials again."
    ),
    TRELLO_BOARD_UNKNOWN: (
        "No Trello board of that name is reachable with the connected "
        "credentials. Check the spelling, or connect an account that is a "
        "member of that board."
    ),
    TRELLO_BOARD_AMBIGUOUS: (
        "Several Trello boards match that name, so the board has to be named "
        "exactly -- picking one at random and writing to it is not "
        "recoverable."
    ),
    TRELLO_TARGET_NOT_FOUND: (
        "Nothing of that name was found. Trello's search only sees boards and "
        "cards the account has access to."
    ),
    TRELLO_TARGET_AMBIGUOUS: (
        "That name matches more than one item, so it is not clear which one "
        "was meant. Use a more specific name, or paste the Trello id from the "
        "item's URL."
    ),
    TRELLO_RESPONSE_NOT_JSON: (
        "Trello returned something that is not JSON. That usually means a "
        "gateway or captcha page rather than the API itself."
    ),
    TRELLO_RESPONSE_UNEXPECTED: (
        "Trello's reply did not have the shape this connector expects, so it "
        "was not trusted rather than guessed at."
    ),
    TRELLO_HTTP_ERROR: "The Trello request failed.",
}

_RETRYABLE = {"RATE_LIMITED", "BACKEND_5XX", "BACKEND_TIMEOUT",
              TRELLO_UNREACHABLE, TRELLO_SECRET_UNAVAILABLE,
              TRELLO_SECRET_WRITE_FAILED}


def is_retryable(code: str) -> bool:
    """Whether retrying the identical call could plausibly succeed."""
    return code in _RETRYABLE


def message_for(code: str) -> str:
    """User-facing text for a structured code (prose and code stay separate)."""
    return _MESSAGES.get(code, "The Trello request failed.")


def fail(code: str, error: str = "") -> dict:
    """Build the module's error envelope with a stable code."""
    return {"ok": False, "code": code, "retryable": is_retryable(code),
            "error": error or message_for(code)}


def transport_error_code(exc: BaseException) -> str:
    """Classify a transport-level failure talking to Trello.

    A timeout is a distinct, retryable condition with its own taxonomy code --
    worth separating from "host does not resolve / refused the connection",
    because the useful next step differs.
    """
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name:
        return "BACKEND_TIMEOUT"
    return TRELLO_UNREACHABLE


def body_text(body) -> str:
    """Best-effort human detail from a Trello error body.

    Trello's failures are usually `text/plain` (`invalid token`), sometimes
    JSON with a `message`. Both are handled; neither is assumed.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:
            return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or "")
    return ""


def classify(status_code: int, body) -> tuple[str, str]:
    """Map a failed Trello response onto (code, user-facing message).

    Trello sends no machine-readable error code, so the HTTP status leads. The
    one place text is inspected is 401: Trello uses it for BOTH an invalid
    token AND a valid token missing a scope, and those need opposite advice
    (re-paste vs re-authorise with write permission). Text-sniffing is fragile
    by nature, so it only ever REFINES a 401 that would otherwise be the
    generic "rejected" -- a wording change on Trello's side degrades to the
    honest generic answer instead of breaking.
    """
    detail = body_text(body)
    lowered = detail.lower()

    if status_code == 400:
        code = TRELLO_VALIDATION_FAILED
    elif status_code == 401:
        if "permission" in lowered or "scope" in lowered:
            code = TRELLO_SCOPE_INSUFFICIENT
        elif "invalid key" in lowered or "invalid app key" in lowered:
            code = TRELLO_KEY_REJECTED
        else:
            code = TRELLO_TOKEN_REJECTED
    elif status_code == 403:
        code = "PERMISSION_DENIED"
    elif status_code == 404:
        code = TRELLO_NOT_FOUND
    elif status_code == 429:
        code = "RATE_LIMITED"
    elif 500 <= status_code < 600:
        code = "BACKEND_5XX"
    else:
        code = TRELLO_HTTP_ERROR

    message = _MESSAGES.get(code) or f"Trello request failed (HTTP {status_code})."
    # Trello's own text is echoed ONLY for validation errors: there it names
    # the offending field ("invalid value for idList"), which is exactly what
    # makes the failure fixable. It is not echoed for auth failures, where the
    # curated explanation is better and the raw text adds nothing actionable.
    if code == TRELLO_VALIDATION_FAILED and detail:
        message = f"Trello rejected the request: {detail}"
    return code, message


async def request(ctx, method: str, path: str, creds, *,
                  data: dict | None = None, params: dict | None = None,
                  timeout: int = 30) -> dict:
    """Call one Trello endpoint.

    `creds` is a (key, token) pair. Both halves are appended as QUERY
    PARAMETERS on every request, including writes -- that is Trello's
    documented simple auth path, and doing it in exactly one place is what
    stops half the call sites from forgetting one of the two.

    Returns {"ok": True, "data": ...} -- Trello has no response envelope, so
    `data` is the parsed body as-is -- or {"ok": False, "error", "code",
    "retryable"}. Every Trello call in this app funnels through here, so
    classification and timeouts cannot drift between sites.
    """
    key, token = ("", "")
    if isinstance(creds, (tuple, list)) and len(creds) == 2:
        key, token = creds[0] or "", creds[1] or ""
    elif isinstance(creds, dict):
        key, token = creds.get("key") or "", creds.get("token") or ""

    if not key or not token:
        return fail(TRELLO_CREDENTIALS_MISSING)

    url = f"{TRELLO_API}/{path.lstrip('/')}"
    fn = getattr(ctx.http, method.lower())

    # Auth rides in the query string for EVERY method. Trello also accepts the
    # pair inside a JSON body on PUT/POST, but mixing the two placements means
    # two things to keep right; one placement means one.
    query = dict(params or {})
    query["key"] = key
    query["token"] = token

    kwargs: dict = {
        "params": query,
        "timeout": timeout,
        "headers": {"Accept": "application/json"},
    }
    if data is not None:
        kwargs["json"] = data
        kwargs["headers"]["Content-Type"] = "application/json"

    try:
        # Explicit timeout: a hanging call must fail as a diagnosable
        # in-handler exception, not hang until the platform cancels the
        # coroutine (which surfaces to the user as an opaque INTERNAL).
        resp = await fn(url, **kwargs)
    except Exception as e:
        # The exception TYPE is a useful fact (DNS vs refused vs timeout); the
        # raw exception string is not -- it can carry hosts and internal paths.
        return fail(transport_error_code(e))

    body = resp.body
    parsed_failed = False
    if isinstance(body, (str, bytes, bytearray)) and body:
        try:
            body = resp.json()
        except Exception:
            # Trello error bodies are frequently plain text, so a parse failure
            # is NOT itself an error here -- the raw text is kept as the detail
            # and the status decides the outcome.
            parsed_failed = True

    if resp.status_code >= 400:
        code, message = classify(resp.status_code, body)
        return {"ok": False, "code": code, "error": message,
                "retryable": is_retryable(code)}

    if parsed_failed:
        return fail(TRELLO_RESPONSE_NOT_JSON,
                    "Trello returned a success status but the response body "
                    "wasn't valid JSON.")

    # A 200 with an empty body is legitimate for some deletes.
    if body is None or body == "":
        return {"ok": True, "data": {}}

    return {"ok": True, "data": body}


async def get_list(ctx, path: str, creds, *, params: dict | None = None,
                   limit: int = 50) -> dict:
    """GET a Trello collection endpoint and validate that it IS a collection.

    Trello returns bare arrays, so "did I get a list?" is a real check rather
    than an envelope inspection: a dict here means the path was wrong (an
    object endpoint), and silently iterating a dict would yield its KEYS.
    """
    query = dict(params or {})
    # Trello's own cap; asking for more is rejected rather than clamped.
    query["limit"] = max(1, min(MAX_PAGE_SIZE, limit))

    out = await request(ctx, "GET", path, creds, params=query)
    if not out.get("ok"):
        return out

    items = out["data"]
    if not isinstance(items, list):
        return fail(TRELLO_RESPONSE_UNEXPECTED,
                    "Trello returned a single object where a list was "
                    "expected.")
    return {"ok": True, "results": items[:limit],
            # Trello gives no "has more" flag; a full page is the only signal,
            # and it is a HINT, not a fact -- an exactly-full last page looks
            # identical to a truncated one.
            "maybe_more": len(items) >= limit}


async def paginate(ctx, path: str, creds, *, params: dict | None = None,
                   limit: int = 200, max_pages: int = 10,
                   id_field: str = "id") -> dict:
    """Follow Trello's keyset pagination (`before`) until `limit` or `max_pages`.

    Trello has no cursor: paging backwards through a long collection means
    passing the ID OF THE LAST ITEM as `before`, and Trello derives the
    creation date from it. That is why this needs `id_field` -- there is
    nothing else to page on.

    `max_pages` is a hard stop so one tool call on a huge board can never turn
    into an unbounded crawl.
    """
    results: list = []
    before = ""

    for _ in range(max_pages):
        want = min(MAX_PAGE_SIZE, max(1, limit - len(results)))
        page_params = dict(params or {})
        page_params["limit"] = want
        if before:
            page_params["before"] = before

        out = await request(ctx, "GET", path, creds, params=page_params)
        if not out.get("ok"):
            return out

        batch = out["data"]
        if not isinstance(batch, list):
            return fail(TRELLO_RESPONSE_UNEXPECTED,
                        "Trello returned a list endpoint response that was "
                        "not a list.")
        results.extend(batch)

        # A short page means the collection is exhausted -- the only reliable
        # end-of-data signal Trello offers.
        if len(batch) < want or len(results) >= limit:
            break

        last = batch[-1] if isinstance(batch[-1], dict) else {}
        before = str(last.get(id_field) or "")
        if not before:
            # No id to page on: stop rather than re-request the same page
            # forever.
            break

    return {"ok": True, "results": results[:limit],
            "maybe_more": len(results) >= limit}
