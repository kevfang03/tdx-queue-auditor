import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx.session_log import (
    KEEP_SESSIONS,
    Session,
    SessionLog,
    format_lifetime,
)

NY = ZoneInfo("America/New_York")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "tdx" / "session-log.json"


def test_a_sign_in_then_an_expiry_gives_the_lifetime(path: Path) -> None:
    log = SessionLog(path)
    log.record_sign_in(at(19, 7, 12))

    hours = log.record_expiry(at(19, 19, 42))

    assert hours == pytest.approx(12.5)
    assert SessionLog(path).sessions == [
        Session(at(19, 7, 12).astimezone(UTC), at(19, 19, 42).astimezone(UTC))
    ]


def test_a_second_expiry_without_a_new_sign_in_is_not_recorded(path: Path) -> None:
    log = SessionLog(path)
    log.record_sign_in(at(19, 7, 0))
    log.record_expiry(at(19, 19, 0))
    written = path.read_text(encoding="utf-8")

    assert log.record_expiry(at(19, 20, 0)) is None
    assert SessionLog(path).record_expiry(at(20, 8, 0)) is None
    assert path.read_text(encoding="utf-8") == written


def test_an_expiry_with_no_sign_in_recorded_writes_nothing(path: Path) -> None:
    assert SessionLog(path).record_expiry(at(19, 9)) is None
    assert not path.exists()


def test_an_expiry_before_the_sign_in_is_ignored(path: Path) -> None:
    log = SessionLog(path)
    log.record_sign_in(at(19, 9))

    assert log.record_expiry(at(19, 8)) is None
    assert log.sessions[-1].expired_seen_at is None


def test_a_new_sign_in_leaves_the_open_session_open(path: Path) -> None:
    log = SessionLog(path)
    log.record_sign_in(at(19, 7))
    log.record_sign_in(at(19, 12))

    assert log.record_expiry(at(19, 18)) == pytest.approx(6.0)
    assert [s.hours for s in SessionLog(path).sessions] == [None, pytest.approx(6.0)]


def test_only_the_last_30_sessions_are_kept(path: Path) -> None:
    log = SessionLog(path)
    first = at(1, 8)
    for day in range(KEEP_SESSIONS + 5):
        start = first + timedelta(days=day)
        log.record_sign_in(start)
        log.record_expiry(start + timedelta(hours=10))

    sessions = SessionLog(path).sessions
    assert len(sessions) == KEEP_SESSIONS == 30
    assert sessions[0].signed_in_at == (first + timedelta(days=5)).astimezone(UTC)


def test_the_file_holds_timestamps_only(path: Path) -> None:
    log = SessionLog(path)
    log.record_sign_in(at(19, 7, 12))
    log.record_expiry(at(19, 19, 42))
    log.record_sign_in(at(20, 7, 5))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "schema": 1,
        "sessions": [
            {
                "signed_in_at": "2026-10-19T11:12:00+00:00",
                "expired_seen_at": "2026-10-19T23:42:00+00:00",
            },
            {"signed_in_at": "2026-10-20T11:05:00+00:00", "expired_seen_at": None},
        ],
    }


def test_an_unreadable_file_is_moved_aside_and_the_log_starts_empty(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    log = SessionLog(path)

    assert log.sessions == []
    assert path.with_name("session-log.json.bad").exists()
    log.record_sign_in(at(19, 7))
    assert len(SessionLog(path).sessions) == 1


def _session(day: int, start: tuple[int, int], end: tuple[int, int] | None) -> Session:
    return Session(at(day, *start), at(day, *end) if end is not None else None)


def test_the_lifetime_line_with_the_median() -> None:
    sessions = [
        _session(15, (7, 0), (18, 48)),  # 11.8 h
        _session(16, (7, 0), (17, 0)),  # 10.0 h
        _session(19, (7, 12), (19, 42)),  # 12.5 h
    ]

    assert format_lifetime(sessions, NY) == [
        "Last session: signed in Mon 07:12, expiry seen Mon 19:42 (12.5 h). "
        "Median of the last 3: 11.8 h."
    ]


def test_the_lifetime_shows_the_current_session_first() -> None:
    sessions = [_session(16, (7, 0), (17, 6)), _session(19, (7, 12), (19, 42))]  # 10.1, 12.5
    sessions.append(_session(20, (7, 5), None))

    assert format_lifetime(sessions, NY) == [
        "Current session: signed in Tue 07:05, no expiry seen.",
        "Last session: signed in Mon 07:12, expiry seen Mon 19:42 (12.5 h). "
        "Median of the last 2: 11.3 h.",
    ]


def test_the_lifetime_is_unknown_until_enough_is_recorded() -> None:
    assert format_lifetime([], NY) == [
        "Last session: unknown (no expiry seen after a tdx login yet)."
    ]
    assert format_lifetime([_session(19, (7, 12), None)], NY) == [
        "Current session: signed in Mon 07:12, no expiry seen.",
        "Last session: unknown (no expiry seen after a tdx login yet).",
    ]
    assert format_lifetime([_session(19, (7, 12), (19, 42))], NY) == [
        "Last session: signed in Mon 07:12, expiry seen Mon 19:42 (12.5 h). "
        "Median: unknown until 2 sessions have ended."
    ]
