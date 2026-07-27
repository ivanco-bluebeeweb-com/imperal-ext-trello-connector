"""The request funnel: auth placement, error classification, pagination.

These are the tests that would have caught the shape mistakes: auth must ride in
the QUERY STRING on every method, a plain-text error body must not crash the
parser, and a 401 must be split into "token bad" vs "scope missing" because the
two have opposite remedies.
"""

import pytest

import trello_client as tc
from conftest import TEST_KEY, TEST_TOKEN, board_payload


CREDS = (TEST_KEY, TEST_TOKEN)


async def test_auth_rides_in_query_on_get(ctx, http):
    http.push(board_payload())
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["ok"] is True
    params = http.last_params()
    assert params["key"] == TEST_KEY
    assert params["token"] == TEST_TOKEN


async def test_auth_rides_in_query_on_write_too(ctx, http):
    """A write must not move the pair into the body -- one placement, one bug."""
    http.push({"id": "x"})
    await tc.request(ctx, "POST", "cards", CREDS, data={"name": "n"})
    assert http.last_params()["key"] == TEST_KEY
    assert http.last_params()["token"] == TEST_TOKEN
    # The body carries only the fields, with no envelope and no credentials.
    assert http.last_body() == {"name": "n"}


async def test_no_envelope_unwrapping(ctx, http):
    """Trello's response IS the payload -- a `data` key would be a real field."""
    http.push([board_payload(name="One"), board_payload(name="Two")])
    out = await tc.request(ctx, "GET", "members/me/boards", CREDS)
    assert isinstance(out["data"], list)
    assert len(out["data"]) == 2


async def test_missing_half_never_reaches_network(ctx, http):
    """A key with no token cannot authorise anything: fail before spending it."""
    out = await tc.request(ctx, "GET", "boards/abc", (TEST_KEY, ""))
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_CREDENTIALS_MISSING
    assert http.calls == []


async def test_plain_text_error_body_is_not_a_parse_failure(ctx, http):
    """Trello answers 401 with `invalid token` as text/plain, not JSON."""
    http.push("invalid token", status=401)
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_TOKEN_REJECTED
    # The failure is classified, not reported as "response wasn't JSON".
    assert out["code"] != tc.TRELLO_RESPONSE_NOT_JSON


async def test_401_scope_message_is_distinguished_from_bad_token(ctx, http):
    """Same status, opposite next steps: re-paste vs re-authorise with write."""
    http.push("unauthorized card permission requested", status=401)
    out = await tc.request(ctx, "PUT", "cards/abc", CREDS, data={"name": "n"})
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_SCOPE_INSUFFICIENT


async def test_404_is_not_found(ctx, http):
    http.push("Card not found", status=404)
    out = await tc.request(ctx, "GET", "cards/abc", CREDS)
    assert out["code"] == tc.TRELLO_NOT_FOUND


async def test_429_is_retryable(ctx, http):
    http.push("Rate limit exceeded", status=429)
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["ok"] is False
    assert out["retryable"] is True


async def test_5xx_is_retryable(ctx, http):
    http.push("server error", status=503)
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["retryable"] is True


async def test_transport_exception_is_classified_not_leaked(ctx, http):
    """The exception TYPE is useful; its string can carry hosts and paths."""
    http.push(TimeoutError("connect to 10.1.2.3 timed out"))
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["ok"] is False
    assert "10.1.2.3" not in out["error"]


async def test_success_status_with_broken_json_is_reported(ctx, http):
    http.push("{not json", status=200)
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["code"] == tc.TRELLO_RESPONSE_NOT_JSON


async def test_every_code_has_a_message():
    """A code with no message would surface as an empty error to the user."""
    codes = [v for k, v in vars(tc).items()
             if k.startswith("TRELLO_") and isinstance(v, str)
             and k != "TRELLO_API"]
    for code in codes:
        assert tc.message_for(code), f"{code} has no message"


async def test_paginate_uses_before_not_offset(ctx, http):
    """Trello's paging is keyset: the id of the last item becomes `before`."""
    first = [board_payload(board_id=f"{i:02d}" + "f" * 22) for i in range(2)]
    http.push(first)
    out = await tc.paginate(ctx, "boards/abc/cards", CREDS, limit=2)
    assert out["ok"] is True
    assert len(out["results"]) == 2
    # A short page means the end: no second request, no invented cursor.
    assert len(http.calls) == 1
    assert "offset" not in http.last_params()


async def test_paginate_stops_at_limit(ctx, http):
    page = [board_payload(board_id=f"{i:02d}" + "f" * 22) for i in range(3)]
    http.push(page)
    out = await tc.paginate(ctx, "boards/abc/cards", CREDS, limit=2)
    assert len(out["results"]) == 2
    assert out["maybe_more"] is True


async def test_paginate_rejects_non_list_response(ctx, http):
    """A list endpoint that answers with an object is a shape error, not data."""
    http.push({"id": "not-a-list"})
    out = await tc.paginate(ctx, "boards/abc/cards", CREDS, limit=10)
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_RESPONSE_UNEXPECTED


async def test_unclassified_4xx_echoes_trellos_own_reason(ctx, http):
    """A status outside the known set must not swallow the explanation.

    This is a REGRESSION TEST for a live failure: creating a custom field came
    back as the bare "The Trello request failed." because the status was not
    400/401/403/404/429, so it fell into the generic bucket where Trello's text
    was discarded. The reason it returned -- one required field missing -- was
    sitting in the body, and finding it cost a dig through the API spec.
    """
    http.push("value not valid for pos", status=422)
    out = await tc.request(ctx, "POST", "customFields", CREDS,
                           data={"name": "Priority"})
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_HTTP_ERROR
    # The actionable part -- what Trello objected to -- survives.
    assert "pos" in out["error"]
    assert "422" in out["error"]


async def test_auth_failures_still_withhold_the_raw_text(ctx, http):
    """Echoing detail is for FIXABLE errors, not for auth.

    On a 401 the curated explanation (re-paste vs re-authorise) is more useful
    than Trello's terse text, and pasting raw auth strings back at the user
    risks surfacing token fragments. The widened echo must not have leaked into
    this path.
    """
    http.push("invalid token", status=401)
    out = await tc.request(ctx, "GET", "boards/abc", CREDS)
    assert out["ok"] is False
    assert "invalid token" not in out["error"].lower()
