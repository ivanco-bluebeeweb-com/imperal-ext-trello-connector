"""Pydantic parameter models and SDL return entities.

Every parameter that names a Trello object accepts a NAME, not just an id: the
user says "the Backlog list", not a 24-character hex string. Ids still work --
pasting one out of a Trello URL must keep working -- but nothing here ever
requires the user to go find one.

`BoardScoped` is the base for almost every tool. It is optional in practice:
when the credentials reach exactly one board, omitting it resolves to that one.
This is the Trello analogue of the Asana connector's `WorkspaceScoped`, and the
difference is deliberate -- Trello's token is not scoped to a workspace, so the
BOARD is the unit that has to be chosen.
"""

from pydantic import BaseModel, Field, model_validator
from imperal_sdk import sdl


# --------------------------- parameters ---------------------------

class BoardScoped(BaseModel):
    """Base for every tool: which Trello board to act in."""
    board: str = Field(
        "", description="Board name, e.g. 'Client Work'. Omit when the "
                        "credentials reach only one board.")


class ListAccountsParams(BaseModel):
    refresh: bool = Field(
        False, description="Re-read account and board details from Trello "
                           "instead of the cache")


class ConnectAccountParams(BaseModel):
    """The credentials the user pastes on the Connect screen.

    Not BoardScoped: this is the one action that runs BEFORE any board is
    known, so asking which board to act in would be circular. The boards are
    discovered FROM the credentials.

    TWO fields, not one, because Trello access is a PAIR -- the key identifies
    the app, the token identifies the user, and neither works alone.
    """
    key: str = Field(
        "", description="Trello API key -- 32 hex characters, from the API Key "
                        "tab of your Power-Up at trello.com/apps/admin.")
    token: str = Field(
        "", description="Trello token -- generated for that same key through "
                        "Trello's Allow prompt. NOT the Secret shown under the "
                        "key: that signs OAuth and cannot authorise a call.")


class ListBoardsParams(BaseModel):
    include_closed: bool = Field(
        False, description="Also list closed (archived) boards")
    limit: int = Field(
        50, ge=1, le=200, description="Maximum boards to return")
    refresh: bool = Field(
        False, description="Re-read the board list from Trello instead of the "
                           "cache")


class ListListsParams(BoardScoped):
    include_closed: bool = Field(
        False, description="Also list archived lists")


class ListCardsParams(BoardScoped):
    list_name: str = Field(
        "", description="Only cards in this list (column), e.g. 'Doing'. Omit "
                        "for every card on the board.")
    include_closed: bool = Field(
        False, description="Also include archived cards")
    limit: int = Field(
        50, ge=1, le=200, description="Maximum cards to return")


class GetCardParams(BoardScoped):
    card: str = Field(..., description="Card to read (name or id)")


class SearchParams(BaseModel):
    """Trello search is ACCOUNT-wide, not board-scoped.

    Not BoardScoped on purpose: `/search` spans every board the token can see,
    which is the point of a search. Restricting it would make it a worse
    version of list_cards.
    """
    query: str = Field(..., description="Text to search for in cards and boards")
    kind: str = Field(
        "cards", description="What to search for: 'cards', 'boards', 'members' "
                             "or 'all'")
    limit: int = Field(
        20, ge=1, le=100, description="Maximum results to return")
    partial: bool = Field(
        True, description="Match partial words too -- 'reno' finds "
                          "'renovation'. Trello's default is exact-word only.")


class ListCommentsParams(BoardScoped):
    card: str = Field(..., description="Card whose comments to read (name or id)")
    limit: int = Field(
        50, ge=1, le=100, description="Maximum comments to return")


class ListMembersParams(BoardScoped):
    pass


class ListLabelsParams(BoardScoped):
    pass


class ListChecklistsParams(BoardScoped):
    card: str = Field(..., description="Card whose checklists to read (name or id)")


class CheckAccessParams(BoardScoped):
    pass


class CreateCardParams(BoardScoped):
    name: str = Field(..., description="Card title")
    list_name: str = Field(
        ..., description="List (column) to create the card in, e.g. 'To Do'")
    desc: str = Field("", description="Card description")
    due: str = Field(
        "", description="Due date 'YYYY-MM-DD' or a full timestamp")
    members: str = Field(
        "", description="Comma-separated member names to assign, or 'me'")
    labels: str = Field(
        "", description="Comma-separated label names or colours to attach")
    position: str = Field(
        "bottom", description="Where in the list: 'top' or 'bottom'")


class UpdateCardParams(BoardScoped):
    card: str = Field(..., description="Card to update (name or id)")
    name: str = Field("", description="New title (omit to keep the current one)")
    desc: str = Field("", description="Replace the description")
    due: str = Field("", description="New due date 'YYYY-MM-DD' or timestamp")
    due_complete: bool | None = Field(
        None, description="Tick (true) or untick (false) the due date as done")
    clear_due: bool = Field(False, description="Remove the due date entirely")
    closed: bool | None = Field(
        None, description="Archive (true) or unarchive (false) the card")


class MoveCardParams(BoardScoped):
    card: str = Field(..., description="Card to move (name or id)")
    list_name: str = Field(
        "", description="Destination list (column) on the current board")
    to_board: str = Field(
        "", description="Move to a different board (name or id). The "
                        "destination list must be named too, since lists do "
                        "not exist across boards.")
    position: str = Field(
        "", description="Where in the destination list: 'top' or 'bottom'")


class ArchiveCardParams(BoardScoped):
    card: str = Field(..., description="Card to archive (name or id)")
    archived: bool = Field(
        True, description="Set false to restore an archived card")


class DeleteCardParams(BoardScoped):
    card: str = Field(..., description="Card to delete (name or id)")


class AddCommentParams(BoardScoped):
    card: str = Field(..., description="Card to comment on (name or id)")
    comment: str = Field(..., description="Comment text")


class CardMembersParams(BoardScoped):
    card: str = Field(..., description="Card to change (name or id)")
    members: str = Field(
        ..., description="Comma-separated member names, or 'me'")
    remove: bool = Field(
        False, description="Set true to REMOVE these members instead of adding")


class CardLabelsParams(BoardScoped):
    card: str = Field(..., description="Card to change (name or id)")
    labels: str = Field(
        ..., description="Comma-separated label names or colours")
    remove: bool = Field(
        False, description="Set true to REMOVE these labels instead of adding")


class CreateListParams(BoardScoped):
    name: str = Field(..., description="Name for the new list (column)")
    position: str = Field(
        "bottom", description="Where on the board: 'top' or 'bottom'")


class ArchiveListParams(BoardScoped):
    list_name: str = Field(..., description="List to archive (name or id)")
    archived: bool = Field(
        True, description="Set false to restore an archived list")


class CreateBoardParams(BaseModel):
    """Not BoardScoped: this CREATES the board, so there is none to act in."""
    name: str = Field(..., description="Name for the new board")
    desc: str = Field("", description="Board description")
    default_lists: bool = Field(
        True, description="Create Trello's default To Do / Doing / Done lists")


class CreateChecklistParams(BoardScoped):
    card: str = Field(..., description="Card to add the checklist to (name or id)")
    name: str = Field("Checklist", description="Checklist title")
    items: str = Field(
        "", description="Comma-separated items to add to the checklist")


class CheckItemParams(BoardScoped):
    card: str = Field(..., description="Card holding the checklist (name or id)")
    item: str = Field(..., description="Checklist item text to tick or untick")
    complete: bool = Field(
        True, description="Set false to untick the item")


# --------------------------- display base ---------------------------

class _Displayable(sdl.Entity):
    """Base for every entity here: fills the REQUIRED display fields.

    `sdl.Entity` requires `id` and `title`, and pydantic raises when a builder
    omits them -- which is correct, but 28 construction sites each repeating
    `title=name or "(unnamed x)"` is 28 chances to forget one and render a blank
    row. So the rule lives in ONE place: `title` falls back to whatever the
    entity calls its human name.

    The fallback order is deliberate. `name` first because that is what almost
    every Trello object has; then the fields used by the entities that have no
    `name` at all (an account is named by its member, a write result by what it
    acted on). `id` is NEVER used as a title -- a 24-character hex string is not
    a name, and showing one would look like a bug rather than a fallback.
    """

    id: str = ""
    title: str = ""

    @model_validator(mode="after")
    def _fill_title(self):
        if not self.title:
            for field in ("name", "account_name", "author", "detail"):
                value = getattr(self, field, "")
                if isinstance(value, str) and value.strip():
                    object.__setattr__(self, "title", value.strip())
                    break
        return self

# --------------------------- return entities ---------------------------

class ConnectResult(_Displayable):
    """Outcome of connecting credentials -- what got connected, and what next."""
    account_name: str = ""
    username: str = ""
    email: str = ""
    already_connected: bool = False
    board_count: int = 0
    boards: str = ""
    next_step: str = ""


class TrelloAccount(_Displayable):
    """One connected Trello account (one key/token pair)."""
    slot: int = 0
    account_name: str = ""
    username: str = ""
    email: str = ""
    boards: str = ""
    board_count: int = 0
    status: str = ""
    detail: str = ""


class TrelloAccountList(sdl.EntityList[TrelloAccount]):
    pass


class TrelloBoard(_Displayable):
    """One board reachable by a connected credential."""
    name: str = ""
    closed: bool = False
    account_name: str = ""
    url: str = ""


class TrelloBoardList(sdl.EntityList[TrelloBoard]):
    pass


class TrelloList(_Displayable):
    """One list (column) on a board."""
    name: str = ""
    closed: bool = False
    board: str = ""
    card_count: int = 0


class TrelloListList(sdl.EntityList[TrelloList]):
    pass


class TrelloCard(_Displayable):
    """One Trello card, flattened for display."""
    name: str = ""
    list_name: str = ""
    closed: bool = False
    due: str = ""
    due_complete: bool = False
    members: str = ""
    labels: str = ""
    desc: str = ""
    comment_count: int = 0
    checklist_summary: str = ""
    attachment_count: int = 0
    url: str = ""
    modified: str = ""
    summary: str = ""


class TrelloCardList(sdl.EntityList[TrelloCard]):
    pass


class TrelloComment(_Displayable):
    """One comment on a card.

    Trello has no comment resource: comments are ACTIONS of type `commentCard`,
    so `id` here is an action id and cannot be used where a card id is expected.
    """
    author: str = ""
    text: str = ""
    created: str = ""


class TrelloCommentList(sdl.EntityList[TrelloComment]):
    pass


class TrelloMember(_Displayable):
    """One person on a board."""
    name: str = ""
    username: str = ""


class TrelloMemberList(sdl.EntityList[TrelloMember]):
    pass


class TrelloLabel(_Displayable):
    """One label on a board.

    `name` is often EMPTY: Trello's six default labels are colour-only, which is
    why the colour is carried as a first-class field rather than a detail.
    """
    name: str = ""
    color: str = ""


class TrelloLabelList(sdl.EntityList[TrelloLabel]):
    pass


class TrelloChecklist(_Displayable):
    """One checklist on a card, with its items flattened."""
    name: str = ""
    items: str = ""
    done_count: int = 0
    total_count: int = 0


class TrelloChecklistList(sdl.EntityList[TrelloChecklist]):
    pass


class TrelloSearchHit(_Displayable):
    """One search result -- a card, board or member."""
    name: str = ""
    kind: str = ""
    board: str = ""
    url: str = ""


class TrelloSearchHitList(sdl.EntityList[TrelloSearchHit]):
    pass


class AccessReport(_Displayable):
    """What the connector can currently reach, and why anything is missing.

    The field set mirrors what `check_access` actually reports. It was
    originally written around a single board (list_count, can_write) while the
    handler reported across ACCOUNTS -- and pydantic drops undeclared keyword
    fields silently, so the report rendered blank with no error anywhere. Named
    for the facts the handler really has.
    """

    # No name of its own: this is a report, not a Trello object.
    title: str = "Trello access"
    accounts_configured: int = 0
    accounts_working: int = 0
    boards_reachable: int = 0
    detail: str = ""
    next_step: str = ""


class WriteResult(_Displayable):
    """Outcome of a write, phrased so the narrator can state what changed."""
    name: str = ""
    action: str = ""
    detail: str = ""
    url: str = ""
