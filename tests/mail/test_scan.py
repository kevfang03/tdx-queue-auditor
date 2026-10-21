import csv
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops_suite.common.config import load_settings
from ops_suite.mail.classify import MailRules
from ops_suite.mail.drop_folder import DropFolderMailSource
from ops_suite.mail.report import ReportWriter
from ops_suite.mail.scan import run_scan
from ops_suite.mail.state import SeenState

FIXTURE_DROP = Path(__file__).parent.parent / "fixtures" / "mail_drop"

EXPECTED_SKIPPED = {
    "automated_sender": 1,
    "automated_local_part": 1,
    "auto_submitted": 1,
    "list_mail": 1,
    "own_mailbox": 1,
}


@pytest.fixture
def rules(test_config: Path) -> MailRules:
    return MailRules.from_settings(load_settings(test_config).mail.work)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "appdata" / "work" / "state" / "seen.json"


@pytest.fixture
def report_path(tmp_path: Path) -> Path:
    return tmp_path / "appdata" / "work" / "reports" / "direct-requests.csv"


def _scan(drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path, **kwargs):
    source = DropFolderMailSource(drop_dir, read_only=kwargs.get("dry_run", False))
    return run_scan(source, rules, SeenState(state_path), ReportWriter(report_path), **kwargs)


def _report_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
    }


def test_first_run_reports_the_three_people(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path
) -> None:
    result = _scan(drop_dir, rules, state_path, report_path)

    assert result.scanned == 8
    assert result.flagged == 3
    assert result.skipped == EXPECTED_SKIPPED
    assert result.duplicates == 0
    assert result.rejected == 1
    assert result.report_path == report_path
    assert result.dry_run_flagged == ()

    rows = _report_rows(report_path)
    assert [row["sender"] for row in rows] == [
        "jordan.rivera@example.edu",
        "maria.okafor@example.edu",
        "priya.natarajan@example.edu",
    ]
    assert rows[0]["subject"] == "Laptop will not join the library Wi-Fi"
    assert rows[0]["received_at_utc"] == "2026-10-01T14:03:22Z"
    assert rows[2]["message_id"] == "AAMkAGI2synthetic0008"

    assert list(drop_dir.glob("*.json")) == []
    assert len(list((drop_dir / "processed").iterdir())) == 8
    assert [p.name for p in (drop_dir / "rejected").iterdir()] == ["09_malformed.json"]


def test_second_run_over_the_same_files_reports_nothing_new(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path
) -> None:
    _scan(drop_dir, rules, state_path, report_path)
    shutil.copytree(FIXTURE_DROP, drop_dir, dirs_exist_ok=True)

    result = _scan(drop_dir, rules, state_path, report_path)

    assert result.scanned == 8
    assert result.duplicates == 8
    assert result.flagged == 0
    assert result.skipped == {}
    assert result.rejected == 1
    assert len(_report_rows(report_path)) == 3
    # The repeats were filed away next to the originals instead of overwriting them.
    assert list(drop_dir.glob("*.json")) == []
    assert len(list((drop_dir / "processed").iterdir())) == 16
    assert len(list((drop_dir / "rejected").iterdir())) == 2


def test_dry_run_leaves_folder_state_and_report_untouched(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path, tmp_path: Path
) -> None:
    before = _snapshot(drop_dir)

    result = _scan(drop_dir, rules, state_path, report_path, dry_run=True)

    assert result.scanned == 8
    assert result.flagged == 3
    assert result.skipped == EXPECTED_SKIPPED
    assert result.duplicates == 0
    assert result.rejected == 1
    assert result.report_path is None
    assert result.dry_run_flagged == (
        (datetime(2026, 10, 1, 14, 3, 22, tzinfo=UTC), "<f01.20261001140322@mail.example.edu>"),
        (datetime(2026, 9, 30, 13, 15, tzinfo=UTC), "<f02.20260930131500@mail.example.edu>"),
        (datetime(2026, 10, 1, 16, 45, 10, tzinfo=UTC), "AAMkAGI2synthetic0008"),
    )
    assert _snapshot(drop_dir) == before
    assert not state_path.exists()
    assert not report_path.exists()
    assert not (tmp_path / "appdata").exists()


def test_dry_run_after_a_real_run_changes_nothing(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path
) -> None:
    _scan(drop_dir, rules, state_path, report_path)
    shutil.copytree(FIXTURE_DROP, drop_dir, dirs_exist_ok=True)
    folder_before = _snapshot(drop_dir)
    state_before = state_path.read_bytes()
    report_before = report_path.read_bytes()

    result = _scan(drop_dir, rules, state_path, report_path, dry_run=True)

    assert result.duplicates == 8
    assert result.flagged == 0
    assert _snapshot(drop_dir) == folder_before
    assert state_path.read_bytes() == state_before
    assert report_path.read_bytes() == report_before


def test_same_message_twice_in_one_run_is_reported_once(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path
) -> None:
    shutil.copy(drop_dir / "01_student_wifi.json", drop_dir / "01_student_wifi_copy.json")

    result = _scan(drop_dir, rules, state_path, report_path)

    assert result.scanned == 9
    assert result.flagged == 3
    assert result.duplicates == 1
    assert len(_report_rows(report_path)) == 3


def test_report_time_comes_from_the_clock_passed_in(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path
) -> None:
    clock = datetime.now(UTC).replace(microsecond=0)

    _scan(drop_dir, rules, state_path, report_path, now=lambda: clock)

    assert {row["reported_at_utc"] for row in _report_rows(report_path)} == {
        clock.strftime("%Y-%m-%dT%H:%M:%SZ")
    }


def test_expired_files_are_purged_after_a_real_run(
    drop_dir: Path, rules: MailRules, state_path: Path, report_path: Path
) -> None:
    (drop_dir / "processed").mkdir()
    stale = drop_dir / "processed" / "old.json"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 30 * 86_400
    os.utime(stale, (old, old))

    _scan(drop_dir, rules, state_path, report_path)

    assert not stale.exists()
