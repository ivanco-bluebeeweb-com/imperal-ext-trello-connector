"""Credential pairs, account listing, and name -> id resolution.

The pair is the whole point of this layer: everything here exists because Trello
access is `key` + `token` and a half-line is not an account.
"""

import accounts as acct
import trello_client as tc
from conftest import (TEST_KEY, TEST_TOKEN, board_payload, card_payload,
                      list_payload, member_payload)


# --- pair parsing -----------------------------------------------------------

def test_pair_is_split_on_first_colon_only():
    pairs = acct.split_pairs(f"{TEST_KEY}:{TEST_TOKEN}")
    assert pairs == [(TEST_KEY, TEST_TOKEN)]


def test_multiple_pairs_one_per_line():
    raw = f"{TEST_KEY}:{TEST_TOKEN}\n{'c' * 32}:{'d' * 64}"
    assert len(acct.split_pairs(raw)) == 2


def test_blank_lines_do_not_create_phantom_accounts():
    raw = f"\n\n{TEST_KEY}:{TEST_TOKEN}\n\n"
    assert len(acct.split_pairs(raw)) == 1


def test_duplicate_pair_is_not_two_accounts():
    raw = f"{TEST_KEY}:{TEST_TOKEN}\n{TEST_KEY}:{TEST_TOKEN}"
    assert len(acct.split_pairs(raw)) == 1


def test_half_line_is_dropped_not_half_accepted():
    """A key with no token cannot authorise anything -- keeping it would show a
    broken line as if it were an account."""
    assert acct.split_pairs(TEST_KEY) == []
    assert acct.split_pairs(f"{TEST_KEY}:") == []
    assert acct.split_pairs(f":{TEST_TOKEN}") == []


def test_join_round_trips():
    pairs = [(TEST_KEY, TEST_TOKEN)]
    assert acct.split_pairs(acct.join_pairs(pairs)) == pairs


# --- describe / connect -----------------------------------------------------

async def test_describe_pair_names_the_account(ctx, http):
    http.push(member_payload())
    out = await acct.describe_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is True
    assert out["member_name"] == "Vlad Ivanco"
    assert out["username"] == "vladivanco"


async def test_describe_pair_reports_rejection_without_leaking(ctx, http):
    http.push("invalid token", status=401)
    out = await acct.describe_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is False
    assert TEST_TOKEN not in str(out)


async def test_add_pair_stores_and_reports_boards(ctx, http):
    http.push(member_payload())                    # validate
    http.push([board_payload(name="Client Work")])  # boards
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is True
    assert out["member_name"] == "Vlad Ivanco"
    stored = await ctx.secrets.get(acct.SECRET_NAME)
    assert TEST_KEY in stored and TEST_TOKEN in stored


async def test_add_pair_refuses_a_credential_that_does_not_work(ctx, http):
    """Storing a bad pair would turn one clear failure into a silent one later."""
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is False
    stored = await ctx.secrets.get(acct.SECRET_NAME)
    assert not stored


async def test_add_pair_is_idempotent(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload()])
    out = await acct.add_pair(connected_ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is True
    assert out.get("already_connected") is True
    stored = await connected_ctx.secrets.get(acct.SECRET_NAME)
    assert stored.count(TEST_KEY) == 1


# --- board resolution -------------------------------------------------------

async def test_single_board_resolves_without_being_named(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload(name="Client Work")])
    out = await acct.resolve_board(connected_ctx, "")
    assert out["ok"] is True
    assert out["board"]["name"] == "Client Work"


async def test_board_matched_by_name(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload(board_id="6a" + "2" * 22, name="Client Work"),
               board_payload(board_id="6b" + "3" * 22, name="Personal")])
    out = await acct.resolve_board(connected_ctx, "Personal")
    assert out["ok"] is True
    assert out["board"]["name"] == "Personal"


async def test_ambiguous_board_name_refuses_to_guess(connected_ctx, http):
    """Picking one and then WRITING to it is the expensive kind of wrong."""
    http.push(member_payload())
    http.push([board_payload(board_id="6a" + "2" * 22, name="Client Work"),
               board_payload(board_id="6b" + "3" * 22, name="Client Work")])
    out = await acct.resolve_board(connected_ctx, "Client Work")
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_BOARD_AMBIGUOUS


async def test_unknown_board_lists_what_exists(connected_ctx, http):
    http.push(member_payload())
    http.push([board_payload(name="Client Work")])
    out = await acct.resolve_board(connected_ctx, "Nonexistent")
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_BOARD_UNKNOWN
    assert "Client Work" in out["error"]


async def test_no_credentials_says_so_clearly(ctx, http):
    out = await acct.resolve_board(ctx, "")
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_CREDENTIALS_MISSING


# --- target resolution ------------------------------------------------------

async def test_target_id_is_passed_through_without_a_lookup(connected_ctx, http):
    """Pasting an id out of a Trello URL must keep working."""
    card_id = "8d" + "5" * 22
    out = await acct.resolve_target(connected_ctx, TEST_KEY, TEST_TOKEN,
                                    "6a" + "2" * 22, card_id, kind="card")
    assert out["ok"] is True
    assert out["id"] == card_id
    assert http.calls == []


async def test_target_resolved_by_name(connected_ctx, http):
    http.push([list_payload(name="To Do"), list_payload(
        list_id="7d" + "5" * 22, name="Done")])
    out = await acct.resolve_target(connected_ctx, TEST_KEY, TEST_TOKEN,
                                    "6a" + "2" * 22, "Done", kind="list")
    assert out["ok"] is True
    assert out["name"] == "Done"


async def test_ambiguous_target_refuses(connected_ctx, http):
    http.push([card_payload(card_id="8d" + "5" * 22, name="Ship it"),
               card_payload(card_id="8e" + "6" * 22, name="Ship it")])
    out = await acct.resolve_target(connected_ctx, TEST_KEY, TEST_TOKEN,
                                    "6a" + "2" * 22, "Ship it", kind="card")
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_TARGET_AMBIGUOUS


async def test_missing_target_names_the_alternatives(connected_ctx, http):
    http.push([list_payload(name="To Do")])
    out = await acct.resolve_target(connected_ctx, TEST_KEY, TEST_TOKEN,
                                    "6a" + "2" * 22, "Backlog", kind="list")
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_TARGET_NOT_FOUND
    assert "To Do" in out["error"]


# --- paste-shape pre-flight -------------------------------------------------
# The admin page shows a 32-hex API key and a 64-hex Secret side by side, so
# these two mistakes are common and both end in Trello's flat "invalid key".

async def test_secret_pasted_as_key_is_named_not_forwarded(ctx, http):
    """A 64-hex key is the Secret: say so instead of asking Trello."""
    out = await acct.add_pair(ctx, "c" * 64, TEST_TOKEN)
    assert out["ok"] is False
    assert out["code"] == tc.TRELLO_KEY_REJECTED
    assert "secret" in out["error"].lower()
    # Decidable from the string, so no request is spent reaching that verdict.
    assert http.calls == []


async def test_swapped_halves_are_named(ctx, http):
    out = await acct.add_pair(ctx, "c" * 64, "d" * 32)
    assert out["ok"] is False
    assert "swap" in out["error"].lower()
    assert http.calls == []


async def test_nothing_is_stored_when_the_shape_is_wrong(ctx, http):
    await acct.add_pair(ctx, "c" * 64, TEST_TOKEN)
    assert await ctx.secrets.get("trello_credentials") in (None, "")


async def test_a_correct_pair_still_reaches_trello(ctx, http):
    """The guard must not become a gate: a normal pair passes through.

    This is the test that matters most -- a shape check that outlives Trello's
    format would reject working credentials, so the happy path is pinned.
    """
    http.push(member_payload())
    http.push([board_payload()])
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is True
    assert http.calls != []


async def test_an_unfamiliar_shape_defers_to_trello(ctx, http):
    """Not-obviously-wrong values are Trello's call, not ours."""
    http.push(member_payload())
    http.push([board_payload()])
    out = await acct.add_pair(ctx, "abc123xyz", "some-token-value")
    assert out["ok"] is True
    assert http.calls != []


# --- rejection must be diagnosable -----------------------------------------
# Trello's "invalid key" is identical whether the key was 8 characters, 40, or
# a perfect 32 that was revoked. Those need opposite actions, so the observed
# SHAPE is reported back.

async def test_rejection_reports_the_observed_lengths(ctx, http):
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, "abcd1234", TEST_TOKEN)
    assert out["ok"] is False
    assert "8 characters" in out["error"]
    assert "32 characters" in out["error"]


async def test_a_wrong_length_key_is_named_as_impossible(ctx, http):
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, "a" * 40, TEST_TOKEN)
    assert "not one" in out["error"]


async def test_a_rejected_token_points_at_the_token(ctx, http):
    """Trello says `invalid token` -> the TOKEN is the thing to regenerate."""
    http.push("invalid token", status=401)
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is False
    low = out["error"].lower()
    assert "revoked" in low or "expired" in low
    # It must NOT tell the user to re-copy a key that is already correct.
    assert "different key" in low or "fresh token" in low


async def test_a_rejected_key_does_not_blame_the_token(ctx, http):
    """The misdiagnosis this replaced.

    Trello validates the KEY FIRST and answers `invalid key` for every bad
    combination; a revoked token reports `invalid token` separately. So on
    `invalid key` with a correctly-shaped key, telling the user to generate a
    fresh token sends them to fix the half that still works -- and the retired
    app-key page is the usual reason a well-formed key is refused.
    """
    http.push("invalid key", status=401)
    http.push("", status=404)          # authorize page: key unknown -> dead
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    low = out["error"].lower()
    assert "does not recognise this key" in low
    assert "app-key" in low
    assert "generate a new api key" in low or "new key" in low


async def test_non_hex_characters_are_flagged(ctx, http):
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, "z" * 32, TEST_TOKEN)
    assert "outside 0-9" in out["error"]


async def test_the_shape_note_never_leaks_the_credential(ctx, http):
    """A credential must not become readable through an error message."""
    http.push("invalid key", status=401)
    secret_key = "feed" + "1" * 28
    out = await acct.add_pair(ctx, secret_key, TEST_TOKEN)
    assert secret_key not in out["error"]
    assert TEST_TOKEN not in out["error"]
    # Not even a prefix: any 6-char run of the real value would be a leak.
    assert secret_key[:6] not in out["error"]
    assert TEST_TOKEN[:6] not in out["error"]


async def test_a_correct_shape_does_not_also_say_recopy_the_key(ctx, http):
    """One conclusion per message.

    Trello's stock line for this code opens with "copy it from the API Key
    tab". When the note has just established the key is exactly right, keeping
    that line means the message says both "your paste is fine" and "re-paste
    it" -- and the user follows the wrong half.
    """
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is False
    assert "the paste is fine" in out["error"]
    # The contradiction was the stock line telling the user to COPY the key
    # again. Pointing at the API Key tab to GENERATE A NEW one is the opposite
    # instruction and is correct here, so the assertion pins the stock
    # sentence itself rather than a substring both messages share.
    assert "Copy it from the API Key tab" not in out["error"]


async def test_a_wrong_shape_still_keeps_the_stock_advice(ctx, http):
    """The opposite case: pointing at the API Key tab is exactly right here."""
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, "abcd1234", TEST_TOKEN)
    assert "API Key tab" in out["error"]



# --- which half is really wrong --------------------------------------------
# Verified against the live API: `invalid key` is what Trello returns for a bad
# PAIR -- a GOOD key with a bad token gets exactly the same wording. Believing
# it talks users into discarding a working key, so the authorize page (which
# takes the key alone) is consulted and its verdict wins.

async def test_a_live_key_is_never_called_dead(ctx, http):
    """The regression that caused a real support loop."""
    http.push("invalid key", status=401)
    http.push("<html>Allow</html>", status=200)   # authorize page: key is live
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    low = out["error"].lower()
    assert "your key is valid" in low
    assert "token is the half at fault" in low
    # It must NOT send the user off to generate a new key.
    assert "generate a new key" not in low


async def test_a_live_key_warns_the_secret_is_not_the_token(ctx, http):
    """The key/secret pair sit side by side; the secret cannot authorise."""
    http.push("invalid key", status=401)
    http.push("<html>Allow</html>", status=200)
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert "secret" in out["error"].lower()


async def test_an_unknown_key_verdict_blames_neither_half(ctx, http):
    """If the check cannot be made, ignorance must not become evidence."""
    http.push("invalid key", status=401)
    http.push("", status=500)          # authorize page: no usable verdict
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    low = out["error"].lower()
    assert "either half could be at fault" in low
    assert "does not recognise this key" not in low


async def test_the_key_check_failing_does_not_break_the_error(ctx, http):
    """A transport failure on the enrichment must not swallow the real reason."""
    http.push("invalid key", status=401)
    http.push(RuntimeError("authorize page unreachable"))
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    assert out["ok"] is False
    assert "what arrived" in out["error"].lower()


async def test_a_wrong_shaped_key_skips_the_extra_call(ctx, http):
    """No point asking about a value that cannot be a key at all."""
    http.push("invalid key", status=401)
    out = await acct.add_pair(ctx, "abcd1234", TEST_TOKEN)
    assert out["ok"] is False
    assert len(http.calls) == 1


async def test_no_message_claims_a_token_link_beside_the_key(ctx, http):
    """The instruction has to match the page the user is looking at.

    Seven messages told the user to click a 'Token' link BESIDE the key. The
    admin page has no such control: the manual-token link sits in a paragraph
    BELOW the key. Directing someone to a button that is not there is worse
    than saying nothing, because they conclude their page is broken.
    """
    import trello_client as _tc
    for code, text in _tc._MESSAGES.items():
        low = text.lower()
        assert "beside it" not in low, code
        assert "beside the key" not in low, code
        assert "beside this key" not in low, code

    # And the same for the live rejection branches, which are built at runtime.
    http.push("invalid key", status=401)
    http.push("<html>Allow</html>", status=200)
    out = await acct.add_pair(ctx, TEST_KEY, TEST_TOKEN)
    low = out["error"].lower()
    # The runtime branches may mention "no button beside the key itself" --
    # that is the correction, not the fiction. What must never appear is an
    # instruction to USE a link there.
    assert "link beside the key" not in low
    assert "link beside this key" not in low
    assert "no button beside the key" in low
