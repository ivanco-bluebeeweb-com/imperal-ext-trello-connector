"""Panels must RENDER, not merely import.

A wrong `ui` prop is invisible to every other test in this suite: the handler
tests never touch panels, and importing the module only defines functions. The
component constructors validate their keyword arguments at CALL time, so
`ui.Alert(variant=...)` -- plausible, and wrong, since Alert takes `type` --
raises only when a panel is actually rendered. That mistake was in all five
Alerts here and was caught by rendering, not by reading.

Both states are rendered because they take different branches: the empty state
renders the not-connected alert and `ui.Empty`, and the connected state renders
the account table. A panel that works empty can still fail once data arrives.
"""

import panels
from conftest import TEST_KEY, TEST_TOKEN, board_payload, member_payload


async def test_center_accounts_renders_when_empty(ctx, http):
    node = await panels.trello_center(ctx)
    assert node is not None


async def test_center_connect_renders_when_empty(ctx, http):
    node = await panels.trello_center(ctx, view="connect")
    assert node is not None


async def test_nav_renders_when_empty(ctx, http):
    node = await panels.trello_nav(ctx)
    assert node is not None


async def test_center_accounts_renders_with_an_account(connected_ctx, http):
    """The connected branch renders the table -- a different code path."""
    http.push(member_payload())
    http.push([board_payload()])
    node = await panels.trello_center(connected_ctx)
    assert node is not None


async def test_center_connect_renders_with_an_account(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    node = await panels.trello_center(connected_ctx, view="connect")
    assert node is not None


async def test_nav_renders_with_an_account(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    node = await panels.trello_nav(connected_ctx)
    assert node is not None


async def test_a_panel_never_breaks_the_shell(ctx, http):
    """A panel must not raise when Trello itself is failing.

    The sidebar renders on every page load; if it propagated an API failure the
    whole shell would break over a connector being briefly unreachable.
    """
    http.push("service unavailable", status=503)
    node = await panels.trello_nav(ctx)
    assert node is not None
