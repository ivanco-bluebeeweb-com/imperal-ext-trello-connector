"""Shared fixtures.

MockHTTP from the SDK only registers GET/POST and returns the first pattern
match, which cannot express "PUT this card" or "the same URL answers
differently on the second call" -- both of which the write tools do. So the HTTP
double here is queue-based: each test states the exact sequence of responses it
expects, and every request is recorded for assertions.

Unlike the Asana harness, the payload builders below return BARE objects and
arrays: Trello has no response envelope, so a `data` key here would be a field
of the object itself rather than a wrapper. And Trello's failure bodies are
frequently `text/plain` (`invalid token`), which is why `FakeResponse` must be
able to carry a body that is NOT valid JSON.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeResponse:
    """Mirrors imperal_sdk HTTPResponse closely enough for trello_client."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        self.headers: dict = {}

    def json(self):
        # Mirrors imperal_sdk HTTPResponse.json(): a str/bytes body is PARSED,
        # so Trello's plain-text errors raise here -- which is exactly what
        # drives the client's "plain text is not itself an error" path.
        if isinstance(self.body, (dict, list)):
            return self.body
        if isinstance(self.body, (str, bytes, bytearray)):
            import json as _json
            return _json.loads(self.body)
        raise ValueError(f"Cannot parse {type(self.body).__name__} body as JSON")

    def text(self) -> str:
        return self.body if isinstance(self.body, str) else str(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class QueueHTTP:
    """HTTP double: queue up responses, then inspect what was requested."""

    def __init__(self):
        self.queued: list = []
        self.calls: list[dict] = []

    def push(self, body, status: int = 200):
        """Queue one response (or an Exception instance to raise)."""
        self.queued.append((status, body))
        return self

    def _next(self, method: str, url: str, kwargs) -> FakeResponse:
        self.calls.append({
            "method": method,
            "url": url,
            "json": kwargs.get("json"),
            "params": kwargs.get("params"),
            "headers": kwargs.get("headers") or {},
        })
        if not self.queued:
            raise AssertionError(f"unexpected {method} {url} — no response queued")
        status, body = self.queued.pop(0)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(status, body)

    async def get(self, url, **kw):
        return self._next("GET", url, kw)

    async def post(self, url, **kw):
        return self._next("POST", url, kw)

    async def put(self, url, **kw):
        return self._next("PUT", url, kw)

    async def patch(self, url, **kw):
        return self._next("PATCH", url, kw)

    async def delete(self, url, **kw):
        return self._next("DELETE", url, kw)

    # -- assertion helpers --------------------------------------------------
    def last_body(self) -> dict:
        return self.calls[-1]["json"] or {}

    def last_params(self) -> dict:
        return self.calls[-1]["params"] or {}

    def last_method(self) -> str:
        return self.calls[-1]["method"]

    def last_path(self) -> str:
        """The last request's path, without the API base or the query string.

        The query string is stripped deliberately: it always carries `key` and
        `token`, so an assertion on the raw url would embed a credential in the
        test and fail for the wrong reason when auth placement changes.
        """
        url = self.calls[-1]["url"].split("?")[0]
        return url

    def paths(self) -> list[str]:
        return [c["url"].split("?")[0] for c in self.calls]

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]


@pytest.fixture
def http():
    return QueueHTTP()


@pytest.fixture
def ctx(http):
    from imperal_sdk.testing import MockContext, MockSecretStore

    mock = MockContext()
    mock.secrets = MockSecretStore({})
    mock.http = http
    return mock


# A key and token shaped like the real thing: 32 and 64 hex characters. Shape
# matters because `describe_pair` validates it before spending a request.
TEST_KEY = "a" * 32
TEST_TOKEN = "b" * 64


@pytest.fixture
def connected_ctx(ctx):
    """A ctx with one usable key:token pair already configured."""
    from imperal_sdk.testing import MockSecretStore

    ctx.secrets = MockSecretStore(
        {"trello_credentials": f"{TEST_KEY}:{TEST_TOKEN}"})
    return ctx


# --- Trello payload builders ------------------------------------------------
# All BARE: Trello returns the object or array itself, never a wrapper.

def member_payload(full_name: str = "Vlad Ivanco",
                   username: str = "vladivanco", **extra) -> dict:
    """`/members/me` -- the call that identifies the token's owner."""
    payload = {
        "id": "5f" + "1" * 22,
        "fullName": full_name,
        "username": username,
        "email": "vlad@bluebeeweb.com",
    }
    payload.update(extra)
    return payload


def board_payload(board_id: str = "6a" + "2" * 22,
                  name: str = "Client Work", **extra) -> dict:
    payload = {
        "id": board_id,
        "name": name,
        "closed": False,
        "url": f"https://trello.com/b/abc12345/{name.lower().replace(' ', '-')}",
        "shortUrl": "https://trello.com/b/abc12345",
        "shortLink": "abc12345",
        "idOrganization": "7b" + "3" * 22,
        "dateLastActivity": "2026-07-20T12:00:00.000Z",
    }
    payload.update(extra)
    return payload


def list_payload(list_id: str = "7c" + "4" * 22, name: str = "To Do",
                 board_id: str = "6a" + "2" * 22, **extra) -> dict:
    payload = {
        "id": list_id,
        "name": name,
        "closed": False,
        "idBoard": board_id,
        "pos": 16384,
    }
    payload.update(extra)
    return payload


def card_payload(card_id: str = "8d" + "5" * 22,
                 name: str = "Ship the landing page", **extra) -> dict:
    payload = {
        "id": card_id,
        "name": name,
        "desc": "Copy is approved, needs images.",
        "closed": False,
        "due": "2026-08-01T12:00:00.000Z",
        "dueComplete": False,
        "idList": "7c" + "4" * 22,
        "idBoard": "6a" + "2" * 22,
        "url": f"https://trello.com/c/short123/{card_id}",
        "shortUrl": "https://trello.com/c/short123",
        "dateLastActivity": "2026-07-20T12:00:00.000Z",
        "badges": {"comments": 2, "attachments": 1},
    }
    payload.update(extra)
    return payload


def comment_action_payload(action_id: str = "9e" + "6" * 22,
                           text: str = "Looks good to me",
                           is_comment: bool = True) -> dict:
    """A Trello comment IS an action of type `commentCard`; text at data.text."""
    return {
        "id": action_id,
        "type": "commentCard" if is_comment else "updateCard",
        "date": "2026-07-21T09:00:00.000Z",
        "data": {"text": text} if is_comment else {"old": {"name": "x"}},
        "memberCreator": {"id": "5f" + "1" * 22, "fullName": "Vlad Ivanco",
                          "username": "vladivanco"},
    }


def checklist_payload(checklist_id: str = "af" + "7" * 22,
                      name: str = "Launch steps") -> dict:
    return {
        "id": checklist_id,
        "name": name,
        "idCard": "8d" + "5" * 22,
        "checkItems": [
            {"id": "b0" + "8" * 22, "name": "Write copy", "state": "complete"},
            {"id": "c1" + "9" * 22, "name": "Add images", "state": "incomplete"},
        ],
    }


def label_payload(label_id: str = "d2" + "a" * 22, name: str = "Urgent",
                  color: str = "red") -> dict:
    return {"id": label_id, "name": name, "color": color,
            "idBoard": "6a" + "2" * 22}


def custom_field_payload(field_id: str = "f5" + "d" * 22,
                         name: str = "Priority",
                         field_type: str = "list",
                         with_options: bool = True) -> dict:
    """A custom field definition.

    `type` is the field that decides the write shape, so it is explicit here
    rather than defaulted into text: a test that sets a dropdown value through a
    text-shaped body would pass against a fixture that lied about the type.
    """
    row = {
        "id": field_id,
        "idModel": "6a" + "2" * 22,
        "modelType": "board",
        "name": name,
        "type": field_type,
        "pos": 16384,
        "display": {"cardFront": True},
    }
    if with_options and field_type == "list":
        row["options"] = [
            {"id": "aa" + "1" * 22, "idCustomField": field_id,
             "value": {"text": "Low"}, "color": "none", "pos": 16384},
            {"id": "bb" + "2" * 22, "idCustomField": field_id,
             "value": {"text": "High"}, "color": "none", "pos": 32768},
        ]
    return row


def workspace_payload(org_id: str = "c7" + "e" * 22,
                      display_name: str = "Acme Studio") -> dict:
    return {
        "id": org_id,
        "name": "acmestudio",
        "displayName": display_name,
        "desc": "Client work",
        "website": "https://acme.dev",
        "idBoards": ["6a" + "2" * 22],
    }


def attachment_payload(att_id: str = "e3" + "b" * 22,
                       name: str = "Brief.pdf",
                       is_upload: bool = False) -> dict:
    """An attachment. `isUpload` matters: it decides what deletion destroys."""
    return {
        "id": att_id,
        "name": name,
        "url": "https://example.dev/brief.pdf",
        "mimeType": "application/pdf",
        "bytes": 12345,
        "isUpload": is_upload,
        "date": "2026-07-20T12:00:00.000Z",
    }


# --- ActionResult accessors -------------------------------------------------
# `ActionResult` carries prose on `summary` when it succeeded and on `error`
# when it failed, and the structured code on `error_code`. Tests read through
# these helpers so an assertion cannot quietly check the wrong attribute (there
# is no `.message`, and reaching for one raises AttributeError mid-test).

def succeeded(result) -> bool:
    """Whether the tool succeeded.

    Reads `status`, NOT `result.success` -- `success` is the CLASSMETHOD that
    BUILDS a result, so `assert result.success is True` passes on a function
    object and would green-light a failing tool.
    """
    return result.status == "success"


def code_of(result) -> str:
    return result.error_code or ""


def text_of_result(result) -> str:
    return str((result.summary if succeeded(result) else result.error) or "")
