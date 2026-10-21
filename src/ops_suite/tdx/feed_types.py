"""Types shared by the feed reader (``feed.py``) and the stale rules (``rules.py``).

Both sides depend on these shapes, so a change here is a change to both: update them together.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

EntryKind = Literal["reply", "private", "system"]


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One activity-feed entry. ``author`` is student data: never log or print it.

    kind: "reply" is visible to the requestor (a comment or an email reply), "private" is a
    private note, "system" is an entry with no visible comment (for example a status change).
    """

    author: str
    created: datetime | None
    kind: EntryKind

    def __repr__(self) -> str:
        when = self.created.isoformat() if self.created else None
        return f"FeedEntry(kind={self.kind!r}, created={when!r})"


@dataclass(frozen=True, slots=True)
class TicketFeed:
    """A ticket's activity feed, newest entry first.

    ``desk_authors``: people the feed itself shows to be staff beyond private-note authors:
    whoever wrote a merge notice or reassignment, and the author of a visible
    reply whose own text holds a desk signature, never the requester. Student data, like
    ``author``.

    ``latest_reply_text``: the comment text of the newest "reply" entry only, in
    memory, for the follow-up classifier. Student data: never log, print, store or put it in
    an exception. ``repr`` leaves it out.
    """

    ticket_id: int
    entries: tuple[FeedEntry, ...]
    unreadable: int = 0
    desk_authors: tuple[str, ...] = ()
    latest_reply_text: str | None = None

    def __repr__(self) -> str:
        return f"TicketFeed(ticket_id={self.ticket_id}, entries={len(self.entries)})"
