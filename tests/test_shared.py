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
        (/opt/extensions/wordpress-hub-extension/models.py)

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


def test_card_entity_names_the_column_from_the_id_map():
    """A card must say WHICH column it sits in.

    Trello's "cards on a board" route does not accept a `list=true` parameter
    (checked against Atlassian's docs, not assumed), so the response carries
    only `idList`. The flattener read a nested `list` object that never arrives,
    so every card came back with an empty `list_name` -- a board full of cards
    that all looked homeless.
    """
    names = {"L_today": "Today", "L_later": "Later"}

    card = shared.card_entity(
        {"id": "C1", "name": "Draft the audit", "idList": "L_today"}, names)

    assert card.list_name == "Today"


def test_card_entity_leaves_the_column_blank_when_unknown():
    """An unmapped id yields "", never the raw id.

    Falling back to the id would put `L_today` in a human-facing column field --
    noise that reads like a name but is not one.
    """
    card = shared.card_entity(
        {"id": "C1", "name": "Orphan", "idList": "L_gone"}, {"L_x": "X"})

    assert card.list_name == ""


def test_card_entity_still_works_without_a_map():
    """`get_card` calls this with one argument and must keep working."""
    card = shared.card_entity({"id": "C1", "name": "Solo", "idList": "L1"})

    assert card.name == "Solo"
    assert card.list_name == ""


def test_list_entity_counts_embedded_cards():
    """`card_count` comes from the embedded array, when it was requested."""
    col = shared.list_entity(
        {"id": "L1", "name": "Today", "cards": [{"id": "a"}, {"id": "b"}]})

    assert col.card_count == 2


def test_list_entity_counts_zero_when_cards_absent():
    """Without `cards=open`, Trello says nothing -- so the count stays 0."""
    col = shared.list_entity({"id": "L1", "name": "Today"})

    assert col.card_count == 0


def test_card_entity_reports_the_badge_counts():
    """Comments and attachments come from `badges`.

    Trello returns only the fields a request names, and `badges` was missing
    from CARD_FIELDS -- so a card with a comment and a checklist on it reported
    comment_count 0 and an empty checklist summary. The numbers looked like
    facts about an empty card rather than fields nobody had asked for.
    """
    card = shared.card_entity({
        "id": "C1",
        "name": "Webbee write probe",
        "badges": {"comments": 1, "attachments": 2,
                   "checkItems": 3, "checkItemsChecked": 1},
    })

    assert card.comment_count == 1
    assert card.attachment_count == 2
    assert card.checklist_summary == "1/3"


def test_checklist_summary_reads_the_card_not_the_array():
    """The progress lives in the card's badges, so the CARD is the argument.

    Both call sites passed `data["checklists"]` -- a LIST -- to a function that
    reads `badges` off a dict. It returned "" unconditionally, which is
    indistinguishable from "this card has no checklists".
    """
    card = {"id": "C1", "name": "x",
            "badges": {"checkItems": 3, "checkItemsChecked": 0},
            "checklists": [{"id": "CL1", "name": "Проверка"}]}

    assert shared.to.checklist_summary(card) == "0/3"
    # The array itself carries no badges, so passing it can only ever yield "".
    assert shared.to.checklist_summary(card["checklists"]) == ""


# --- custom field value shapes ----------------------------------------------
# Trello's guide is explicit: the key inside `value` is chosen by the FIELD
# TYPE, every scalar is sent as a STRING (including number and checkbox), and a
# dropdown takes an option id under `idValue` with no `value` at all. Getting
# this wrong is a 400 on some types and a silently ignored write on others.

def test_text_field_shape():
    assert shared.custom_field_body("text", "Hello", []) == {
        "value": {"text": "Hello"}}


def test_number_goes_in_as_a_string():
    """A real int would be rejected: Trello wants the number as text."""
    body = shared.custom_field_body("number", "42", [])
    assert body == {"value": {"number": "42"}}
    assert isinstance(body["value"]["number"], str)


def test_checkbox_is_normalised_to_trello_strings():
    for yes in ("true", "yes", "1", "on", "checked"):
        assert shared.custom_field_body("checkbox", yes, []) == {
            "value": {"checked": "true"}}
    for no in ("false", "no", "0", "", "nonsense"):
        assert shared.custom_field_body("checkbox", no, []) == {
            "value": {"checked": "false"}}


def test_date_field_shape():
    assert shared.custom_field_body("date", "2026-08-01", []) == {
        "value": {"date": "2026-08-01"}}


def test_dropdown_resolves_the_option_TEXT_to_its_id():
    """A dropdown takes an option id -- not the text the user typed."""
    options = [
        {"id": "aa" + "1" * 22, "value": {"text": "Low"}},
        {"id": "bb" + "2" * 22, "value": {"text": "High"}},
    ]
    assert shared.custom_field_body("list", "High", options) == {
        "idValue": "bb" + "2" * 22}
    # Case-insensitive, because the user is typing a label they read.
    assert shared.custom_field_body("list", "low", options) == {
        "idValue": "aa" + "1" * 22}


def test_dropdown_returns_empty_for_an_unknown_option():
    """Empty body means REFUSE -- never write nothing while reporting success.

    An unmatched dropdown value must not fall through to {"value": {"text": ...}}:
    Trello would accept the call and the field would stay visibly unset.
    """
    options = [{"id": "aa" + "1" * 22, "value": {"text": "Low"}}]
    assert shared.custom_field_body("list", "Critical", options) == {}
