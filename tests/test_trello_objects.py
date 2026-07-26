"""Object shapes: id recognition, comment extraction, checklist counting.

Trello's field names are its own, and these tests pin the translations that the
rest of the app relies on -- especially the ones that are easy to get subtly
wrong: a board's shortLink is NOT an id, and a comment's text is nested.
"""

import trello_objects as to
from conftest import (card_payload, checklist_payload, comment_action_payload,
                      board_payload, member_payload)


def test_id_recognised():
    assert to.looks_like_id("8d" + "5" * 22) is True


def test_shortlink_is_not_an_id():
    """A board's 8-char shortLink appears in its URL but 404s as an id."""
    assert to.looks_like_id("abc12345") is False


def test_name_is_not_an_id():
    assert to.looks_like_id("To Do") is False
    assert to.looks_like_id("") is False


def test_non_hex_of_right_length_is_not_an_id():
    assert to.looks_like_id("z" * 24) is False


def test_member_name_falls_back_to_username():
    """A Trello member may have no fullName set; username is the real handle."""
    assert to.name_of(member_payload(full_name="Vlad Ivanco")) == "Vlad Ivanco"
    m = member_payload(full_name="")
    assert to.name_of(m) == "vladivanco"


def test_comment_text_is_nested_under_data():
    action = comment_action_payload(text="Ship it")
    assert to.comment_text(action) == "Ship it"
    assert to.is_comment(action) is True


def test_non_comment_action_is_excluded():
    """Card history is full of updateCard actions; they are not comments."""
    action = comment_action_payload(is_comment=False)
    assert to.is_comment(action) is False
    assert to.comment_text(action) == ""


def test_comment_author_reads_member_creator():
    assert to.comment_author(comment_action_payload()) == "Vlad Ivanco"


def test_created_at_prefers_action_date():
    assert to.created_at(comment_action_payload()).startswith("2026-07-21")


def test_created_at_falls_back_to_card_activity():
    assert to.created_at(card_payload()).startswith("2026-07-20")


def test_created_at_never_invents():
    assert to.created_at({}) == ""
    assert to.created_at(None) == ""


def test_checkitem_counts_are_derived_not_read():
    """Trello returns items with a state, never a done/total pair."""
    done, total = to.checkitem_counts(checklist_payload())
    assert (done, total) == (1, 2)


def test_checkitem_counts_on_empty_checklist():
    assert to.checkitem_counts({"checkItems": []}) == (0, 0)
    assert to.checkitem_counts({}) == (0, 0)


def test_checkitem_lines_show_state():
    lines = to.checkitem_lines(checklist_payload())
    assert "Write copy" in lines and "Add images" in lines


def test_board_url_prefers_short_url():
    assert to.board_url(board_payload()).startswith("https://trello.com/b/")


def test_missing_fields_yield_empty_never_guesses():
    assert to.id_of({}) == ""
    assert to.name_of({}) == ""
    assert to.id_of(None) == ""
    assert to.label_names({}) == ""
