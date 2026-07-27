"""Extension declaration, secrets, lifecycle hooks.

CONNECTION MODEL -- why an API key + token pair, and not platform OAuth.

The platform's `ext.oauth(...)` flow only knows three providers: `google`,
`microsoft` and `yahoo` (`ctx.oauth_authorize_url` raises ValueError on
anything else). Trello is not among them, so there is no platform-run OAuth
dance to hand this off to.

Trello's own documented simple path is a PAIR of credentials, and that pairing
is the structural fact this connector is built around:

  * the API KEY identifies the *application* (a Trello Power-Up). Atlassian
    documents it as "intended to be publicly accessible" -- by itself it grants
    access to nothing.
  * the TOKEN identifies the *user* who granted that application access, and
    Atlassian is explicit that tokens "should be kept secret".

Both must travel on every request. So one credential is not enough to reach
Trello, and unlike an Asana PAT or a Notion integration token there is no
single string that constitutes an account.

HOW THIS DIFFERS FROM THE OTHER TRACKER CONNECTORS HERE (deliberately):

  * Notion: one token == one workspace. One token per line.
  * Asana:  one token == one user, reaching many workspaces. One token per line.
  * Trello: one KEY:TOKEN PAIR == one user's access. One PAIR per line.

Trello has no workspace concept above the board in the way Asana does -- it has
ORGANIZATIONS (also surfaced as "Workspaces" in the UI) which own boards, but a
token reaches every board its owner can see regardless of organization. So the
addressable unit for almost every tool here is the BOARD, not a workspace, and
`board` is the parameter that appears everywhere `workspace` does in the Asana
connector.

Credentials are stored ONE PAIR PER LINE so a user can hold separate access for,
say, a personal and a client account. Account names are cached in the store; the
credentials themselves never leave the Vault-encrypted secret.
"""

from imperal_sdk import Extension, ChatExtension

ext = Extension(
    "trello-connector",
    version="1.0.0",
    # Declared so the kernel enforces `tool.required_scopes subset-of declared`
    # instead of falling back to a WILDCARD scope grant (validator V34).
    capabilities=["trello:read", "trello:write"],
    display_name="Trello Connector",
    description=(
        "Read and operate on Trello: browse boards, lists and cards, read card "
        "details, checklists and comments, create and update cards, move them "
        "between lists and boards, manage labels, members, custom fields, "
        "stickers and due dates, copy boards and cards, move whole lists "
        "between boards, manage workspaces and their members, and read board "
        "activity and notifications -- across multiple Trello accounts."
        "\n\n"
        "TWO HONEST LIMITS, both established against the live API rather than "
        "assumed. Attachments are LINK-ONLY: Trello accepts file uploads solely "
        "as multipart/form-data, which this connector does not send, so a URL "
        "can be attached and a local file cannot. And Trello CANNOT PUSH "
        "changes here: its webhooks require a publicly reachable callback URL "
        "that answers Trello's own verification, which an extension does not "
        "have -- so board changes are seen when something asks, never announced. "
        "Anything depending on 'tell me when a card moves' needs a schedule, "
        "not a subscription."
    ),
    icon="icon.svg",
    actions_explicit=True,
)

chat = ChatExtension(
    ext,
    tool_name="trello",
    description=(
        "Trello Connector -- find and read Trello boards, lists and cards, "
        "create and update cards, move or archive them, and manage comments, "
        "labels, members, checklists, custom fields, stickers, votes, "
        "workspaces and due dates. Reads board activity and notifications. "
        "Attachments are links only, and Trello cannot notify this app of "
        "changes -- ask and it looks; it is never told."
    ),
)

# Credentials never flow through chat arguments -- the user pastes them into the
# Connect screen or the platform Secrets tab (auto-added because the secret is
# declared here).
ext.secret(
    "trello_credentials",
    "Trello API key and token -- one 'key:token' pair per line, one line per "
    "account. Generate the key at trello.com/apps/admin (Power-Up -> API Key), "
    "then authorise your own account through Trello's Allow prompt to get a "
    "token for that key. A token reaches every board its owner can see.",
    required=True,
    # "both" -- Panel UI writes it (Secrets manager) AND the app writes it
    # itself from the Connect screen.
    #
    # The Notion connector learned this the hard way: with "user" the app
    # cannot store a credential at all, so a panel form has no action it may
    # legally call, and saving through the owner-facing route reports success
    # while the extension runtime still reads nothing back -- a save that looks
    # like a no-op. With "both" the value is written through the very same
    # client that later reads it, so "saved" and "visible" cannot disagree.
    write_mode="both",
    max_bytes=4096,
    rotation_hint_days=180,
)(lambda: None)


@ext.health_check
async def health_check(ctx) -> dict:
    """Liveness probe: report whether at least one credential pair is set.

    Deliberately does NOT call Trello: a health check must stay fast and must
    not fail because a third party is briefly unreachable. It answers
    "is this app configured", not "is Trello up".

    Counts only lines that actually carry BOTH halves, because a line holding a
    key with no token is not usable access -- reporting it as configured would
    make a broken setup look healthy.
    """
    try:
        raw = await ctx.secrets.get("trello_credentials")
        count = 0
        for line in (raw or "").splitlines():
            key, sep, token = line.partition(":")
            if sep and key.strip() and token.strip():
                count += 1
    except Exception:
        count = 0
    return {
        "healthy": count > 0,
        "accounts_configured": count,
        "detail": ("No Trello API key/token pair configured yet."
                   if count == 0 else f"{count} account credential(s) configured."),
    }


@ext.on_install
async def on_install(ctx):
    """Make the first step traceable -- and knowable.

    A Trello key/token pair cannot be provisioned for the user, so a fresh
    install is inert by design until credentials are pasted. Recording that at
    install time means "nothing works yet" shows up as an expected state in the
    audit log rather than looking like a broken deployment.
    """
    await ctx.log(
        "Trello Connector installed -- awaiting an API key/token pair; "
        "the Connect panel walks the user through it.",
        level="info",
    )
