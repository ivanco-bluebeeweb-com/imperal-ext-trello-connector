# Trello Connector

Read and operate on Trello from Imperal: browse boards, lists and cards, read
card details, checklists and comments, create and update cards, move them
between lists and boards, archive or delete them, manage members, labels,
checklists, custom fields, stickers and votes, copy boards and cards, move whole
lists between boards, and administer workspaces and their members.

Everything is **name-first**. You say "move *Fix hero* to *Done* on *Client
Work*" — you never handle a 24-character Trello id. Pasted ids still work when
a name is genuinely ambiguous.

## Connecting

Trello is not one of the platform's OAuth providers, and its authorisation is a
**pair**, not a single token:

| Half | What it identifies | Where it comes from |
|------|--------------------|---------------------|
| API key | the application | the API Key tab of a Power-Up at [trello.com/apps/admin](https://trello.com/apps/admin) |
| Token | you, the user | the "Token" link beside the key |

Both are required on **every** request — Trello passes them as query
parameters, not as a `Bearer` header. The key alone authorises nothing, so a
half-entered credential is rejected rather than stored.

1. Create (or open) a Power-Up at [trello.com/apps/admin](https://trello.com/apps/admin).
2. Copy the API key, then click the Token link and approve.
3. Paste both into the Trello panel's Connect screen.

The pair is validated against Trello **before** it is stored, so a bad paste is
reported immediately instead of failing on the first real call. Pairs live in
the Vault-encrypted `trello_credentials` secret, one `key:token` per line, and
are never echoed back.

### More than one account

Paste another pair to add it — nothing is replaced. Boards from every working
pair appear together, each tagged with the account that reaches it. When only
one board is reachable, you do not have to name it.

## The 62 tools

**Reading (18)** — `check_access`, `get_card`, `get_token_link`,
`list_accounts`, `list_activity`, `list_attachments`, `list_boards`,
`list_cards`, `list_checklists`, `list_comments`, `list_custom_fields`,
`list_labels`, `list_lists`, `list_members`, `list_notifications`,
`list_stickers`, `list_workspaces`, `search`.

**Writing (41)** — `add_attachment`, `add_check_item`, `add_comment`,
`archive_all_cards`, `archive_card`, `archive_list`, `connect_account`,
`copy_board`, `copy_card`, `create_board`, `create_card`, `create_checklist`,
`create_custom_field`, `create_label`, `create_list`, `create_workspace`,
`delete_attachment`, `delete_board`, `delete_check_item`, `delete_checklist`,
`delete_comment`, `delete_label`, `edit_comment`, `move_all_cards`, `move_card`,
`move_list_to_board`, `set_board_member`, `set_card_labels`, `set_card_members`,
`set_check_item`, `set_custom_field`, `set_custom_field_option`, `set_sticker`,
`set_vote`, `set_workspace_member`, `update_board`, `update_card`,
`update_checklist`, `update_label`, `update_list`, `update_workspace`.

**Destructive (3)** — `delete_card`, `delete_custom_field`, `delete_workspace`.

These three are gated behind an explicit `confirm` because Trello keeps no copy:
a deleted card is gone, and deleting a custom field takes its value on **every**
card with it. `delete_board` and `delete_attachment` are irreversible too and
carry the same gate; archiving exists as the recoverable default for cards and
lists, which is why it is a separate tool rather than a flag.

## What this connector cannot do

Both limits below were established against the live API, not assumed. They are
written down because a connector that quietly does nothing is worse than one
that says no.

**Attachments are link-only.** Trello accepts file uploads solely as
`multipart/form-data`; this connector sends JSON. So a URL can be attached and a
local file cannot. `add_attachment` says so rather than accepting a path and
silently attaching nothing.

**Trello cannot push changes here.** Trello's webhooks require a publicly
reachable callback URL that answers Trello's own verification request, which an
extension does not have. This was probed live before being accepted as a limit.
Consequence: board changes are seen when something *asks*, never announced —
anything that wants "tell me when a card moves" needs a schedule, not a
subscription.

## Why the Trello API shapes this code

Four facts about Trello drive most of the design, and each one is a bug if
assumed away:

* **No response envelope.** Trello returns the object or array itself. A `data`
  key in a Trello response is a real field, not a wrapper to unwrap.
* **Updates are PUT, not PATCH.** There is no PATCH route for a card; sending
  one returns a 404 that reads like a missing card.
* **A comment is an action.** Comments are created at
  `/cards/{id}/actionsComments` and read back as actions of type `commentCard`,
  with the text at `data.text`. There is no comment resource.
* **Lists do not exist across boards.** Moving a card to another board needs
  `idBoard` *and* `idList`, because the source board's list id is meaningless
  on the destination.

Errors get the same care: Trello's failure bodies are frequently `text/plain`
(`invalid token`), so a JSON parse failure is not itself treated as an error,
and a 401 is split into "the token was rejected" versus "the token is fine but
lacks scope" — two problems with opposite fixes.

## Panels

* **Trello** (center) — connected accounts, the boards they reach, and a
  Connect screen for pasting a new pair.
* **Trello** (left) — connection state at a glance.
* **Secrets** (right) — the platform's own credential editor.

## Tests

202 tests, no network:

```
python -m pytest tests/ -q
```

The suite covers the request funnel (auth placement, error classification,
pagination), the object translations, credential-pair parsing, every read and
write tool, and — deliberately — that all three panels **render**. A wrong `ui`
prop is invisible to an import test and only raises when a panel is actually
built; all five alerts in this app had exactly that bug until the render tests
caught it.

### What the tests could not tell us

A green suite is not a working connector, and this app is the evidence. Every one
of these was found by calling the real Trello, with all tests passing:

* `create_custom_field` was missing a **required** field (`pos`) and failed every
  time. The mock accepted the body the code sent, because the mock was written
  from the same assumption as the code.
* The failure said only *"The Trello request failed."* Trello had named the
  offending field in the response body; the client echoed that text for one
  status code and discarded it for the rest.
* Clearing a custom field took **three** live rounds to get right, each one
  disproving the fix before it. A dropdown rejected the body that worked for
  scalars (it has no `value` to empty, only an option id to unset). The obvious
  replacement, an empty `value` object, turned out to be rejected for *every*
  type — clearing means writing the field's own key as an empty string. And then
  `date` rejected that too, because `""` is not a date-time, so a date is the one
  type that genuinely needs a bare empty `value`. A regression test had asserted
  the very body Trello refuses, and passed.
* `set_vote` blamed the token for a refusal caused by a board **preference**,
  sending the user to regenerate credentials that were working seconds earlier.
* A board copied successfully then resolved as *"no reachable board matches"* —
  a cached board list that three writes changed without invalidating.
* A workspace description, once set, could not be removed at all: an empty
  string means "not given", so nothing could express "clear it". Found by trying
  to undo a test edit and discovering the connector could not.

The pattern is worth naming: tests written alongside code share the code's
assumptions, so they confirm the shape the author already believed. Only the real
API disagrees. Each fix above carries a regression test, and each of those was
**sabotage-verified** — the fix is reverted, the suite must go red, and the
comment records which live failure it came from so nobody "tidies" it away.

One of those tests was itself wrong before sabotage caught it: asserting "the
response queue is empty" looked like proof no extra request went out, but an
unqueued request fails inside the account layer, which swallows it — queue empty,
test green, bug undetected. Counting requests is the assertion that bites.
