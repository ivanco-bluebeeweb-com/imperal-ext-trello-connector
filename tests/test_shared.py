"""Tests for the helpers both tool layers depend on.

The case that matters here is not a Trello behaviour at all -- it is a
NEIGHBOUR behaviour. An extension does not get a private interpreter: it runs
beside its siblings, and a top-level module name like `models` is not namespaced
per app. So `sys.modules["models"]` may already hold ANOTHER extension's
`models.py` by the time one of these helpers runs.

That is not hypothetical. Live calls failed with

    cannot import name 'TrelloList' from 'models'
        (/opt/extensions/automations/models.py)
    cannot import name 'TrelloCard' from 'models'
        (/opt/extensions/wp-site-connector-extension/models.py)

-- two DIFFERENT strangers, which is what made it intermittent: whichever app's
module happened to be cached in that worker won. `list_labels` kept working
throughout, because its entity is built from a name bound at import time, so a
green test suite proved nothing about the broken path.

The defence is to bind the names while THIS app's own import path is what
resolves them. These tests state that by sabotage: they poison the cache with a
foreign `models` and require the helpers to keep working anyway.
"""

import sys
import types

import pytest

import shared


def _foreign_models() -> types.ModuleType:
    """A stand-in for another extension's `models.py`.

    Deliberately carries a `TrelloBoard`-shaped hole: the module EXISTS and is
    importable, it simply has none of this app's classes. That is exactly the
    shape of the production failure -- an ImportError about a missing NAME, not
    a missing module.
    """
    mod = types.ModuleType("models")
    mod.__file__ = "/opt/extensions/some-other-app/models.py"
    mod.SomeOtherAppModel = object
    return mod


@pytest.fixture
def foreign_models_cached():
    """Poison `sys.modules['models']` for the duration of one test."""
    original = sys.modules.get("models")
    sys.modules["models"] = _foreign_models()
    try:
        yield
    finally:
        if original is not None:
            sys.modules["models"] = original
        else:
            del sys.modules["models"]


def test_card_entity_survives_a_foreign_models_module(foreign_models_cached):
    """A card still flattens when a sibling extension owns the `models` name.

    This is the exact call that failed in production for `list_cards`.
    """
    card = shared.card_entity({
        "id": "6a66715375a34fdecf05cf90",
        "name": "Draft the July audit",
        "shortUrl": "https://trello.com/c/abc123",
    })

    assert card.name == "Draft the July audit"
    assert card.title == "Draft the July audit"
    assert card.url == "https://trello.com/c/abc123"


def test_list_entity_survives_a_foreign_models_module(foreign_models_cached):
    """A column still flattens too -- this is what broke `list_lists`."""
    column = shared.list_entity({
        "id": "6a66715375a34fdecf05cf91",
        "name": "Doing",
        "idBoard": "6a66715075a34fdecf05cce8",
    })

    assert column.name == "Doing"
    assert column.board == "6a66715075a34fdecf05cce8"


def test_board_entity_survives_a_foreign_models_module(foreign_models_cached):
    """Boards were on the same lazy path, so they were luck, not safety.

    `list_boards` happened to succeed in the live run. Without this, the next
    neighbour to load first would have turned that luck around silently.
    """
    board = shared.board_entity({
        "id": "6a66715075a34fdecf05cce8",
        "name": "My Trello board",
        "closed": False,
    })

    assert board.name == "My Trello board"
    assert board.closed is False


def test_helpers_do_not_import_models_at_call_time():
    """The names must be bound at IMPORT time, not looked up per call.

    Asserting the OUTCOME above is necessary but not sufficient: a future edit
    could restore a lazy `from models import ...` and the sabotage tests would
    still pass whenever the poisoned module happened to be absent. This checks
    the mechanism directly -- the classes are attributes of `shared` itself,
    which is only true if the module resolved them on its own import path.
    """
    assert shared.TrelloCard is not None
    assert shared.TrelloList is not None
    assert shared.TrelloBoard is not None

    source = (shared.card_entity.__code__.co_consts
              + shared.list_entity.__code__.co_consts
              + shared.board_entity.__code__.co_consts)
    # A lazy `from models import X` leaves the module name in the function's
    # constants; a module-level import does not.
    assert "models" not in source
