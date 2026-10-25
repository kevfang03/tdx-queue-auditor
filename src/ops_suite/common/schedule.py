"""Desk hours, when the background loops may run, and the shift-summary slots. Pure; no clock
of its own.

Times are compared in the desk's local time (``schedule.timezone``) through ``zoneinfo``, so a
window keeps its wall-clock hours across a DST change.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ops_suite.common.config import (
    WEEKDAYS,
    ScheduleSettings,
    TdxSummarySettings,
    parse_time,
    parse_window,
)

SEARCH_DAYS = 8


@dataclass(frozen=True, slots=True)
class DeskHours:
    tz: ZoneInfo
    windows: Mapping[int, tuple[time, time]]  # weekday() -> (start, end); missing = closed

    @classmethod
    def from_settings(cls, schedule: ScheduleSettings) -> "DeskHours":
        windows: dict[int, tuple[time, time]] = {}
        for day, text in schedule.desk_hours.items():
            window = parse_window(text)
            if window is not None:
                (start_h, start_m), (end_h, end_m) = window
                windows[WEEKDAYS.index(day)] = (time(start_h, start_m), time(end_h, end_m))
        return cls(ZoneInfo(schedule.timezone), windows)

    def _local(self, now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(self.tz)

    def is_open(self, now: datetime) -> bool:
        """Inside today's window: the start counts as open, the end as closed."""
        local = self._local(now)
        window = self.windows.get(local.weekday())
        return window is not None and window[0] <= local.time() < window[1]

    def next_open(self, now: datetime) -> datetime:
        """``now`` itself when open, else the next window start after it; aware, in ``tz``.

        ValueError if every day is closed.
        """
        local = self._local(now)
        if self.is_open(local):
            return local
        for offset in range(SEARCH_DAYS + 1):
            day = local.date() + timedelta(days=offset)
            window = self.windows.get(day.weekday())
            if window is None:
                continue
            start = datetime.combine(day, window[0], tzinfo=self.tz)
            # Compare in UTC: Python compares same-tzinfo datetimes as wall-clock times.
            if start.astimezone(UTC) > local.astimezone(UTC):
                return start
        raise ValueError("desk hours are closed on every day of the week")

    def _window_on(self, day: date) -> tuple[datetime, datetime] | None:
        """``day``'s window as aware datetimes in ``tz``, or None when the desk is closed."""
        window = self.windows.get(day.weekday())
        if window is None:
            return None
        return (
            datetime.combine(day, window[0], tzinfo=self.tz),
            datetime.combine(day, window[1], tzinfo=self.tz),
        )

    def window_at(self, now: datetime) -> tuple[datetime, datetime] | None:
        """The window containing ``now`` (the start inside, the end outside), or None."""
        local = self._local(now)
        window = self._window_on(local.date())
        moment = local.astimezone(UTC)
        if window is not None and window[0].astimezone(UTC) <= moment < window[1].astimezone(UTC):
            return window
        return None

    def last_closed(self, now: datetime) -> tuple[datetime, datetime] | None:
        """The most recent window that ended at or before ``now`` (searching back
        ``SEARCH_DAYS`` days), or None."""
        local = self._local(now)
        moment = local.astimezone(UTC)
        for offset in range(SEARCH_DAYS + 1):
            window = self._window_on(local.date() - timedelta(days=offset))
            if window is not None and window[1].astimezone(UTC) <= moment:
                return window
        return None

    def next_window(self, now: datetime) -> tuple[datetime, datetime]:
        """The window containing ``now``, else the next one to start after it.

        ValueError if every day is closed.
        """
        current = self.window_at(now)
        if current is not None:
            return current
        start = self.next_open(now)
        window = self._window_on(start.date())
        assert window is not None  # next_open only returns a window's start
        return window


@dataclass(frozen=True, slots=True)
class SummarySlots:
    """Shift-summary times (``tdx.summary.times``): wall-clock times per weekday in ``tz``."""

    tz: ZoneInfo
    times: Mapping[int, tuple[time, ...]]  # weekday() -> sorted times; missing = none

    @classmethod
    def from_settings(
        cls, summary: TdxSummarySettings, schedule: ScheduleSettings
    ) -> "SummarySlots":
        times: dict[int, tuple[time, ...]] = {}
        for day, texts in summary.times.items():
            parsed = sorted(time(*parse_time(text)) for text in texts)
            if parsed:
                times[WEEKDAYS.index(day)] = tuple(parsed)
        return cls(ZoneInfo(schedule.timezone), times)

    def _on(self, day: date) -> list[datetime]:
        return [
            datetime.combine(day, when, tzinfo=self.tz)
            for when in self.times.get(day.weekday(), ())
        ]

    def latest_at_or_before(self, now: datetime) -> datetime | None:
        """The most recent slot at or before ``now`` (searching back a week), or None."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        moment = now.astimezone(UTC)
        local = now.astimezone(self.tz)
        for offset in range(SEARCH_DAYS + 1):
            day = local.date() - timedelta(days=offset)
            past = [s for s in self._on(day) if s.astimezone(UTC) <= moment]
            if past:
                return past[-1]
        return None

    def next_after(self, now: datetime) -> datetime | None:
        """The first slot strictly after ``now``, or None when there are no slots at all."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        moment = now.astimezone(UTC)
        local = now.astimezone(self.tz)
        for offset in range(SEARCH_DAYS + 1):
            for slot in self._on(local.date() + timedelta(days=offset)):
                if slot.astimezone(UTC) > moment:
                    return slot
        return None
