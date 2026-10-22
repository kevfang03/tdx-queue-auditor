"""The event request form in a ticket's description: the few answers the outreach rules need.
Pure functions; no I/O.

Event requests arrive from one standard web form, titled "CCI Event Request Form" until the
desk renames the ticket. The college is now the SCIS (formerly CCI); until the form follows,
the defaults list both names ("SCIS Event Request Form", "SCIS Operational Assistance").

Its answers sit in the ticket's description: first a ``Recipient Data:`` block (submission
metadata: an IP address, a response id, a link to a private results report), then a
``Response Summary:`` block with the answers, which hold student names, emails and ID numbers.
The outreach rules that use them are in docs/tdx-rules.md.

``parse_event_form`` reads only "Response Summary:" blocks: each runs from that label to the
next "Recipient Data:" or "Response Summary:", so a Recipient Data block is never parsed. A
page can hold more than one copy of the form (a shortened preview and the full text), so
every block is read and the most complete one is used. Only the recurring answer, the date
and time answers, "I am looking:", the type of space and the services are read, into an
``EventForm`` of dates, flags and one count. Everything else is dropped with the text.

Personal data rule: the description is student data. It is never logged, kept, put in an
exception or in ``repr()``; ``EventForm`` holds no text at all, and parsing never raises.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from ops_suite.tdx.events import EventInfo, read_event_times

# The description's text container in the TDNext ticket frame (docs/tdx-page-structure.md,
# "Ticket description"); the first selector that matches wins.
DESCRIPTION_SELECTORS: tuple[str, ...] = ("#divDescription .ticket-desc", ".ticket-desc")

_RESPONSE_SUMMARY = re.compile(r"response\s+summary\s*:", re.IGNORECASE)
_RECIPIENT_DATA = re.compile(r"recipient\s+data\s*:", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_SPACES = re.compile(r"\s+")
_ITEM_SEPARATORS = re.compile(r"[\n;,]")
_BLOCK_TAGS = ["div", "p", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"]
# Never part of a description's rendered text: code, styles, buttons, and form controls,
# whose contents are source text (a text box's copy of the form is read on its own).
_NOT_TEXT_TAGS = ["script", "style", "button", "textarea"]
# A copy of the form outside the rendered description (a text box, an attribute, a script)
# is recognised by these words; only such copies are read. Such a copy is HTML source, so
# the space may be written as an entity (Response&nbsp;Summary).
_SUMMARY_WORDS = re.compile(r"response(?:\s|&nbsp;|&#0*160;|&#x0*a0;)+summary", re.IGNORECASE)
# An HTML tag of the kinds a description uses, left in text that was escaped HTML.
_MARKUP = re.compile(r"</?(?:div|p|br|b|strong|span|td|tr|li|ul|table)\b[^>]*>", re.IGNORECASE)
# A double-quoted string literal in a script, escapes included, as JSON reads it.
_JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')

# Every label of the Response Summary, in form order, as the visible prefix TDX shows (some
# labels are cut with "..."). A field's answer runs to the next label found. Labels missing
# from a form (fields vary with the "Are you:" answer) are skipped.
_LABELS: tuple[tuple[str, str], ...] = (
    ("are_you", "Are you:"),
    ("event_type", "Event Type:"),
    ("event_title", "Event Title:"),
    ("event_description", "Event Description:"),
    ("on_site_contact", "On-Site Contact:"),
    ("recurring", "Is this a recurring event:"),
    ("dates", "What is your proposed event date(s):"),
    ("times", "What is your event proposed start/end times:"),
    ("reviewed", "Please confirm that you have reviewed the event date(s)"),
    ("looking", "I am looking:"),
    ("space", "What type of space are you looking for:"),
    ("audience", "Who is your anticipated audience"),
    ("headcount", "What is your estimated event headcount?"),
    ("minors", "Will this event include minors?"),
    ("services", "Please select all that apply:"),
    ("av_needs", "Please describe your A/V needs in detail"),
    ("consent", "By submitting this request"),
)
# The type of space, from the first word of the single-choice answer (the form's options are
# "Lobby Area ...", "Classroom Space ..." and "Other ..."); a category, never the answer text.
SpaceType = Literal["lobby", "classroom", "other"]
_SPACE_WORDS: dict[str, SpaceType] = {"lobby": "lobby", "classroom": "classroom", "other": "other"}
# The only answers that are read; the rest only mark where these end.
READ_FIELDS = frozenset({"recurring", "dates", "times", "looking", "space", "services"})


def _label_pattern(label: str, anchored: bool) -> re.Pattern[str]:
    words = r"\s+".join(re.escape(word) for word in label.split())
    return re.compile((r"(?:^|(?<=\n))[ \t]*" if anchored else "") + words, re.IGNORECASE)


# (field, label at a line start, label anywhere): a label is looked for at a line start
# first, so the same words inside an earlier answer are not taken for it.
_LABEL_PATTERNS = tuple(
    (name, _label_pattern(label, True), _label_pattern(label, False)) for name, label in _LABELS
)


@dataclass(frozen=True, slots=True)
class EventFormOptions:
    """Option prefixes, matched case-insensitively at the start of an answer after collapsing
    whitespace, so small wording changes on the form still match."""

    marketing_only_prefixes: tuple[str, ...] = ("Marketing/advertising",)
    av_service_prefixes: tuple[str, ...] = ("A/V & Technical Support",)
    room_space_prefixes: tuple[str, ...] = ("Classroom Space",)
    # The form's other options, listed only so they are not counted as unknown.
    known_option_prefixes: tuple[str, ...] = (
        "To host an event",
        "Lobby Area",
        "Other",
        "CCI Operational Assistance",
        "SCIS Operational Assistance",
        "Event Catering",
        "Marketing Support",
    )

    def all_prefixes(self) -> tuple[str, ...]:
        return (
            *self.marketing_only_prefixes,
            *self.av_service_prefixes,
            *self.room_space_prefixes,
            *self.known_option_prefixes,
        )


@dataclass(frozen=True, slots=True)
class EventForm:
    """What the outreach rules need from one form; no text."""

    dates: tuple[datetime, ...]  # aware, from the date and time answers; empty if unreadable
    recurring: bool | None
    wants_av: bool
    wants_room: bool
    marketing_only: bool
    unknown_options: int  # selected options that matched no known prefix (count only)
    # For the summary's "Events this week": whether a start time was given, the
    # end of the time range when one was, and the type of space as a category.
    has_time: bool = False
    end_time: time | None = None
    space: SpaceType | None = None

    def __repr__(self) -> str:
        return (
            f"EventForm(dates={len(self.dates)}, recurring={self.recurring}, "
            f"wants_av={self.wants_av}, wants_room={self.wants_room}, "
            f"marketing_only={self.marketing_only}, unknown_options={self.unknown_options}, "
            f"has_time={self.has_time}, end_time={self.end_time}, space={self.space})"
        )


# --- the description's text --------------------------------------------------------------
#
# Both readers turn a description into text with ``description_to_text``, so they cannot
# drift apart: the browser hands over the description's HTML from the page's DOM, the Web API
# its stored description. The browser's rendered text (innerText) is not used, because it
# leaves out whatever the page hides. A long description shown as a shortened preview, with
# the full text hidden behind a "show more" toggle, would lose its later answers, the
# services and the space among them.


def description_to_text(description: str, *, rich_html: bool = True) -> str:
    """A description as text, one line per block or line break, hidden parts included.

    ``description``: the description's HTML (part of the page's DOM, or the Web API's
    rich-text description); with ``rich_html=False``, plain text, kept as it is.
    Non-breaking spaces become spaces and line ends become ``\\n``, so the option prefixes
    match the same way whichever reader gave the text. Scripts, styles, buttons and text
    boxes are never part of the rendered description and are dropped. Student data: hand
    the result straight to ``parse_event_form``."""
    if rich_html:
        soup = BeautifulSoup(description, "html.parser")
        # A template's contents are left out of get_text(); parsed again, they count.
        while (template := soup.find("template")) is not None:
            template.replace_with(BeautifulSoup(template.decode_contents(), "html.parser"))
        for element in soup.find_all(_NOT_TEXT_TAGS):
            element.decompose()
        for line_break in soup.find_all("br"):
            line_break.replace_with("\n")
        for block in soup.find_all(_BLOCK_TAGS):
            block.insert_before("\n")
            block.insert_after("\n")
        text = soup.get_text()
    else:
        text = description
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")


# Where on the ticket page a copy of the description was found.
PartSource = Literal["container", "outside", "text box", "template", "attribute", "script"]
PART_SOURCES: tuple[PartSource, ...] = (
    "container",
    "outside",
    "text box",
    "template",
    "attribute",
    "script",
)


@dataclass(frozen=True, slots=True)
class DescriptionPart:
    """One place on the page that holds description text. Student data: ``text`` is never
    shown in ``repr()``, logged or kept."""

    source: PartSource
    text: str = field(repr=False)


def _container(soup: BeautifulSoup) -> Tag | None:
    for selector in DESCRIPTION_SELECTORS:
        container = soup.select_one(selector)
        if container is not None:
            return container
    return None


def _section(container: Tag) -> Tag:
    """The description's section: the ``#divDescription`` around the container, or the
    element around that whose id ends in "Description" (with the heading and any toggle)."""
    section = container.find_parent(id="divDescription") or container.parent or container
    outer = section.parent
    if isinstance(outer, Tag) and str(outer.get("id", "")).lower().endswith("description"):
        return outer
    return section


def description_parts(html: str) -> list[DescriptionPart] | None:
    """Every place in a ticket frame's HTML that holds the description; None if the frame
    has no description container. In order:

    - ``container``: the container's DOM, hidden parts included;
    - ``outside``: the rest of its section, when it holds "Response Summary" (a full copy
      may sit next to the container);
    - ``text box``, ``template``, ``attribute``, ``script``: anywhere in the frame, a copy
      kept in a text box (as source text), a template, an attribute or a script's string
      literal, read only when it holds "Response Summary".

    Student data: hand the parts straight to ``read_event_form``."""
    soup = BeautifulSoup(html, "html.parser")
    container = _container(soup)
    if container is None:
        return None
    parts = [DescriptionPart("container", description_to_text(container.decode_contents()))]
    outside = _section(container).decode_contents().replace(container.decode(), "", 1)
    if _SUMMARY_WORDS.search(outside):
        parts.append(DescriptionPart("outside", description_to_text(outside)))
    for box in soup.find_all("textarea"):
        source = box.get_text()
        if _SUMMARY_WORDS.search(source):
            parts.append(DescriptionPart("text box", description_to_text(source)))
    for template in soup.find_all("template"):
        if any(parent is container for parent in template.parents):
            continue  # inside the container: already part of its text
        contents = template.decode_contents()
        if _SUMMARY_WORDS.search(contents):
            parts.append(DescriptionPart("template", description_to_text(contents)))
    for element in soup.find_all(True):
        for value in element.attrs.values():
            if isinstance(value, str) and _SUMMARY_WORDS.search(value):
                parts.append(DescriptionPart("attribute", description_to_text(value)))
    for script in soup.find_all("script"):
        code = script.get_text()
        if not _SUMMARY_WORDS.search(code):
            continue
        for literal in _JSON_STRING.findall(code):
            if not _SUMMARY_WORDS.search(literal):
                continue
            try:
                decoded = json.loads(literal)
            except ValueError:
                continue
            if isinstance(decoded, str):
                parts.append(DescriptionPart("script", description_to_text(decoded)))
    # A copy may be kept as escaped HTML in the page's text, so its text still shows tags
    # around the labels: such a part is read once more as HTML, from the same place.
    for part in list(parts):
        if _MARKUP.search(part.text) and _SUMMARY_WORDS.search(part.text):
            parts.append(DescriptionPart(part.source, description_to_text(part.text)))
    return parts


def description_text(html: str) -> str | None:
    """Every copy of the description in a ticket frame's HTML as one text, through
    ``description_to_text``; None if the frame has no description. Student data: hand it
    straight to ``parse_event_form``."""
    parts = description_parts(html)
    return "\n\n".join(part.text for part in parts) if parts is not None else None


# --- parsing -----------------------------------------------------------------------------


def _norm(text: str) -> str:
    return _SPACES.sub(" ", text).strip().casefold()


def _starts_with_any(answer: str, prefixes: Sequence[str]) -> bool:
    folded = _norm(answer)
    return any(prefix.strip() and folded.startswith(_norm(prefix)) for prefix in prefixes)


def _answers(summary: str) -> dict[str, str]:
    """The answers of READ_FIELDS: each runs from its label to the next label found."""
    found: list[tuple[str, int, int]] = []  # (field, label start, label end)
    position = 0
    for name, anchored, anywhere in _LABEL_PATTERNS:
        match = anchored.search(summary, position) or anywhere.search(summary, position)
        if match is None:
            continue
        found.append((name, match.start(), match.end()))
        position = match.end()
    answers: dict[str, str] = {}
    for index, (name, _, end) in enumerate(found):
        if name not in READ_FIELDS:
            continue
        stop = found[index + 1][1] if index + 1 < len(found) else len(summary)
        answers[name] = summary[end:stop].strip()
    return answers


def _first_line(answer: str) -> str:
    return next((line.strip() for line in answer.splitlines() if line.strip()), "")


def _recurring(answer: str | None) -> bool | None:
    if answer is None:
        return None
    word = _norm(_first_line(answer)).split(" ", 1)[0] if answer.strip() else ""
    if word.startswith("yes"):
        return True
    if word.startswith("no"):
        return False
    return None


def _dates(answers: dict[str, str], tz: ZoneInfo, now: datetime) -> EventInfo:
    text = answers.get("dates", "")
    if not text.strip():
        return EventInfo(dates=(), has_time=False, location_tbd=False)
    # A date picker may answer 2026-10-28; the lenient reader expects month/day.
    text = _ISO_DATE.sub(lambda m: f"{int(m[2])}/{int(m[3])}/{m[1]}", text)
    times = answers.get("times", "")
    return read_event_times(f"{text}\n{times}", tz, now)


def _space(answer: str) -> SpaceType | None:
    first = _norm(answer).split(" ", 1)[0]
    return _SPACE_WORDS.get(first) if first else None


def _summary_blocks(text: str) -> list[str]:
    """Each "Response Summary:" block of ``text``: from the label to the next "Recipient
    Data:" or "Response Summary:", so no metadata block is ever part of one."""
    blocks: list[str] = []
    for start in _RESPONSE_SUMMARY.finditer(text):
        end = len(text)
        for boundary in (_RECIPIENT_DATA, _RESPONSE_SUMMARY):
            following = boundary.search(text, start.end())
            if following is not None:
                end = min(end, following.start())
        blocks.append(text[start.end() : end])
    return blocks


def _form(
    answers: dict[str, str], tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> EventForm:
    known = options.all_prefixes()
    unknown = 0
    looking = _first_line(answers.get("looking", ""))
    space = _first_line(answers.get("space", ""))
    for single in (looking, space):
        if single and not _starts_with_any(single, known):
            unknown += 1
    services = answers.get("services", "")
    items = [item for item in _ITEM_SEPARATORS.split(services) if item.strip()]
    unknown += sum(not _starts_with_any(item, known) for item in items)
    # Searched anywhere in the answer, so options run together on one line still count.
    folded_services = _norm(services)
    wants_av = any(
        prefix.strip() and _norm(prefix) in folded_services
        for prefix in options.av_service_prefixes
    )
    when = _dates(answers, tz, now)
    return EventForm(
        dates=when.dates,
        recurring=_recurring(answers.get("recurring")),
        wants_av=wants_av,
        wants_room=bool(space) and _starts_with_any(space, options.room_space_prefixes),
        marketing_only=bool(looking) and _starts_with_any(looking, options.marketing_only_prefixes),
        unknown_options=unknown,
        has_time=when.has_time,
        end_time=when.end_time,
        space=_space(space),
    )


@dataclass(frozen=True, slots=True)
class FormRead:
    """A form read from one or more copies of a description: the form, and how it was found
    as counts and a place name. No text."""

    form: EventForm | None
    blocks: int = 0  # "Response Summary:" blocks found, over every copy
    answers: int = 0  # of READ_FIELDS, the labels found in the block used
    source: PartSource | None = None  # the copy whose block was used


def _read(
    parts: Sequence[DescriptionPart], tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> FormRead:
    best: tuple[int, bool, int] | None = None
    chosen: tuple[dict[str, str], PartSource] | None = None
    blocks = 0
    for part in parts:
        for block in _summary_blocks(part.text):
            blocks += 1
            answers = _answers(block)
            # The most labels found wins; then a block without leftover tags (escaped HTML
            # read as text has them in its answers); then the longer block, so a full copy
            # beats a preview cut short inside its last answer. On a tie, the earlier copy.
            score = (len(answers), _MARKUP.search(block) is None, len(block.strip()))
            if best is None or score > best:
                best, chosen = score, (answers, part.source)
    if chosen is None:
        return FormRead(None, blocks)
    answers, source = chosen
    return FormRead(_form(answers, tz, now, options), blocks, len(answers), source)


def read_event_form(
    parts: Sequence[DescriptionPart], tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> FormRead:
    """The form from every "Response Summary:" block of every copy in ``parts``: the block
    with the most of the read labels, then the longest. ``FormRead.form`` is None when no
    block is found. Each block stops at the next "Recipient Data:" or "Response Summary:",
    so no metadata is parsed. Never raises with text in the message: any failure gives an
    unreadable form."""
    try:
        return _read(parts, tz, now, options)
    except Exception:  # an exception could carry form text: unreadable instead
        return FormRead(None)


def parse_event_form(
    description: str, tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> EventForm | None:
    """The form in one description's text (``read_event_form`` on it alone); None when no
    "Response Summary:" block is found. Only the recurring, date, time, "I am looking:",
    space and services answers are read. Never raises: any failure gives None."""
    return read_event_form([DescriptionPart("container", description)], tz, now, options).form
