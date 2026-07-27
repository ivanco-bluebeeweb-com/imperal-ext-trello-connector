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


# --- attachments ---
# Trello attachments come in two shapes that share one endpoint: a URL, and an
# uploaded file. Only the URL form is offered here. `POST /cards/{id}/attachments`
# takes a file as multipart/form-data, and every request in this app goes through
# one client that sends JSON -- so a `file` parameter would be a field that
# accepts a path and then cannot deliver it. A named gap beats a broken promise.

class ListAttachmentsParams(BoardScoped):
    card: str = Field(..., description="Card whose attachments to read (name or id)")


class AddAttachmentParams(BoardScoped):
    card: str = Field(..., description="Card to attach to (name or id)")
    url: str = Field(
        ..., description="Link to attach, e.g. a Google Doc or an image URL")
    name: str = Field(
        "", description="Label for the link (defaults to the URL itself)")


class DeleteAttachmentParams(BoardScoped):
    card: str = Field(..., description="Card holding the attachment (name or id)")
    attachment: str = Field(
        ..., description="Attachment to remove, by its name or id")


# --- comments ---

class EditCommentParams(BoardScoped):
    card: str = Field(..., description="Card holding the comment (name or id)")
    comment_id: str = Field(
        ..., description="Id of the comment to change -- from list_comments. A "
                         "comment is an ACTION, so this is an action id.")
    text: str = Field(..., description="Replacement comment text")


class DeleteCommentParams(BoardScoped):
    card: str = Field(..., description="Card holding the comment (name or id)")
    comment_id: str = Field(
        ..., description="Id of the comment to delete -- from list_comments.")


# --- checklists ---

class AddCheckItemParams(BoardScoped):
    card: str = Field(..., description="Card holding the checklist (name or id)")
    item: str = Field(..., description="Text of the item to add")
    checklist: str = Field(
        "", description="Which checklist to add to. Omit when the card has "
                        "only one.")
    position: str = Field(
        "bottom", description="Where in the checklist: 'top' or 'bottom'")


class DeleteCheckItemParams(BoardScoped):
    card: str = Field(..., description="Card holding the checklist (name or id)")
    item: str = Field(..., description="Text of the item to delete")


class UpdateChecklistParams(BoardScoped):
    card: str = Field(..., description="Card holding the checklist (name or id)")
    checklist: str = Field(..., description="Checklist to rename (name or id)")
    name: str = Field(..., description="New checklist title")


class DeleteChecklistParams(BoardScoped):
    card: str = Field(..., description="Card holding the checklist (name or id)")
    checklist: str = Field(
        ..., description="Checklist to delete (name or id). Its items go with "
                         "it.")


# --- lists (columns) ---

class UpdateListParams(BoardScoped):
    list_name: str = Field(..., description="List to change (name or id)")
    name: str = Field("", description="New list name")
    position: str = Field(
        "", description="Move it: 'top', 'bottom' or a number")
    subscribed: bool | None = Field(
        None, description="Follow (true) or unfollow (false) this list")


class ListBulkParams(BoardScoped):
    list_name: str = Field(..., description="Source list (name or id)")
    to_list: str = Field(
        "", description="Destination list for a move. Omit when archiving.")
    to_board: str = Field(
        "", description="Destination board, when moving cards to another board")


# --- boards ---

class UpdateBoardParams(BaseModel):
    """Not BoardScoped: the board is the OBJECT here, not the context.

    `board` is required rather than optional, unlike BoardScoped's convenience
    default. Renaming or closing a board is not something to do to whichever
    board happened to be the only one reachable.
    """
    board: str = Field(..., description="Board to change (name or id)")
    name: str = Field("", description="New board name")
    desc: str = Field("", description="New board description")
    closed: bool | None = Field(
        None, description="Close/archive (true) or reopen (false) the board")


class DeleteBoardParams(BaseModel):
    """Deleting a board is permanent -- so the name is required, never inferred."""
    board: str = Field(..., description="Board to delete permanently (name or id)")
    confirm: bool = Field(
        False, description="Must be true. Deleting a board destroys every list, "
                           "card and comment on it, and Trello offers no undo.")


class BoardMemberParams(BaseModel):
    board: str = Field(..., description="Board to change (name or id)")
    member: str = Field(
        ..., description="Person to add or remove: email address, username, or "
                         "a name already on the board")
    role: str = Field(
        "normal", description="'normal', 'admin' or 'observer'")
    remove: bool = Field(
        False, description="Set true to REMOVE this person from the board")


# --- labels ---

class CreateLabelParams(BoardScoped):
    name: str = Field(..., description="Label text, e.g. 'Blocked'")
    color: str = Field(
        "green", description="Trello colour: green, yellow, orange, red, "
                             "purple, blue, sky, lime, pink, black")


class UpdateLabelParams(BoardScoped):
    label: str = Field(..., description="Label to change (name, colour or id)")
    name: str = Field("", description="New label text")
    color: str = Field("", description="New colour")


class DeleteLabelParams(BoardScoped):
    label: str = Field(..., description="Label to delete (name, colour or id)")


# --- cards ---

class CopyCardParams(BoardScoped):
    card: str = Field(..., description="Card to copy (name or id)")
    name: str = Field("", description="Title for the copy (defaults to the original)")
    to_list: str = Field(
        "", description="List to put the copy in. Omit to copy beside the "
                        "original.")
    to_board: str = Field(
        "", description="Copy onto a different board (name or id). The "
                        "destination list must be named too, since lists "
                        "belong to one board.")
    position: str = Field(
        "bottom", description="Where in the destination list: 'top' or 'bottom'")
    keep: str = Field(
        "all", description="What to carry over: 'all', 'none', or a "
                           "comma-separated pick of checklists, attachments, "
                           "comments, due, labels, members, stickers")


# --- custom fields ---
# The VALUE SHAPE DEPENDS ON THE FIELD TYPE, verified against Trello's custom
# fields guide: {"value": {"text"|"number"|"date"|"checked": "..."}} for the
# scalar types, and {"idValue": "<option id>"} for a dropdown. Every one is sent
# as a STRING, including number and checked. One `value` parameter is offered
# here and coerced to the right shape from the field's own declared type, because
# asking the user which JSON key Trello wants is asking them to know the API.

class ListCustomFieldsParams(BoardScoped):
    card: str = Field(
        "", description="Also show this card's current values (name or id). "
                        "Omit to list just the field definitions.")


class CreateCustomFieldParams(BoardScoped):
    name: str = Field(..., description="Field name shown on the card, e.g. 'Priority'")
    field_type: str = Field(
        "text", description="One of: text, number, date, checkbox, list "
                            "(list = a dropdown, which needs options)")
    options: str = Field(
        "", description="Comma-separated dropdown choices, for a 'list' field")
    show_on_card: bool = Field(
        True, description="Show the field on the front of the card")


class DeleteCustomFieldParams(BoardScoped):
    field: str = Field(..., description="Custom field to delete (name or id)")
    confirm: bool = Field(
        False, description="Must be true. Deleting the field also deletes its "
                           "value on EVERY card on the board, with no undo.")


class SetCustomFieldParams(BoardScoped):
    card: str = Field(..., description="Card to set the value on (name or id)")
    field: str = Field(..., description="Custom field (name or id)")
    value: str = Field(
        "", description="The value. For a dropdown, the option's text. For a "
                        "checkbox, 'true' or 'false'. For a date, "
                        "'YYYY-MM-DD'. Leave empty with clear=true to erase.")
    clear: bool = Field(
        False, description="Erase the value on this card instead of setting one")


class CustomFieldOptionParams(BoardScoped):
    field: str = Field(..., description="Dropdown custom field (name or id)")
    option: str = Field(..., description="Option text to add or remove")
    remove: bool = Field(
        False, description="Set true to REMOVE this option instead of adding it")


# --- stickers and votes ---

class StickerParams(BoardScoped):
    card: str = Field(..., description="Card to put the sticker on (name or id)")
    sticker: str = Field(
        ..., description="Sticker name, e.g. 'thumbsup', 'star', 'heart', "
                         "'check', 'clock', 'rocketship'")
    remove: bool = Field(
        False, description="Set true to REMOVE this sticker from the card")


class VoteParams(BoardScoped):
    card: str = Field(..., description="Card to vote on (name or id)")
    member: str = Field(
        "me", description="Who votes -- defaults to the connected account")
    remove: bool = Field(
        False, description="Set true to take the vote back")


# --- workspaces (organizations) ---
# Trello calls them ORGANIZATIONS in the API and WORKSPACES in the UI. The tools
# use the word the user sees; the routes use the word Trello's API uses.

class ListWorkspacesParams(BaseModel):
    refresh: bool = Field(
        False, description="Re-read from Trello instead of the cache")


class CreateWorkspaceParams(BaseModel):
    name: str = Field(..., description="Workspace name, e.g. 'Acme Studio'")
    desc: str = Field("", description="What the workspace is for")
    website: str = Field("", description="Workspace website URL")


class UpdateWorkspaceParams(BaseModel):
    workspace: str = Field(..., description="Workspace to change (name or id)")
    name: str = Field("", description="New name")
    desc: str = Field("", description="New description")
    website: str = Field("", description="New website URL")


class DeleteWorkspaceParams(BaseModel):
    workspace: str = Field(..., description="Workspace to delete (name or id)")
    confirm: bool = Field(
        False, description="Must be true. Deleting a workspace is permanent.")


class WorkspaceMemberParams(BaseModel):
    workspace: str = Field(..., description="Workspace to change (name or id)")
    member: str = Field(
        ..., description="Person to add or remove: email, username or full name")
    role: str = Field("normal", description="'normal' or 'admin'")
    remove: bool = Field(
        False, description="Set true to REMOVE them from the workspace")


# --- board copy, list move, activity ---

class CopyBoardParams(BaseModel):
    # Named `board` like every other tool in this connector. It was `source`,
    # which read fine in isolation and wrongly everywhere else: one tool asking
    # for `source` while forty-four ask for `board` is a trap for the caller.
    board: str = Field(..., description="Board to copy (name or id)")
    name: str = Field(..., description="Name for the new board")
    workspace: str = Field(
        "", description="Workspace to create the copy in (name or id)")
    keep_cards: bool = Field(
        True, description="Copy the cards too. False copies only the columns.")


class MoveListParams(BoardScoped):
    list_name: str = Field(..., description="List (column) to move (name or id)")
    to_board: str = Field(..., description="Destination board (name or id)")


class ListActivityParams(BoardScoped):
    card: str = Field(
        "", description="Only this card's activity (name or id). Omit for the "
                        "whole board.")
    limit: int = Field(
        20, ge=1, le=100, description="How many recent entries to return")


class ListNotificationsParams(BaseModel):
    unread_only: bool = Field(
        True, description="Only unread notifications")
    limit: int = Field(
        20, ge=1, le=100, description="How many to return")
    mark_read: bool = Field(
        False, description="Mark ALL notifications read instead of listing them")


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


class TrelloAttachment(_Displayable):
    """One attachment on a card.

    `is_upload` distinguishes a file Trello stores from a link that merely points
    somewhere: deleting the first destroys the only copy, deleting the second
    only removes a reference. That difference matters enough to be a field.
    """
    name: str = ""
    url: str = ""
    mime_type: str = ""
    bytes_size: int = 0
    is_upload: bool = False
    created: str = ""


class TrelloAttachmentList(sdl.EntityList[TrelloAttachment]):
    pass


class TrelloCustomField(_Displayable):
    """One custom field definition, plus this card's value when asked for one.

    `field_type` is carried because it DECIDES the write shape: a number goes in
    as {"number": "42"}, a dropdown as an option id. A caller that cannot see the
    type cannot set the value correctly.
    """
    name: str = ""
    field_type: str = ""
    options: list[str] = []
    value: str = ""
    shown_on_card: bool = False


class TrelloCustomFieldList(sdl.EntityList[TrelloCustomField]):
    pass


class TrelloSticker(_Displayable):
    name: str = ""
    image: str = ""


class TrelloStickerList(sdl.EntityList[TrelloSticker]):
    pass


class TrelloWorkspace(_Displayable):
    """A Trello workspace -- `organization` in the API, workspace in the UI."""
    name: str = ""
    display_name: str = ""
    desc: str = ""
    website: str = ""
    board_count: int = 0
    member_count: int = 0


class TrelloWorkspaceList(sdl.EntityList[TrelloWorkspace]):
    pass


class TrelloActivity(_Displayable):
    """One entry from a board's or card's action history."""
    action: str = ""
    member: str = ""
    created: str = ""
    summary: str = ""


class TrelloActivityList(sdl.EntityList[TrelloActivity]):
    pass


class TrelloNotification(_Displayable):
    kind: str = ""
    member: str = ""
    created: str = ""
    unread: bool = False
    summary: str = ""


class TrelloNotificationList(sdl.EntityList[TrelloNotification]):
    pass


class TrelloSearchHit(_Displayable):
    """One search result -- a card, board or member."""
    name: str = ""
    kind: str = ""
    board: str = ""
    url: str = ""


class TrelloSearchHitList(sdl.EntityList[TrelloSearchHit]):
    pass


class GetTokenLinkParams(BaseModel):
    """Just the key -- the one thing the user definitely has.

    Not BoardScoped and no token field: this runs BEFORE a token exists. Asking
    for one here would be the circular request that made the Connect screen
    unusable -- a field the user had no way to fill.
    """
    key: str = Field(
        "", description="Your Trello API key -- 32 hex characters, from the "
                        "API Key tab of your Power-Up at trello.com/apps/admin.")


class TokenLink(_Displayable):
    """A ready-made authorize link, plus whether the key behind it is real.

    The whole point is that the user does not have to find anything on
    Trello's page: they open this link, click Allow, and Trello hands them the
    token. `key_status` is checked first so a dead key is caught here rather
    than after a pointless round trip through the Allow prompt.
    """
    authorize_url: str = ""
    key_status: str = ""
    expiration: str = ""
    scope: str = ""
    next_step: str = ""


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
