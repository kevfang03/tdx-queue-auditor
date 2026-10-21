"""The event request form in a ticket's description: the few answers the outreach rules need.
Pure functions; no I/O.

Event requests arrive from one standard web form, titled "CCI Event Request Form" until the
desk renames the ticket. The college is now the SCIS (formerly CCI); until the form follows,
the defaults list both names ("SCIS Event Request Form", "SCIS Operational Assistance").

Its answers sit in the ticket's description: first a ``Recipient Data:`` block (submission
metadata: an IP address, a response id, a link to a private results report), then a
``Response Summary:`` block with the answers, which hold student names, emails and ID numbers.
See docs/ARCHITECTURE.md, Module 3, "Event outreach".

``parse_event_form`` first cuts the text at "Response Summary:", so nothing before it, the
Recipient Data block included, is ever parsed. It then reads only the recurring answer, the
date and time answers, "I am looking:", the type of space and the services, and returns an
``EventForm`` of dates, flags and one count. Everything else is dropped with the text.

Personal data rule: the description is student data. It is never logged, kept, put in an
exception or in ``repr()``; ``EventForm`` holds no text at all, and parsing never raises.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
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
_BLOCK_TAGS = ["div", "p", "li", "tr", "h1", "h2", "h3", "h4"]

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
    whitespace (docs/payloads/10-event-outreach.md, "Mapping")."""

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
    # For the summary's "Events this week" (payload 16): whether a start time was given, the
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


def description_text(html: str) -> str | None:
    """The description container's text from a frame's HTML, one line per rendered block or
    line break; None if the frame has no description. The fallback for when the browser's
    ``innerText`` cannot be read. Student data: hand it straight to ``parse_event_form``."""
    soup = BeautifulSoup(html, "html.parser")
    container: Tag | None = None
    for selector in DESCRIPTION_SELECTORS:
        container = soup.select_one(selector)
        if container is not None:
            break
    if container is None:
        return None
    for line_break in container.find_all("br"):
        line_break.replace_with("\n")
    for block in container.find_all(_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")
    return container.get_text()


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


def _parse(
    description: str, tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> EventForm | None:
    start = _RESPONSE_SUMMARY.search(description)
    if start is None:
        return None
    summary = description[start.end() :]  # the Recipient Data block is before this: dropped
    again = _RECIPIENT_DATA.search(summary)
    if again is not None:
        summary = summary[: again.start()]  # never read past a second metadata block
    answers = _answers(summary)

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


def parse_event_form(
    description: str, tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> EventForm | None:
    """None when no "Response Summary:" block is found. First cuts the text at
    "Response Summary:" (everything before it, including "Recipient Data:", is dropped),
    then reads only the recurring, date, time, "I am looking:", space and services answers.
    Never raises with text in the message: any failure gives None, as an unreadable form."""
    try:
        return _parse(description, tz, now, options)
    except Exception:  # an exception could carry form text: unreadable instead
        return None
