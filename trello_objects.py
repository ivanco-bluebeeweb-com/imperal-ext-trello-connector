"""Trello object shapes: safe field access and flattening for display.

Trello's field names are its own and differ from every other tracker this
workspace connects to, so the mapping lives HERE rather than being spelled out
at each call site:

* `id`, not `gid` -- a 24-character hex MongoDB ObjectId.
* `desc`, not `notes` -- the card/board description.
* `closed` -- boards and lists use this for "archived"; cards use `closed` too.
* `idList` / `idBoard` -- parentage is by id reference, not a nested object.
* `due` / `dueComplete` -- a card's due date and whether it was ticked off.
* Comments are ACTIONS of type `commentCard`, with the text buried at
  `data.text`. There is no comment resource of its own.

Nothing here invents a value: a missing field yields "" or a false-y default so
a display never shows something Trello did not say.
"""

from __future__ import annotations

# A Trello id is a 24-char hex MongoDB ObjectId. Boards additionally have an
# 8-char "shortLink" used in their URL, which is NOT accepted where an id is
# expected -- so recognising an id has to be strict about both length and
# alphabet, or a shortLink would be forwarded as an id and 404.
_HEX = set("0123456789abcdefABCDEF")


def looks_like_id(value: str) -> bool:
    """True when the string is plausibly a Trello object id.

    Guard, not validation: it decides whether to skip the name lookup, so it is
    deliberately strict about shape (24 hex chars) and never invents an id.
    """
    if not value:
        return False
    raw = value.strip()
    return len(raw) == 24 and all(ch in _HEX for ch in raw)


def id_of(obj) -> str:
    """The object's id, or "" -- never a guess."""
    if isinstance(obj, dict):
        return str(obj.get("id") or "")
    return ""


def name_of(obj) -> str:
    """The object's display name.

    Cards and boards use `name`; a member uses `fullName` with `username` as the
    fallback, because a member with no full name set would otherwise render as
    an empty string.
    """
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("name")
               or obj.get("fullName")
               or obj.get("username")
               or "")


def text_of(obj, key: str) -> str:
    """A string field, normalised to "" when absent or null."""
    if not isinstance(obj, dict):
        return ""
    value = obj.get(key)
    return "" if value is None else str(value)


def board_url(obj) -> str:
    """A board's or card's web URL. Trello supplies it; this never builds one."""
    return text_of(obj, "shortUrl") or text_of(obj, "url")


def label_names(obj) -> str:
    """Card labels as a readable string.

    A Trello label may have NO name -- colour-only labels are normal and common
    -- so an unnamed label is rendered by its colour instead of vanishing.
    """
    if not isinstance(obj, dict):
        return ""
    out: list[str] = []
    for label in (obj.get("labels") or []):
        if not isinstance(label, dict):
            continue
        name = str(label.get("name") or "").strip()
        colour = str(label.get("color") or "").strip()
        if name:
            out.append(name)
        elif colour:
            out.append(f"({colour})")
    return ", ".join(out)


def member_names(obj, key: str = "members") -> str:
    """Names of members embedded in an object, comma-separated."""
    if not isinstance(obj, dict):
        return ""
    names = [name_of(m) for m in (obj.get(key) or []) if isinstance(m, dict)]
    return ", ".join(n for n in names if n)


def checklist_summary(obj) -> str:
    """Checklist progress as "3/7", from the card's badges.

    Trello puts this in `badges.checkItemsChecked` / `badges.checkItems`, which
    only arrive when badges are requested -- so an absent badges block yields ""
    rather than a misleading "0/0".
    """
    if not isinstance(obj, dict):
        return ""
    badges = obj.get("badges")
    if not isinstance(badges, dict):
        return ""
    total = badges.get("checkItems")
    done = badges.get("checkItemsChecked")
    if not isinstance(total, int) or not isinstance(done, int) or total == 0:
        return ""
    return f"{done}/{total}"


def comment_text(action) -> str:
    """The text of a `commentCard` action.

    Trello has no comment resource: a comment is an ACTION whose `type` is
    `commentCard` and whose text lives at `data.text`. Reading `action["text"]`
    -- the obvious guess -- returns nothing at all.
    """
    if not isinstance(action, dict):
        return ""
    data = action.get("data")
    if isinstance(data, dict):
        return str(data.get("text") or "")
    return ""


def is_comment(action) -> bool:
    """Whether an action is a user comment rather than a system event."""
    if not isinstance(action, dict):
        return False
    return str(action.get("type") or "") == "commentCard"


def actor_name(action) -> str:
    """Who performed an action."""
    if not isinstance(action, dict):
        return ""
    creator = action.get("memberCreator")
    return name_of(creator) if isinstance(creator, dict) else ""


def comment_author(action) -> str:
    """Who wrote a comment.

    A thin alias over `actor_name`: at the call site "author of this comment"
    and "actor of this action" are the same field, but a comment handler that
    has to reach for a word about ACTIONS leaks Trello's internal shape into
    code that is talking about comments.
    """
    return actor_name(action)


def created_at(obj) -> str:
    """When an action or object was created.

    Trello returns `date` on an action and `dateLastActivity` on a card, so both
    are tried in that order. Never synthesised: a missing date yields "", which
    a display can render as blank rather than as a wrong timestamp.
    """
    if not isinstance(obj, dict):
        return ""
    for field in ("date", "dateLastActivity", "dateCreated"):
        value = obj.get(field)
        if value:
            return str(value)
    return ""


def checkitem_counts(checklist) -> tuple[int, int]:
    """(done, total) for one checklist.

    Trello does NOT return a done/total pair on a checklist -- it returns the
    items, each with its own `state` of `complete` or `incomplete`. So the
    counts are derived here rather than read from a field that does not exist.
    """
    if not isinstance(checklist, dict):
        return 0, 0
    items = checklist.get("checkItems")
    if not isinstance(items, list):
        return 0, 0
    total = len(items)
    done = sum(1 for i in items
               if isinstance(i, dict) and i.get("state") == "complete")
    return done, total


def checkitem_lines(checklist) -> str:
    """The checklist's items as readable lines, ticked or not.

    Rendered as text rather than a nested structure because the entity that
    carries it is a flat display row -- and a user reading a checklist wants to
    see which items are done, not a list of ids.
    """
    if not isinstance(checklist, dict):
        return ""
    items = checklist.get("checkItems")
    if not isinstance(items, list):
        return ""
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mark = "x" if item.get("state") == "complete" else " "
        lines.append(f"[{mark}] {name_of(item)}")
    return "\n".join(lines)


def card_summary(card) -> str:
    """One-line summary of a card: the facts a human scans for.

    Only states what Trello reported. `dueComplete` is honoured because a card
    with a past due date that was already ticked is NOT overdue, and calling it
    overdue would be a false alarm.
    """
    if not isinstance(card, dict):
        return ""
    bits: list[str] = []
    due = text_of(card, "due")
    if due:
        if card.get("dueComplete"):
            bits.append(f"due {due[:10]} (done)")
        else:
            bits.append(f"due {due[:10]}")
    members = member_names(card)
    if members:
        bits.append(f"with {members}")
    labels = label_names(card)
    if labels:
        bits.append(f"labels: {labels}")
    checklists = checklist_summary(card)
    if checklists:
        bits.append(f"checklist {checklists}")
    if card.get("closed"):
        bits.append("archived")
    return " · ".join(bits)


# Field sets requested explicitly. Trello returns a LARGE default object for
# cards and boards; naming fields keeps responses small and, more importantly,
# makes it obvious which facts the display depends on.
CARD_FIELDS = ("id,name,desc,closed,due,dueStart,dueComplete,idList,idBoard,"
               "idMembers,labels,shortUrl,url,dateLastActivity,pos")
BOARD_FIELDS = ("id,name,desc,closed,url,shortUrl,shortLink,idOrganization,"
                "dateLastActivity,prefs")
LIST_FIELDS = "id,name,closed,idBoard,pos"
# `fullName` is the human name and `username` the @handle; Trello returns the
# two in separate fields and a member display needs both, so they are requested
# together everywhere rather than one being inferred from the other.
MEMBER_FIELDS = "id,fullName,username"
