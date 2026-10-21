"""A ticket's activity feed from the HTML of its page's frames. Pure functions; no Playwright.

The selectors follow docs/tdx-page-structure.md, "Ticket page and feed": only class names and
roles the ticket capture kept unmasked, never ids, and never page text except the TDX interface
phrase of a field change ("Changed Status from ... to ..."), which is only tested, never kept.

Private notes: an entry is private when its private marker (``span.feed-private``) is
*rendered*. The page may carry the marker on entries where it is hidden, which the HTML alone
cannot show, so the browser side passes one visibility flag per marker
(``private_marker_visible``). Without that list, a marker that is present counts, which is how
the synthetic fixture tests run.

Personal data rule: authors stay inside ``FeedEntry`` objects in memory, and comment text is
read only to tell whether any is there, except the newest reply's, which is kept in memory as
``TicketFeed.latest_reply_text`` for the follow-up classifier, and each reply's, which is
tested for a desk signature and dropped. None of it goes into an
exception message, a log record or ``repr()``. An entry that cannot be read leaves
this module only as a structural shape.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag

from ops_suite.tdx.feed_types import EntryKind, FeedEntry, TicketFeed
from ops_suite.tdx.followup import strip_quoted
from ops_suite.tdx.names import same_person
from ops_suite.tdx.rows import date_shape, parse_tdx_datetime

FeedState = Literal["absent", "loading", "entries", "empty"]

# --- selectors (see docs/tdx-page-structure.md) ------------------------------------------
FEED_ROOT_SELECTOR = ".feed"
ENTRY_ROLE = "group"  # the entry's own class is masked in the capture
BODY_CLASS = "feed-body"
HEADING_CLASS = "feed-heading"
AUTHOR_CLASS = "feed-participant-name"
PRIVATE_CLASS = "feed-private"
PRIVATE_SELECTOR = f".{PRIVATE_CLASS}"
CONTEXT_CLASS = "feed-context"
TIMESTAMP_SELECTOR = ".feed-timestamp"
REPLY_CLASS = "feed-reply"
REPLY_CONTEXT_CLASS = "reply-context"
INDICATOR_SELECTOR = ".refresh-indicator"
# Children of .feed-body that are not the comment; the comment's own classes are masked.
NOT_COMMENT_CLASSES = frozenset(
    {HEADING_CLASS, "feed-breadcrumbs", CONTEXT_CLASS, "feed-actions", REPLY_CLASS}
)
NOT_REPLY_COMMENT_CLASSES = frozenset({AUTHOR_CLASS, REPLY_CONTEXT_CLASS, PRIVATE_CLASS})
# Inside a comment: attachment blocks and quoted email history do not count as comment text.
SKIPPED_SELECTOR = 'blockquote, [class*="attachment"], [class*="quote"]'
HIDDEN_CLASSES = frozenset({"hidden", "ng-hide", "d-none"})
# Fixed TDX interface phrases that open an entry's own comment (case-insensitive, after
# trimming, quoted history left out), as they appear on live tickets. Both kinds
# are "system" entries, whatever their private marker says:
# - a merge notice, "Merged incident <id> (<title>, originally created by <name>):", followed
#   by the merged ticket's whole thread. It stays ONE entry;
# - a reassignment, "Reassigned this incident from <group> to <group>.".
# The author of either counts as desk staff: only staff merge or reassign tickets.
MERGE_NOTE_PREFIXES: tuple[str, ...] = ("Merged incident",)
SYSTEM_NOTE_PREFIXES: tuple[str, ...] = ("Reassigned this incident from",)

# The TDX interface phrase of a field change, tested on one line of a comment that also holds
# bold text: "Changed Status from <b>New</b> to <b>In Process</b>."
_FIELD_CHANGE = re.compile(r"changed\b.*\bfrom\b.*\bto\b", re.IGNORECASE | re.DOTALL)
_BOLD_MARK = "\x00"
_LINE_MARK = "\x01"
_BLOCK_TAGS = ["div", "p", "li", "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4"]
_WORD = re.compile(r"\w")
_SPACES = re.compile(r"\s+")
# The newest reply's text is cut at this many characters (the classifier caps it lower).
REPLY_TEXT_MAX_CHARS = 20_000


# Keys of FeedDetails.marker_stats (and of FeedRead.marker_stats, summed over a run).
MARKER_STAT_KEYS: tuple[str, ...] = (
    "entries",  # entry blocks read, replies to entries included
    "nested",  # of those, replies to an entry (.feed-reply)
    "private_present",  # blocks with a private marker of their own in the HTML
    "private_visible",  # blocks whose own marker is rendered (= present without a list)
    "inherited_private",  # replies private only through their parent's visible marker
    "visibility_fallback",  # feeds read by marker presence because the list did not fit
    "merge_notes",  # entries that open with a MERGE_NOTE_PREFIXES phrase
    "reassignments",  # entries that open with a SYSTEM_NOTE_PREFIXES phrase
    "signature_desk_replies",  # replies whose own text holds a desk signature
)


def empty_marker_stats() -> dict[str, int]:
    return dict.fromkeys(MARKER_STAT_KEYS, 0)


@dataclass(frozen=True, slots=True)
class FeedDetails:
    """A parsed feed plus what may be shown about it: counts and structural shapes only."""

    feed: TicketFeed
    unreadable_shapes: tuple[str, ...]  # "no author", or "undated: <date shape>"
    marker_stats: Mapping[str, int] = field(default_factory=empty_marker_stats)


@dataclass(frozen=True, slots=True)
class _Entry:
    author: str
    created: datetime | None
    kind: EntryKind
    # A reply's comment parts, read for text only if it turns out to be the newest reply.
    parts: tuple[Tag | NavigableString, ...] = ()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _classes(tag: Tag) -> list[str]:
    value = tag.get("class") or []
    return list(value) if isinstance(value, list) else str(value).split()


def _has_class(tag: Tag, name: str) -> bool:
    return name in _classes(tag)


def _children(tag: Tag) -> list[Tag]:
    return [child for child in tag.children if isinstance(child, Tag)]


def _child_with_class(tag: Tag, name: str) -> Tag | None:
    return next((child for child in _children(tag) if _has_class(child, name)), None)


def _collapse(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


# --- the feed root and its state ------------------------------------------------------------


def _entries(root: Tag) -> list[tuple[Tag, Tag]]:
    """(entry, its .feed-body) for each top-level entry of the feed, in page order."""
    found: list[tuple[Tag, Tag]] = []
    for element in root.find_all(attrs={"role": ENTRY_ROLE}):
        body = _child_with_class(element, BODY_CLASS)
        if body is not None:
            found.append((element, body))
    return found


def _is_hidden(tag: Tag, root: Tag) -> bool:
    node: Tag | None = tag
    while node is not None:
        style = str(node.get("style", "")).replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if node.has_attr("hidden") or HIDDEN_CLASSES.intersection(_classes(node)):
            return True
        if node is root:
            return False
        node = node.parent if isinstance(node.parent, Tag) else None
    return False


def _roots(soup: BeautifulSoup) -> list[Tag]:
    """Elements of class ``feed`` that hold entries or the feed's refresh indicator."""
    return [
        root
        for root in soup.select(FEED_ROOT_SELECTOR)
        if _entries(root) or root.select_one(INDICATOR_SELECTOR) is not None
    ]


def _state(root: Tag) -> FeedState:
    if _entries(root):
        return "entries"
    indicators = root.select(INDICATOR_SELECTOR)
    if any(not _is_hidden(indicator, root) for indicator in indicators):
        return "loading"
    return "empty"


def feed_state(html: str) -> FeedState:
    """``absent`` (no feed in this frame), ``loading``, ``entries`` or ``empty``."""
    roots = _roots(_soup(html))
    if not roots:
        return "absent"
    states = [_state(root) for root in roots]
    for state in ("entries", "empty"):
        if state in states:
            return state
    return "loading"


def feed_ready(html: str) -> bool:
    """The frame shows a loaded feed (entries, or none with the indicator hidden)."""
    return feed_state(html) in ("entries", "empty")


# --- comments ----------------------------------------------------------------------------


def _comment_lines(parts: Sequence[Tag | NavigableString]) -> list[tuple[str, bool]]:
    """The comment's lines as (text with whitespace collapsed, is a TDX field-change line).

    Attachments and quoted history are removed first. The parts are re-parsed, so the caller's
    tree is not changed.
    """
    html = "".join(str(part) for part in parts)
    soup = _soup(html)
    for skipped in soup.select(SKIPPED_SELECTOR):
        skipped.decompose()
    for bold in soup.find_all(["b", "strong"]):
        bold.insert_before(_BOLD_MARK)
    for line_break in soup.find_all("br"):
        line_break.replace_with(_LINE_MARK)
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before(_LINE_MARK)
        block.insert_after(_LINE_MARK)
    lines = []
    for line in soup.get_text().split(_LINE_MARK):
        text = _collapse(line.replace(_BOLD_MARK, ""))
        lines.append((text, _BOLD_MARK in line and bool(_FIELD_CHANGE.match(text))))
    return lines


def _has_visible_comment(parts: Sequence[Tag | NavigableString]) -> bool:
    """True if the parts hold comment text other than field-change lines."""
    return any(_WORD.search(text) and not change for text, change in _comment_lines(parts))


def _comment_text(parts: Sequence[Tag | NavigableString]) -> str | None:
    """The comment element's text, one line per rendered line, blank lines and TDX
    field-change lines left out, cut at REPLY_TEXT_MAX_CHARS. None without a comment element
    or text. Student data: the caller keeps it in memory only."""
    elements = [part for part in parts if isinstance(part, Tag)]
    if not elements:
        return None
    lines = [text for text, change in _comment_lines(elements) if text and not change]
    text = "\n".join(lines)[:REPLY_TEXT_MAX_CHARS].strip()
    return text or None


def signature_phrases(signatures: Sequence[str]) -> tuple[str, ...]:
    """The configured desk signatures as matched: whitespace collapsed, case folded."""
    return tuple(folded for phrase in signatures if (folded := _collapse(phrase).casefold()))


def _has_signature(parts: Sequence[Tag | NavigableString], phrases: Sequence[str]) -> bool:
    """Whether the comment's own text, quoted mail cut first, holds one of ``phrases``.

    Only the answer leaves this function; the text is dropped. The sign-off is kept, because
    the signature comes after it.
    """
    if not phrases:
        return False
    text = _comment_text(parts)
    if text is None:
        return False
    own = _collapse(strip_quoted(text, keep_sign_off=True)).casefold()
    return any(phrase in own for phrase in phrases)


def _comment_parts(container: Tag, excluded: frozenset[str]) -> list[Tag | NavigableString]:
    parts: list[Tag | NavigableString] = []
    for child in container.children:
        if isinstance(child, Tag):
            if not excluded.intersection(_classes(child)):
                parts.append(child)
        elif isinstance(child, NavigableString) and type(child) is NavigableString:
            parts.append(child)  # plain text, not a comment or a doctype
    return parts


def _kind(private: bool, parts: Sequence[Tag | NavigableString]) -> EntryKind:
    if private:
        return "private"
    return "reply" if _has_visible_comment(parts) else "system"


NoteKind = Literal["merge", "reassignment"]


def _note_kind(parts: Sequence[Tag | NavigableString]) -> NoteKind | None:
    """Which fixed TDX phrase the comment opens with, if any. Only tested, never kept.

    Quoted history and attachments are left out first, so a phrase inside a quoted or
    carried-over message never counts.
    """
    soup = _soup("".join(str(part) for part in parts))
    for skipped in soup.select(SKIPPED_SELECTOR):
        skipped.decompose()
    folded = _collapse(soup.get_text(" ")).casefold()
    if any(folded.startswith(prefix.casefold()) for prefix in MERGE_NOTE_PREFIXES):
        return "merge"
    if any(folded.startswith(prefix.casefold()) for prefix in SYSTEM_NOTE_PREFIXES):
        return "reassignment"
    return None


# --- entries -----------------------------------------------------------------------------


def _timestamp_text(container: Tag | None) -> str | None:
    if container is None:
        return None
    stamp = container.select_one(TIMESTAMP_SELECTOR)
    if stamp is None:
        return None
    first = stamp.find("span")
    return _collapse((first or stamp).get_text(" "))


def _own_timestamp(body: Tag) -> str | None:
    for child in _children(body):
        if _has_class(child, CONTEXT_CLASS) and child.select_one(TIMESTAMP_SELECTOR) is not None:
            return _timestamp_text(child)
    return None


def _author(element: Tag | None) -> str:
    return _collapse(element.get_text(" ")) if element is not None else ""


class _Reader:
    def __init__(
        self,
        tz: ZoneInfo,
        now: datetime,
        visible: Mapping[int, bool] | None,
        signatures: Sequence[str] = (),
        requester: str | None = None,
    ) -> None:
        self.tz = tz
        self.now = now
        self.visible = visible  # id() of each marker tag -> rendered; None = presence counts
        # The desk-signature rule needs the requester, who never becomes desk through it.
        self.signatures = signature_phrases(signatures) if requester else ()
        self.requester = requester or ""
        self.entries: list[_Entry] = []
        self.shapes: list[str] = []
        self.stats = empty_marker_stats()
        self.desk_authors: list[str] = []

    def add(
        self,
        author: str,
        stamp: str | None,
        kind: EntryKind,
        parts: Sequence[Tag | NavigableString] = (),
    ) -> None:
        if not author:
            self.shapes.append("no author")
            return
        created = parse_tdx_datetime(stamp, self.tz, self.now) if stamp else None
        if created is None:
            self.shapes.append(f"undated: {date_shape(stamp) if stamp else 'no timestamp'}")
        kept = tuple(parts) if kind == "reply" else ()
        self.entries.append(_Entry(author, created, kind, kept))

    def _marked(self, container: Tag | None) -> bool:
        """Whether ``container`` holds a rendered private marker; counts the block's markers."""
        markers = container.select(PRIVATE_SELECTOR) if container is not None else []
        if not markers:
            return False
        self.stats["private_present"] += 1
        if self.visible is None:
            shown = True
        else:
            shown = any(self.visible.get(id(marker), False) for marker in markers)
        self.stats["private_visible"] += shown
        return shown

    def _note(self, author: str, parts: Sequence[Tag | NavigableString]) -> NoteKind | None:
        """A merge notice or reassignment: counted, and its author marked as desk."""
        note = _note_kind(parts)
        if note == "merge":
            self.stats["merge_notes"] += 1
        elif note == "reassignment":
            self.stats["reassignments"] += 1
        if note is not None and author:
            self.desk_authors.append(author)  # like a private note: only staff merge or reassign
        return note

    def _signed(self, author: str, kind: EntryKind, parts: Sequence[Tag | NavigableString]) -> None:
        """A visible reply whose own text holds a desk signature: its author is desk staff.

        Never the requester, even with a signature. Private notes are a strong signal already.
        """
        if kind != "reply" or not author or same_person(author, self.requester):
            return
        if not _has_signature(parts, self.signatures):
            return
        self.stats["signature_desk_replies"] += 1
        if not any(same_person(author, known) for known in self.desk_authors):
            self.desk_authors.append(author)

    def read(self, body: Tag) -> None:
        heading = _child_with_class(body, HEADING_CLASS)
        author = _author(heading.select_one(f".{AUTHOR_CLASS}") if heading else None)
        private = self._marked(heading)
        self.stats["entries"] += 1
        parts = _comment_parts(body, NOT_COMMENT_CLASSES)
        note = self._note(author, parts)
        if note is not None:
            # One entry, however long the carried-over thread inside it: the thread is text in
            # the comment, never .feed-reply blocks, so nothing is split off.
            self.add(author, _own_timestamp(body), "system")
            private = False  # replies under a TDX notice inherit nothing
        else:
            kind = _kind(private, parts)
            self.add(author, _own_timestamp(body), kind, parts)
            self._signed(author, kind, parts)
        for reply in _children(body):
            if not _has_class(reply, REPLY_CLASS):
                continue
            self.stats["entries"] += 1
            self.stats["nested"] += 1
            reply_author = _author(_child_with_class(reply, AUTHOR_CLASS))
            own = self._marked(reply)
            # A reply under a private note is private too only if the note's author wrote it:
            # anyone else replying there may have written to the requestor.
            inherited = not own and private and same_person(reply_author, author)
            self.stats["inherited_private"] += inherited
            reply_parts = _comment_parts(reply, NOT_REPLY_COMMENT_CLASSES)
            reply_kind = _kind(own or inherited, reply_parts)
            self.add(
                reply_author,
                _timestamp_text(_child_with_class(reply, REPLY_CONTEXT_CLASS)),
                reply_kind,
                reply_parts,
            )
            if note != "merge":  # nothing inside a merge notice counts
                self._signed(reply_author, reply_kind, reply_parts)


def _newest_first(entries: list[_Entry]) -> list[_Entry]:
    """Dated entries sorted newest first; undated ones keep their page position."""
    dated = iter(sorted((e for e in entries if e.created), key=lambda e: e.created, reverse=True))
    return [entry if entry.created is None else next(dated) for entry in entries]


def _latest_reply_text(ordered: Sequence[_Entry]) -> str | None:
    """The comment text of the first "reply" entry, the one the rules judge; nothing else."""
    newest = next((entry for entry in ordered if entry.kind == "reply"), None)
    return _comment_text(newest.parts) if newest is not None else None


def _document(tag: Tag) -> Tag:
    top = tag
    for parent in tag.parents:
        top = parent
    return top


def _visibility(root: Tag, flags: Sequence[bool] | None) -> tuple[dict[int, bool] | None, bool]:
    """Map each private marker of the root's document to its flag, in document order.

    Returns (mapping or None, fell_back). None means presence counts: no flags were given, or
    their number does not match the markers in the HTML.
    """
    if flags is None:
        return None, False
    markers = _document(root).select(PRIVATE_SELECTOR)
    if len(markers) != len(flags):
        return None, True
    return {id(marker): bool(flag) for marker, flag in zip(markers, flags, strict=True)}, False


def read_feed_frames(
    frame_htmls: Sequence[str],
    ticket_id: int,
    tz: ZoneInfo,
    now: datetime,
    *,
    private_marker_visible: Sequence[bool] | None = None,
    desk_signatures: Sequence[str] = (),
    requester: str | None = None,
) -> FeedDetails | None:
    """The feed from the first frame that holds one (a loaded one first); None if none does.

    ``private_marker_visible``: one flag per ``.feed-private`` element of the feed frame, in
    document order, saying whether the browser rendered it. Pass only the feed frame's HTML
    with it.

    ``desk_signatures``: a visible reply whose own text (quoted mail cut) holds
    one of these phrases, case-insensitively with whitespace collapsed, puts its author in
    ``desk_authors``. Never the ``requester``; without a requester the rule is off.
    """
    candidates: list[Tag] = []
    for html in frame_htmls:
        for root in _roots(_soup(html)):
            candidates.append(root)
    if not candidates:
        return None
    ready = [root for root in candidates if _state(root) != "loading"]
    root = (ready or candidates)[0]
    visible, fell_back = _visibility(root, private_marker_visible)
    reader = _Reader(tz, now, visible, desk_signatures, requester)
    reader.stats["visibility_fallback"] = int(fell_back)
    for _, body in _entries(root):
        reader.read(body)
    ordered = _newest_first(reader.entries)
    feed = TicketFeed(
        ticket_id=ticket_id,
        entries=tuple(FeedEntry(e.author, e.created, e.kind) for e in ordered),
        unreadable=len(reader.shapes),
        desk_authors=tuple(reader.desk_authors),
        latest_reply_text=_latest_reply_text(ordered),
    )
    return FeedDetails(
        feed=feed, unreadable_shapes=tuple(reader.shapes), marker_stats=dict(reader.stats)
    )


def parse_feed(
    frame_htmls: Sequence[str],
    ticket_id: int,
    tz: ZoneInfo,
    now: datetime,
    *,
    private_marker_visible: Sequence[bool] | None = None,
    desk_signatures: Sequence[str] = (),
    requester: str | None = None,
) -> TicketFeed | None:
    """The ticket's feed, newest entry first; None if no frame holds a feed."""
    details = read_feed_frames(
        frame_htmls,
        ticket_id,
        tz,
        now,
        private_marker_visible=private_marker_visible,
        desk_signatures=desk_signatures,
        requester=requester,
    )
    return details.feed if details is not None else None
