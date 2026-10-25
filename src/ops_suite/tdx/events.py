"""Event-ticket titles: spotting them and reading their dates. Pure functions.

The desk renames event request forms to something like "EVENT [10/15 2-4pm Room TBD] Name",
but each student assistant writes the date a little differently, so reading is lenient: the
dates after the "event" prefix, in any of the numeric or month-name styles, each with the
first time after it. Some titles list several dates ("EVENT [10/15, 10/22 2pm Room] Name");
every date in that list is read. The rules that use them are in docs/tdx-rules.md.

Personal data rule: titles are student data. Nothing here logs, prints or raises with a title;
any input that cannot be read gives ``dates=()``.
"""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_EVENT_PREFIX = re.compile(r"\s*event(?=[\s\[(:\-])", re.IGNORECASE)
_TBD = re.compile(r"\btbd\b", re.IGNORECASE)
# Words that mark a recurring event; such tickets have no single date to check.
_RECURRING = re.compile(
    r"\b(?:weekly|bi-weekly|biweekly|bi\s+weekly|daily|monthly|recurring|every)\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
# "am"/"pm" in any spelling: "pm", "PM", "p.m.", "p m" is not accepted.
_AMPM = r"(?:\s*(?P<{name}>[ap])\.?m\b\.?)"
_NOT_A_TIME = r"(?!\s*[ap]\.?m\b|:\d)"
_DAY_SUFFIX = r"(?:st|nd|rd|th)?"

# 10/15, 10/15/26, 10/15/2026, 10-15, 10.15 (US month/day). Not "2-4pm" (a time range) and
# not part of a time such as "14:00-15:00".
_NUMERIC_DATE = re.compile(
    r"(?<![\d:/.\-])(?P<m>\d{1,2})(?P<sep>[/.\-])(?P<d>\d{1,2})"
    r"(?:(?P=sep)(?P<y>\d{4}|\d{2}))?(?!\d)(?!(?P=sep)\d)" + _NOT_A_TIME,
)
# Oct 15, October 15th, Oct. 15, 2026
_MONTH_DAY = re.compile(
    rf"\b(?P<mon>{_MONTH})\.?,?[\s\-]*(?P<d>\d{{1,2}}){_DAY_SUFFIX}(?!\d){_NOT_A_TIME}"
    r"(?:\s*,?\s*(?P<y>20\d{2})(?!\d))?",
    re.IGNORECASE,
)
# 15 Oct, 15th of October 2026
_DAY_MONTH = re.compile(
    rf"(?<![\d:/.\-])(?P<d>\d{{1,2}}){_DAY_SUFFIX}[\s\-]*(?:of\s+)?(?P<mon>{_MONTH})\.?"
    r"(?:\s*,?\s*(?P<y>20\d{2})(?!\d))?",
    re.IGNORECASE,
)
# Oct 20-22, October 20 – 22: two dates in the same month. Numeric "10/20-22" is not read as
# a range (it is ambiguous); it stays the single date 10/20.
_MONTH_RANGE = re.compile(
    rf"\b(?P<mon>{_MONTH})\.?,?[\s\-]*(?P<d>\d{{1,2}}){_DAY_SUFFIX}(?!\d)"
    rf"\s*[-–—]\s*(?P<d2>\d{{1,2}}){_DAY_SUFFIX}(?!\d){_NOT_A_TIME}"
    r"(?:\s*,?\s*(?P<y>20\d{2})(?!\d))?",
    re.IGNORECASE,
)
_DATE_PATTERNS = (_MONTH_RANGE, _NUMERIC_DATE, _MONTH_DAY, _DAY_MONTH)
# What may sit between two dates of one list when the title has no leading bracket.
_LIST_SEPARATOR = re.compile(r"(?:[\s,&;/\-–]|\band\b|\bto\b)+", re.IGNORECASE)
# A leading [...] or (...) group: when it holds a date, its dates are the date list.
_LEADING_GROUP = re.compile(r"\s*(?:\[(?P<square>[^\]]*)\]|\((?P<round>[^)]*)\))")
# 2pm, 2:30 PM, 14:00, and ranges 2-4pm, 2:00-3:30 PM, 2pm - 4pm (the start is used).
_TIME = re.compile(
    r"(?<![\d:/.\-])(?P<h>\d{1,2})(?!\d)(?::(?P<min>\d{2})(?!\d))?"
    + _AMPM.format(name="ap")
    + r"?(?:\s*(?:-|–|—|to)\s*(?P<h2>\d{1,2})(?!\d)(?::(?P<min2>\d{2})(?!\d))?"
    + _AMPM.format(name="ap2")
    + r"?)?",
    re.IGNORECASE,
)
_NOON = re.compile(r"\bnoon\b", re.IGNORECASE)
# What else a leading [...] group may hold besides dates, times and a location: weekdays and
# joining words. Whatever is left after these is taken as the location.
_WEEKDAY = re.compile(
    r"\b(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\b\.?", re.IGNORECASE
)
_FILLER = re.compile(r"\b(?:at|from|to|and|on|until|est|edt|et)\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]")

_END_OF_DAY = time(23, 59)


@dataclass(frozen=True, slots=True)
class EventInfo:
    dates: tuple[datetime, ...]  # sorted, aware, in tz; () if no date could be read
    has_time: bool  # any date had a time
    location_tbd: bool
    # The end of the time range after the last date ("2-4pm" gives 16:00), when it has one.
    end_time: time | None = None
    # A leading [...] or (...) group with a date and a time but nothing else:
    # "EVENT [10/21/26 6PM-7PM] Resume Review" names no room. Never True with location_tbd.
    location_missing: bool = False
    # A leading [...] or (...) group with a date and some other text left after the dates,
    # times, weekdays and joining words: "EVENT [10/28 10:30 AM Room 912] Talk" names a
    # location. False without such a group, for a date-only group and with TBD, so a
    # location is only taken as named where the title clearly names one. A yes/no only.
    location_named: bool = False

    @property
    def when(self) -> datetime | None:
        """The first date, or None (kept for callers that read one date)."""
        return self.dates[0] if self.dates else None


@dataclass(frozen=True, slots=True)
class _Found:
    """A date, or a month-name day range, found in the title text."""

    start: int
    end: int
    days: tuple[tuple[int, int, int | None], ...]  # (month, day, year or None): 1, or 2


def is_event_title(title: str) -> bool:
    """True if ``title`` starts with the word "event" followed by a space, bracket, colon or
    dash. "CCI Event Request Form" and "Eventbrite ..." are not event titles."""
    return _EVENT_PREFIX.match(title) is not None


def is_recurring_event(title: str) -> bool:
    """True for an event title that marks a recurring event after the "event" prefix: the
    whole word weekly, bi-weekly, biweekly, bi weekly, daily, monthly, recurring or every, in
    any capitalisation. ``EVENT [Bi-Weekly, Mon/Wed 2-3pm] Club`` is recurring."""
    prefix = _EVENT_PREFIX.match(title)
    return prefix is not None and _RECURRING.search(title, prefix.end()) is not None


def parse_event_title(title: str, tz: ZoneInfo, now: datetime) -> EventInfo:
    """Read every date in the title's date list, each with its time. Never raises.

    The date list is the dates inside a leading [...] or (...) group, or, when there is no
    such group or it holds no date, the run of dates starting at the first date. A date
    takes the first time after it and before the next date; a date without one takes the
    time after the last date; with no time at all, the end of the day.
    """
    location_tbd = _TBD.search(title) is not None
    prefix = _EVENT_PREFIX.match(title)
    text = title[prefix.end() :] if prefix else title
    info = read_event_times(text, tz, now)
    leftover = _group_leftover(text)
    missing = not location_tbd and leftover is not None and leftover == (True, False)
    named = not location_tbd and leftover is not None and leftover[1]
    return replace(info, location_tbd=location_tbd, location_missing=missing, location_named=named)


def read_dates(text: str, tz: ZoneInfo, now: datetime) -> tuple[datetime, ...]:
    """Every date of ``text``'s date list, each with its time, as ``parse_event_title`` reads
    a title's; for the event request form's date and time answers. Sorted; never raises."""
    return read_event_times(text, tz, now).dates


def read_event_times(text: str, tz: ZoneInfo, now: datetime) -> EventInfo:
    """``read_dates`` with whether any date had a time and the end time, when one is given.
    ``location_tbd`` is always False here. Never raises."""
    found = _date_list(text)
    if not found:
        return EventInfo(dates=(), has_time=False, location_tbd=False)
    dates, has_time = _read(text, found, tz, now)
    end = _first_end_time(text[found[-1].end :]) if has_time else None
    return EventInfo(dates=dates, has_time=has_time, location_tbd=False, end_time=end)


def _read(
    text: str, found: list["_Found"], tz: ZoneInfo, now: datetime
) -> tuple[tuple[datetime, ...], bool]:
    last_time = _first_time(text[found[-1].end :])
    dates: set[datetime] = set()
    has_time = False
    for index, item in enumerate(found):
        if index + 1 < len(found):
            at = _first_time(text[item.end : found[index + 1].start]) or last_time
        else:
            at = last_time
        for month, day, year in item.days:
            when = _resolve(month, day, year, at or _END_OF_DAY, tz, now)
            if when is not None:
                dates.add(when)
                has_time = has_time or at is not None
    return tuple(sorted(dates)), has_time


def _group_leftover(text: str) -> tuple[bool, bool] | None:
    """For a leading [...] or (...) group holding a date: (it has a time, text is left after
    the dates, times, weekdays and joining words). None without such a group: where its
    location would be cannot be told. Only flags come out, never the text.

    ``(True, False)``, a date and a time and nothing else, is a missing location; any text
    left over names one."""
    group = _LEADING_GROUP.match(text)
    if group is None:
        return None
    name = "square" if group["square"] is not None else "round"
    inside = text[group.start(name) : group.end(name)]
    found = _dates_in(inside)
    if not found:
        return None
    for item in reversed(found):
        inside = inside[: item.start] + " " + inside[item.end :]
    times = [match for match in _TIME.finditer(inside) if _time_from(match) is not None]
    times += list(_NOON.finditer(inside))
    for match in sorted(times, key=lambda m: m.start(), reverse=True):
        inside = inside[: match.start()] + " " + inside[match.end() :]
    rest = _FILLER.sub(" ", _WEEKDAY.sub(" ", inside))
    return bool(times), _WORD.search(rest) is not None


# --- dates -------------------------------------------------------------------------------


def _date_list(text: str) -> list[_Found]:
    """The title's date list, in title order. See ``parse_event_title``."""
    group = _LEADING_GROUP.match(text)
    if group is not None:
        name = "square" if group["square"] is not None else "round"
        inside = _dates_in(text[group.start(name) : group.end(name)], offset=group.start(name))
        if inside:
            return inside

    found = _dates_in(text)
    if not found:
        return []
    run = [found[0]]
    while True:
        separator = _LIST_SEPARATOR.match(text, run[-1].end)
        following = _date_at(text, separator.end()) if separator is not None else None
        if following is None:
            return run
        run.append(following)


def _dates_in(text: str, offset: int = 0) -> list[_Found]:
    """Every date in ``text``, in order and without overlaps (a range wins over its first
    day). Offsets are shifted by ``offset``."""
    candidates = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            days = _date_parts(match)
            if days is not None:
                candidates.append(_Found(match.start() + offset, match.end() + offset, days))
    candidates.sort(key=lambda found: (found.start, -found.end))
    dates: list[_Found] = []
    for found in candidates:
        if not dates or found.start >= dates[-1].end:
            dates.append(found)
    return dates


def _date_at(text: str, start: int) -> _Found | None:
    """The longest date that begins exactly at ``start``, or None. It is matched on the rest
    of the text alone, so the second date of "10/20-10/22" is read after the dash."""
    rest = text[start:]
    best: _Found | None = None
    for pattern in _DATE_PATTERNS:
        match = pattern.match(rest)
        days = _date_parts(match) if match is not None else None
        if match is None or days is None:
            continue
        if best is None or start + match.end() > best.end:
            best = _Found(start, start + match.end(), days)
    return best


def _date_parts(match: re.Match[str]) -> tuple[tuple[int, int, int | None], ...] | None:
    """The (month, day, year or None) of a date match: one, or two for a day range."""
    groups = match.groupdict()
    if groups.get("mon"):
        month = _MONTHS[groups["mon"][:3].lower()]
    else:
        month = int(groups["m"])
    year = _year(groups["y"]) if groups.get("y") else None
    days = [int(groups["d"])]
    if groups.get("d2"):
        days.append(int(groups["d2"]))
        if days[1] <= days[0]:
            return None  # "Oct 30-2" is not read as a range
    # A date that exists in no year (13/40, 2/30) is not a date. Feb 29 is checked later,
    # once the year is known.
    try:
        for day in days:
            date(2024, month, day)
    except ValueError:
        return None
    return tuple((month, day, year) for day in days)


def _year(text: str) -> int:
    return 2000 + int(text) if len(text) == 2 else int(text)


def _resolve(
    month: int, day: int, year: int | None, at: time, tz: ZoneInfo, now: datetime
) -> datetime | None:
    """The aware datetime; a missing year is the one that puts the date closest to ``now``."""
    years = [year] if year is not None else [now.year - 1, now.year, now.year + 1]
    best: datetime | None = None
    for candidate in years:
        try:
            when = datetime.combine(date(candidate, month, day), at, tzinfo=tz)
        except (ValueError, OverflowError):
            continue
        if best is None or abs(when - now) < abs(best - now):
            best = when
    return best


# --- times -------------------------------------------------------------------------------


def _first_time(text: str) -> time | None:
    """The first time in ``text`` (a range gives its start), or None."""
    candidates: list[tuple[int, time]] = []
    for match in _TIME.finditer(text):
        at = _time_from(match)
        if at is not None:
            candidates.append((match.start(), at))
            break
    noon = _NOON.search(text)
    if noon is not None:
        candidates.append((noon.start(), time(12, 0)))
    return min(candidates, key=lambda c: c[0])[1] if candidates else None


def _first_end_time(text: str) -> time | None:
    """The end of the first time in ``text`` when it is a range ("2:00 PM - 4:00 PM" gives
    16:00), else None. The end needs its own am/pm, or a 24-hour "16:00"."""
    for match in _TIME.finditer(text):
        if _time_from(match) is None:
            continue
        if match["h2"] is None:
            return None
        hour, minute = int(match["h2"]), int(match["min2"] or 0)
        if match["ap2"] is not None:
            if not 1 <= hour <= 12 or minute > 59:
                return None
            return time(hour % 12 + (12 if match["ap2"].lower() == "p" else 0), minute)
        if match["ap"] is not None or match["min2"] is None or hour > 23 or minute > 59:
            return None
        return time(hour, minute)
    return None


def _time_from(match: re.Match[str]) -> time | None:
    hour, minute = int(match["h"]), int(match["min"] or 0)
    ampm = match["ap"]
    if ampm is None and match["h2"] is not None and match["ap2"] is not None:
        # A range's am/pm applies to a start without one, unless that would put the start
        # after the end ("11-1pm" starts at 11am).
        ampm = match["ap2"]
        if hour % 12 > int(match["h2"]) % 12:
            ampm = "a" if ampm.lower() == "p" else "p"
    if ampm is not None:
        if not 1 <= hour <= 12 or minute > 59:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
        return time(hour, minute)
    # No am/pm anywhere: only a 24-hour "14:00" counts; a bare number is not a time.
    if match["min"] is None or hour > 23 or minute > 59:
        return None
    return time(hour, minute)
