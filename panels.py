"""Panels: connect first, then show what is reachable and why.

Three surfaces, in the order a new user meets them:

* ``connect``     -- center overlay: paste the key/token PAIR and be done. A
                     first-time user opens the app with credentials in hand and
                     needs somewhere to put them; the auto-injected Secrets tab
                     is correct but not discoverable.
* ``accounts``    -- center overlay: connected accounts, the boards they reach,
                     and what an empty list means.
* ``trello_nav``  -- left sidebar: connection state at a glance.

CREDENTIAL HANDLING (federal EXT-SECRETS-V1)
``trello_credentials`` is declared ``write_mode="both"``, so the extension CAN
write it -- which is what makes the Connect form below actually work. The
Notion connector declared its secret ``write_mode="user"`` and its connect
action silently could not save, so the pattern here is deliberate.

The form posts to ``connect_account``, a function OF THIS EXTENSION. A panel
``action`` is resolved against this app's own functions, so pointing it at the
developer app's ``save_app_secret`` would fail at click time -- a trap the
Notion connector's notes document at length.

TWO FIELDS, NOT ONE. Trello access is a pair: the key names the application,
the token names the user. A single input would have to ask the user to
hand-splice ``key:token``, so the form takes them separately and the handler
joins them.

No panel here ever reads a credential back.
"""

from __future__ import annotations

from imperal_sdk import ui

import accounts as acct
from app import ext

# Where the platform's own credential UI lives for this extension.
_SECRETS_ROUTE = "/ext/trello-connector/secrets"

_KEY_PAGE = "https://trello.com/apps/admin"


def _errors_of(records: list[dict]) -> list[str]:
    """Human-readable problems, one per unusable account."""
    out: list[str] = []
    for record in records:
        if record.get("status") == "ok":
            continue
        label = record.get("member_name") or "a credential"
        detail = record.get("error") or "not usable"
        out.append(f"{label}: {detail}")
    return out


def _board_count(records: list[dict]) -> int:
    """How many boards are reachable across every connected account."""
    total = 0
    for record in records:
        boards = record.get("boards")
        if isinstance(boards, list):
            total += len(boards)
    return total


def _state_alert(records: list[dict]):
    """One alert that states the connection situation without hedging."""
    if not records:
        return ui.Alert(
            type="info",
            title="Not connected yet",
            message=("Trello needs an API key and a token. Both come from "
                     "the same page -- the key identifies the app, the "
                     "token identifies you."),
        )

    problems = _errors_of(records)
    usable = len(records) - len(problems)

    if problems and usable == 0:
        return ui.Alert(
            type="error",
            title="No credential is currently working",
            message="; ".join(problems),
        )
    if problems:
        return ui.Alert(
            type="warning",
            title=f"{usable} of {len(records)} credential(s) working",
            message="; ".join(problems),
        )
    return ui.Alert(
        type="success",
        title=f"{usable} account(s) connected",
        message=f"{_board_count(records)} board(s) reachable.",
    )


async def connect_panel(ctx, **kwargs):
    """Paste the key/token pair and connect an account.

    NOT a panel of its own: it is one VIEW of the single center panel below.

    SKETCH -- connect screen (props checked against ui-components-reference)
      ui.Stack (v, gap=4)
        ui.Header(text="Connect Trello", level=2, subtitle=...)
        ui.Alert(...)                       -- already-connected notice, if any
        ui.Section(title="1. Get the pair", children=[
          ui.Text(content=..., variant="body")
          ui.Link(label="Open trello.com/apps/admin", href=_KEY_PAGE)
        ])
        ui.Section(title="2. Paste both halves", children=[
          ui.Form(action="connect_account", submit_label="Connect", children=[
            ui.Input(placeholder="API key", param_name="key")
            ui.Password(placeholder="token", param_name="token")
          ])
          ui.Link(label="Or manage stored credentials directly", href=...)
        ])
        ui.Section(title="3. Check what it reaches", children=[
          ui.Button(label="Check what is reachable", ...)
        ])

    Text nodes use `content=`, never `text=`: `ui.Text(text=...)` fails the
    platform's prop check -- a mistake the Asana panel documents.
    """
    try:
        records = await acct.list_accounts(ctx)
    except Exception:
        records = []

    children: list = [
        ui.Header(
            text="Connect Trello",
            level=2,
            subtitle=("An API key identifies the application; a token "
                      "identifies you. Trello needs both on every request."),
        ),
    ]

    if records:
        children.append(ui.Alert(
            type="info",
            title=f"{len(records)} credential(s) already stored",
            message=("Connecting another adds it alongside the existing "
                     "ones -- nothing is replaced."),
        ))

    children.append(ui.Section(
        title="1. Get the key and the token",
        children=[
            ui.Text(
                content=("Open the admin page, pick your Power-Up and use the "
                         "API Key tab. The token is NOT beside the key: use the "
                         "'Token' link in the paragraph below it, about "
                         "generating a token manually, then click Allow -- "
                         "Trello shows the token to copy. If you have no "
                         "Power-Up yet, create one there first; that is what a "
                         "key belongs to."),
                variant="body",
            ),
            ui.Link(label="Open trello.com/apps/admin", href=_KEY_PAGE),
            # The SECRET sits directly under the key on that page and is the
            # same 64-hex shape a token has, so pasting it is the single most
            # likely mistake here -- and it fails with Trello's misleading
            # "invalid key". Naming it on the screen where the paste happens
            # is worth more than explaining it in the error afterwards.
            ui.Alert(
                type="warning",
                title="The Secret is not the token",
                message=("The 'Secret' shown under the key signs OAuth and can "
                         "never authorise a REST call. A token comes from the "
                         "Allow prompt and is what belongs in the token "
                         "field."),
            ),
        ],
    ))

    children.append(ui.Section(
        title="2. Paste both halves",
        children=[
            ui.Text(
                content=("The key is safe to show -- Atlassian documents it as "
                         "publicly accessible. The token is the secret half and "
                         "is stored encrypted."),
                variant="body",
            ),
            ui.Form(
                action="connect_account",
                submit_label="Connect",
                children=[
                    ui.Input(
                        placeholder="API key (32 hex characters)",
                        param_name="key",
                    ),
                    ui.Password(
                        placeholder="Token",
                        param_name="token",
                    ),
                ],
            ),
            ui.Link(
                label="Or manage the stored credentials directly",
                href=_SECRETS_ROUTE,
            ),
        ],
    ))

    children.append(ui.Section(
        title="3. Check what it reaches",
        children=[
            ui.Text(
                content=("Trello shows whatever the token's owner can already "
                         "see -- there is no per-board sharing step."),
                variant="body",
            ),
            ui.Button(
                label="Check what is reachable",
                variant="secondary",
                on_click=ui.Send("Check my Trello access"),
            ),
        ],
    ))

    return ui.Stack(direction="v", gap=4, children=children)


async def accounts_panel(ctx, **kwargs):
    """Render connected accounts and the boards they reach.

    One VIEW of the single center panel, not a panel itself.
    """
    refresh = bool(kwargs.get("refresh"))
    try:
        records = await acct.list_accounts(ctx, refresh=refresh)
    except Exception:
        records = []

    children: list = [
        ui.Header(
            text="Trello Connector",
            level=2,
            subtitle="Boards, lists and cards, by name rather than by id.",
        ),
        _state_alert(records),
    ]

    if not records:
        children.append(ui.Section(
            title="Connected accounts",
            children=[
                ui.Empty(
                    message="No Trello credentials stored yet.",
                    action=ui.Call("__panel__trello", view="connect"),
                ),
            ],
        ))
    else:
        rows = []
        for record in records:
            boards = record.get("boards")
            board_names = ", ".join(
                str(b.get("name", "")) for b in boards
                if isinstance(b, dict)
            ) if isinstance(boards, list) else ""
            rows.append({
                "account": record.get("member_name") or "",
                "username": record.get("username") or "",
                "boards": str(len(boards) if isinstance(boards, list) else 0),
                "names": board_names[:120],
                "status": "ready" if record.get("status") == "ok" else "error",
            })

        children.append(ui.Section(
            title="Connected accounts",
            children=[
                ui.DataTable(
                    columns=[
                        ui.DataColumn(key="account", label="Account"),
                        ui.DataColumn(key="username", label="Username"),
                        ui.DataColumn(key="boards", label="Boards"),
                        ui.DataColumn(key="names", label="Reachable boards"),
                        ui.DataColumn(key="status", label="Status"),
                    ],
                    rows=rows,
                ),
            ],
        ))

    children.append(ui.Section(
        title="How access works",
        children=[
            ui.Text(
                content=("A token reaches every board its owner can see, across "
                         "every workspace -- Trello has no per-board sharing "
                         "step to perform."),
                variant="body",
            ),
            ui.Text(
                content=("So an empty board list usually means the token "
                         "belongs to a different Trello account than the one "
                         "you are looking at in the browser."),
                variant="body",
            ),
            ui.Row(
                children=[
                    ui.Button(
                        label="Refresh from Trello",
                        variant="secondary",
                        on_click=ui.Call("__panel__trello", view="accounts",
                                         refresh=True),
                    ),
                    ui.Button(
                        label="Connect another account",
                        variant="ghost",
                        on_click=ui.Call("__panel__trello", view="connect"),
                    ),
                ],
            ),
        ],
    ))

    return ui.Stack(direction="v", gap=4, children=children)


@ext.panel("trello", slot="center", title="Trello", icon="Trello",
           center_overlay=True, refresh="manual")
async def trello_center(ctx, **kwargs):
    """The ONE center panel. `view` picks which screen renders inside it.

    Dispatch targets are always the mounted panel, because there is only one:
    `ui.Call("__panel__trello", view=...)`.
    """
    view = str(kwargs.get("view") or "").strip().lower()

    if view == "connect":
        return await connect_panel(ctx, **kwargs)
    return await accounts_panel(ctx, **kwargs)


@ext.panel("trello_nav", slot="left", title="Trello", icon="Trello",
           refresh="manual")
async def trello_nav(ctx, **kwargs):
    """Sidebar entry: connection state at a glance, and a way in."""
    try:
        records = await acct.list_accounts(ctx)
    except Exception:
        # The sidebar must never be the thing that breaks the shell.
        records = []

    usable = sum(1 for r in records if r.get("status") == "ok")
    if not records:
        state = "Not connected yet"
        primary = ui.Button(
            label="Connect Trello",
            variant="primary",
            full_width=True,
            on_click=ui.Call("__panel__trello", view="connect"),
        )
    else:
        state = f"{usable} of {len(records)} account(s) ready"
        primary = ui.Button(
            label="Open Trello panel",
            variant="secondary",
            full_width=True,
            on_click=ui.Call("__panel__trello", view="accounts"),
        )

    return ui.Stack(
        direction="v",
        gap=2,
        children=[
            ui.Text(content=state, variant="body"),
            primary,
            ui.Button(
                label="My Trello cards",
                variant="ghost",
                full_width=True,
                on_click=ui.Send("Show my Trello cards"),
            ),
        ],
    )
