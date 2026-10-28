"""Times as the Teams cards show them: 12-hour, like the ticket titles (``7:30 AM``,
``1:30–2:30 PM``, ``Tue 1:37 PM``, ``Tue 10/20 1:37 PM`` a week or more away). Display only:
settings, state files, logs and the terminal keep 24-hour times.

Built from the hour and minute rather than strftime, because the flags that drop the
leading zero (``%-I``, ``%#I``) differ between platforms.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo


def _clock(value: datetime | time) -> tuple[str, str]:
    """``("1:37", "PM")``: the clock reading of ``value`` as it is, and its half of the day."""
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d}", "AM" if value.hour < 12 else "PM"


def clock_text(value: datetime | time) -> str:
    """``7:30 AM``, ``12:00 PM``, ``12:05 AM`` (five past midnight): the clock reading of
    ``value`` as it is, with no time zone conversion."""
    text, half = _clock(value)
    return f"{text} {half}"


def clock_range(start: datetime | time, end: datetime | time) -> str:
    """``1:30–2:30 PM`` when both ends are in the same half of the day, else ``11:30
    AM–12:30 PM``. Clock readings as they are, like ``clock_text``."""
    start_text, start_half = _clock(start)
    end_text, end_half = _clock(end)
    if start_half == end_half:
        return f"{start_text}–{end_text} {end_half}"
    return f"{start_text} {start_half}–{end_text} {end_half}"


# From this many days apart, a weekday alone could be read as the wrong week: on a
# Wednesday card, "since Tue" eight days back reads as yesterday.
DATED_AFTER_DAYS = 6


def card_time(
    when: datetime, tz: ZoneInfo, *, weekday: bool = False, now: datetime | None = None
) -> str:
    """``when`` in the display time zone ``tz``: ``7:30 AM``, or ``Tue 1:37 PM`` with
    ``weekday``. With ``weekday`` and the card's own time ``now``, a ``when`` whose local
    date is ``DATED_AFTER_DAYS`` or more days from ``now``'s, either way, also shows the date
    (``Tue 10/20 1:37 PM``), because the weekday alone would read as the nearest one."""
    local = when.astimezone(tz)
    text = clock_text(local)
    if not weekday:
        return text
    # Calendar dates, not elapsed time, so a DST change in between does not shift the count.
    days_apart = abs((local.date() - now.astimezone(tz).date()).days) if now is not None else 0
    if days_apart >= DATED_AFTER_DAYS:
        return f"{local:%a} {local.month}/{local.day} {text}"
    return f"{local:%a} {text}"
