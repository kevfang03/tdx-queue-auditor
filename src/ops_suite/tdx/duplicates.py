"""Possible duplicate tickets, for the shift summary's rows. Pure functions.

Two unclaimed tickets are possible duplicates when the requester is the same person
(``same_person``) and the titles are similar: a token Jaccard of at least the threshold after
lower-casing, dropping punctuation and a few stop words, or one title containing the other.
The newer ticket points at the oldest similar one.

Event tickets and event request forms are left out: form tickets all share the form's title,
and a renamed event's title is mostly its date and room, so two events by one requester look
alike without being duplicates.

Personal data rule: names and titles are compared in memory only. The result is ticket ids;
nothing about it is logged, stored or shown except "(possible duplicate of #<id>)".
"""

import re
from collections.abc import Sequence
from datetime import UTC

from ops_suite.tdx.events import is_event_title
from ops_suite.tdx.names import same_person
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import is_form_title

_APOSTROPHES = re.compile(r"['’]")
_NOT_WORD = re.compile(r"[^\w]+")
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "with",
        "from",
        "my",
        "is",
        "it",
        "i",
        "re",
        "fw",
        "fwd",
        "please",
    }
)


def title_tokens(title: str) -> tuple[str, ...]:
    """The title's words, lower-cased, without punctuation or stop words, in order."""
    text = _APOSTROPHES.sub("", title.casefold())
    words = _NOT_WORD.sub(" ", text).replace("_", " ").split()
    return tuple(word for word in words if word not in STOP_WORDS)


def similar_titles(first: str, second: str, threshold: float) -> bool:
    """A token Jaccard of at least ``threshold``, or one title's words inside the other's."""
    a, b = title_tokens(first), title_tokens(second)
    if not a or not b:
        return False
    joined_a, joined_b = f" {' '.join(a)} ", f" {' '.join(b)} "
    if joined_a in joined_b or joined_b in joined_a:
        return True
    set_a, set_b = set(a), set(b)
    return len(set_a & set_b) / len(set_a | set_b) >= threshold


def _older_first(row: TicketRow) -> tuple[object, ...]:
    """TDX ids grow with each new ticket, so the id breaks ties and stands in for a missing
    Created time."""
    created = row.created.astimezone(UTC).timestamp() if row.created is not None else None
    return (created is None, created or 0.0, row.ticket_id)


def possible_duplicates(
    rows: Sequence[TicketRow], threshold: float, form_titles: Sequence[str] = ()
) -> dict[int, int]:
    """Ticket id -> the id of the oldest earlier unclaimed ticket by the same requester with a
    similar title. Only the newer ticket of a pair is a key."""
    seen: set[int] = set()
    candidates: list[TicketRow] = []
    for row in rows:
        if row.ticket_id in seen or row.widget != "unclaimed" or not row.requestor.strip():
            continue
        if is_event_title(row.title) or is_form_title(row.title, form_titles):
            continue
        seen.add(row.ticket_id)
        candidates.append(row)
    candidates.sort(key=_older_first)
    marks: dict[int, int] = {}
    for index, row in enumerate(candidates):
        for older in candidates[:index]:
            if same_person(row.requestor, older.requestor) and similar_titles(
                row.title, older.title, threshold
            ):
                marks[row.ticket_id] = older.ticket_id
                break
    return marks
