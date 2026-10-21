"""How long a TDX sign-in lasts: ``session-log.json`` in the TDX data folder (payload 15).

Each finished ``tdx login`` starts a session (``signed_in_at``); the first "not signed in"
that ``tdx audit``, ``tdx watch`` or ``tdx rows`` meets after it closes the session
(``expired_seen_at``). The file holds those two timestamps per session and nothing else, for
the last 30 sessions. An expiry is only seen when something runs, and ``tdx watch`` runs only
during desk hours, so a lifetime is an upper bound.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from ops_suite.common import paths

if TYPE_CHECKING:
    from ops_suite.common.config import Settings

log = logging.getLogger(__name__)

SESSION_LOG = "session-log.json"
KEEP_SESSIONS = 30
# The median is shown from this many finished sessions on.
MEDIAN_FROM = 2
_SCHEMA = 1


def session_log_path(settings: Settings) -> Path:
    return paths.tdx_dir(settings) / SESSION_LOG


@dataclass(frozen=True)
class Session:
    signed_in_at: datetime
    expired_seen_at: datetime | None = None

    @property
    def hours(self) -> float | None:
        """Sign-in to the first expiry seen, in hours; None while no expiry is seen."""
        if self.expired_seen_at is None:
            return None
        return (self.expired_seen_at - self.signed_in_at).total_seconds() / 3600


def _aware(text: object) -> datetime:
    if not isinstance(text, str):
        raise TypeError("not a timestamp")
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError("naive time")
    return when


class SessionLog:
    """``{"schema": 1, "sessions": [{"signed_in_at": ISO, "expired_seen_at": ISO | null}]}``,
    oldest first, in UTC.

    An unreadable file is moved aside to ``.bad`` and the log starts empty: only the lifetime
    statistic is lost.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sessions: list[Session] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data["schema"] != _SCHEMA:
                    raise ValueError("unknown schema")
                self.sessions = [
                    Session(
                        _aware(item["signed_in_at"]),
                        None
                        if item["expired_seen_at"] is None
                        else _aware(item["expired_seen_at"]),
                    )
                    for item in data["sessions"]
                ]
            except (ValueError, KeyError, TypeError):
                log.warning("session log unreadable")
                try:
                    os.replace(path, path.with_name(path.name + ".bad"))
                except OSError as error:
                    log.warning(
                        "session log not moved aside", extra={"error": type(error).__name__}
                    )

    def __repr__(self) -> str:
        return f"SessionLog(sessions={len(self.sessions)})"

    def record_sign_in(self, when: datetime) -> None:
        """Start a session at ``when``. A session still open is left without an expiry."""
        self.sessions.append(Session(when.astimezone(UTC)))
        self.sessions = self.sessions[-KEEP_SESSIONS:]
        self._write()

    def record_expiry(self, when: datetime) -> float | None:
        """Close the newest session at ``when`` if it is still open; its hours, else None.

        A second expiry without a new sign-in is not recorded, and neither is one before
        the sign-in (a clock change).
        """
        if not self.sessions:
            return None
        last = self.sessions[-1]
        when = when.astimezone(UTC)
        if last.expired_seen_at is not None or when < last.signed_in_at:
            return None
        closed = Session(last.signed_in_at, when)
        self.sessions[-1] = closed
        self._write()
        return closed.hours

    def _write(self) -> None:
        data = {
            "schema": _SCHEMA,
            "sessions": [
                {
                    "signed_in_at": session.signed_in_at.isoformat(),
                    "expired_seen_at": (
                        None
                        if session.expired_seen_at is None
                        else session.expired_seen_at.isoformat()
                    ),
                }
                for session in self.sessions
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)


def record_sign_in(settings: Settings, when: datetime) -> None:
    """``tdx login`` finished signed in. A file that cannot be written is only logged."""
    try:
        SessionLog(session_log_path(settings)).record_sign_in(when)
    except OSError as error:
        log.warning("session log not written", extra={"error": type(error).__name__})


def record_expiry(settings: Settings, when: datetime) -> float | None:
    """A command met "not signed in": close the open session and log ``tdx session expired``
    with ``lasted_hours``. The file is read again each time, because ``tdx login`` writes it
    while ``tdx watch`` runs. Returns the hours, or None if no open session was closed."""
    try:
        hours = SessionLog(session_log_path(settings)).record_expiry(when)
    except OSError as error:
        log.warning("session log not written", extra={"error": type(error).__name__})
        return None
    if hours is not None:
        log.info("tdx session expired", extra={"lasted_hours": round(hours, 1)})
    return hours


def format_lifetime(sessions: Sequence[Session], tz: tzinfo) -> list[str]:
    """The lifetime lines for ``tdx session`` and ``tdx agent status``.

    ``Last session: signed in Mon 07:12, expiry seen Mon 19:40 (12.5 h). Median of the last
    3: 11.8 h.``, preceded by a ``Current session`` line while the newest one is open, and
    "unknown" while not enough is recorded.
    """

    def at(when: datetime) -> str:
        return f"{when.astimezone(tz):%a %H:%M}"

    lines = []
    if sessions and sessions[-1].expired_seen_at is None:
        lines.append(f"Current session: signed in {at(sessions[-1].signed_in_at)}, no expiry seen.")
    finished = [session for session in sessions if session.hours is not None]
    if not finished:
        lines.append("Last session: unknown (no expiry seen after a tdx login yet).")
        return lines
    last = finished[-1]
    assert last.expired_seen_at is not None and last.hours is not None
    hours = [session.hours for session in finished if session.hours is not None]
    if len(hours) >= MEDIAN_FROM:
        median = f"Median of the last {len(hours)}: {statistics.median(hours):.1f} h."
    else:
        median = f"Median: unknown until {MEDIAN_FROM} sessions have ended."
    lines.append(
        f"Last session: signed in {at(last.signed_in_at)}, expiry seen "
        f"{at(last.expired_seen_at)} ({last.hours:.1f} h). {median}"
    )
    return lines
