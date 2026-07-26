# Trello Connector

Read and operate on Trello from Imperal: browse boards, lists and cards, read
card details, checklists and comments, create and update cards, move them
between lists and boards, archive or delete them, and manage members, labels
and checklists.

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

## The 25 tools

**Reading** — `list_accounts`, `list_boards`, `list_lists`, `list_cards`,
`get_card`, `list_comments`, `list_checklists`, `list_labels`, `list_members`,
`search`, `check_access`.

**Writing** — `connect_account`, `create_card`, `update_card`, `move_card`,
`archive_card`, `delete_card`, `add_comment`, `set_card_members`,
`set_card_labels`, `create_list`, `archive_list`, `create_board`,
`create_checklist`, `set_check_item`.

`delete_card` is the only irreversible one: Trello does **not** keep deleted
cards recoverable, which is why archiving is a separate tool and the safer
default.

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

98 tests, no network:

```
python -m pytest tests/ -q
```

The suite covers the request funnel (auth placement, error classification,
pagination), the object translations, credential-pair parsing, every read and
write tool, and — deliberately — that all three panels **render**. A wrong `ui`
prop is invisible to an import test and only raises when a panel is actually
built; all five alerts in this app had exactly that bug until the render tests
caught it.
