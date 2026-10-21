import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops_suite.mail.drop_folder import DropFileError, DropFolderMailSource, parse_drop_file

DAY = 86_400

VALID_FILES = [
    "01_student_wifi.json",
    "02_staff_printer.json",
    "03_tdx_notification.json",
    "04_noreply_vendor.json",
    "05_auto_reply.json",
    "06_newsletter.json",
    "07_desk_reply.json",
    "08_mixed_case_list_to.json",
]


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
    }


def _write(path: Path, **fields: object) -> Path:
    data = {"id": "x1", "received_at": "2026-10-01T12:00:00Z", "from": "person@example.edu"}
    data.update(fields)
    path.write_text(json.dumps({k: v for k, v in data.items() if v is not ...}), encoding="utf-8")
    return path


def _age(path: Path, days: float) -> None:
    old = time.time() - days * DAY
    os.utime(path, (old, old))


def test_fetch_reads_top_level_json_in_name_order(drop_dir: Path) -> None:
    (drop_dir / "notes.txt").write_text("not a message", encoding="utf-8")
    (drop_dir / "nested").mkdir()
    _write(drop_dir / "nested" / "00_ignored.json")

    source = DropFolderMailSource(drop_dir, read_only=True)
    messages = list(source.fetch())

    assert [m.source_ref for m in messages] == VALID_FILES
    assert source.rejected == 1


def test_student_fixture_parses_to_expected_fields(drop_dir: Path) -> None:
    message = parse_drop_file(drop_dir / "01_student_wifi.json")

    assert message.id == "<f01.20261001140322@mail.example.edu>"
    assert message.conversation_id == "AAQkAGI2conversation0001"
    assert message.received_at == datetime(2026, 10, 1, 14, 3, 22, tzinfo=UTC)
    assert message.sender == "jordan.rivera@example.edu"
    assert message.to == ("shared-mailbox@example.edu",)
    assert message.cc == ()
    assert message.subject == "Laptop will not join the library Wi-Fi"
    assert message.body.startswith("<html>")
    assert message.is_html is True
    assert message.has_attachments is False
    assert message.headers == {}
    assert message.source_ref == "01_student_wifi.json"


def test_offset_time_is_converted_to_utc_and_to_string_is_split(drop_dir: Path) -> None:
    message = parse_drop_file(drop_dir / "02_staff_printer.json")

    assert message.received_at == datetime(2026, 9, 30, 13, 15, tzinfo=UTC)
    assert message.received_at.utcoffset().total_seconds() == 0
    assert message.to == ("shared-mailbox@example.edu", "frontdesk@example.edu")
    assert message.cc == ("tomas.lindqvist@example.edu",)
    assert message.is_html is False
    assert message.has_attachments is True


def test_mixed_case_fixture_is_normalised(drop_dir: Path) -> None:
    message = parse_drop_file(drop_dir / "08_mixed_case_list_to.json")

    # No internet_message_id, so the flow's own id is used.
    assert message.id == "AAMkAGI2synthetic0008"
    assert message.sender == "priya.natarajan@example.edu"
    assert message.to == ("shared-mailbox@example.edu", "helpdesk@example.edu")
    assert message.cc == ()
    # No offset in the file: treated as UTC.
    assert message.received_at == datetime(2026, 10, 1, 16, 45, 10, tzinfo=UTC)
    assert message.is_html is False
    assert message.has_attachments is False
    assert message.headers == {}


@pytest.mark.parametrize(
    ("file_name", "sender", "headers"),
    [
        ("03_tdx_notification.json", "notifications@tdx.example.edu", {}),
        ("04_noreply_vendor.json", "no-reply@vendor.example.com", {}),
        ("05_auto_reply.json", "sam.whitfield@example.edu", {"auto-submitted": "auto-replied"}),
        (
            "06_newsletter.json",
            "campus-news@lists.example.edu",
            {"list-unsubscribe": "<mailto:leave-campus-news@lists.example.edu>"},
        ),
        ("07_desk_reply.json", "shared-mailbox@example.edu", {}),
    ],
)
def test_remaining_fixtures_parse(
    drop_dir: Path, file_name: str, sender: str, headers: dict[str, str]
) -> None:
    message = parse_drop_file(drop_dir / file_name)

    assert message.sender == sender
    assert message.headers == headers
    assert message.received_at.tzinfo is UTC


def test_optional_fields_default(tmp_path: Path) -> None:
    message = parse_drop_file(_write(tmp_path / "minimal.json", subject=None, is_html="True"))

    assert message.id == "x1"
    assert message.conversation_id is None
    assert message.subject == ""
    assert message.body == ""
    assert message.to == ()
    assert message.is_html is True


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"id": ...}, "missing_id"),
        ({"from": ...}, "missing_from"),
        ({"from": "Nobody <>"}, "missing_from"),
        ({"received_at": ...}, "missing_received_at"),
        ({"received_at": "yesterday"}, "bad_received_at"),
        ({"to": 5}, "bad_to"),
        ({"headers": ["Auto-Submitted"]}, "bad_headers"),
        ({"subject": 12}, "bad_subject"),
    ],
)
def test_unusable_files_are_refused_with_a_reason(
    tmp_path: Path, fields: dict[str, object], reason: str
) -> None:
    with pytest.raises(DropFileError, match=reason):
        parse_drop_file(_write(tmp_path / "bad.json", **fields))


def test_non_object_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(DropFileError, match="not_an_object"):
        parse_drop_file(path)


def test_malformed_file_is_counted_and_moved_to_rejected(drop_dir: Path) -> None:
    source = DropFolderMailSource(drop_dir)

    messages = list(source.fetch())

    assert len(messages) == 8
    assert source.rejected == 1
    assert not (drop_dir / "09_malformed.json").exists()
    assert (drop_dir / "rejected" / "09_malformed.json").is_file()


def test_acknowledge_moves_file_to_processed(drop_dir: Path) -> None:
    _age(drop_dir / "01_student_wifi.json", days=30)
    source = DropFolderMailSource(drop_dir)
    first = next(source.fetch())

    source.acknowledge(first)

    moved = drop_dir / "processed" / "01_student_wifi.json"
    assert not (drop_dir / "01_student_wifi.json").exists()
    assert moved.is_file()
    # Modified time is reset, so retention counts from handling.
    assert time.time() - moved.stat().st_mtime < 60


def test_existing_target_name_gets_a_numeric_suffix(drop_dir: Path) -> None:
    source = DropFolderMailSource(drop_dir)
    (drop_dir / "processed").mkdir()
    (drop_dir / "processed" / "01_student_wifi.json").write_text("{}", encoding="utf-8")
    (drop_dir / "processed" / "01_student_wifi-1.json").write_text("{}", encoding="utf-8")

    source.acknowledge(next(source.fetch()))

    assert (drop_dir / "processed" / "01_student_wifi-2.json").is_file()
    assert not (drop_dir / "01_student_wifi.json").exists()


def test_purge_removes_only_old_handled_files(drop_dir: Path) -> None:
    source = DropFolderMailSource(drop_dir, retention_days=7)
    for message in source.fetch():
        source.acknowledge(message)
    old_processed = drop_dir / "processed" / "01_student_wifi.json"
    old_rejected = drop_dir / "rejected" / "09_malformed.json"
    recent_processed = drop_dir / "processed" / "02_staff_printer.json"
    _age(old_processed, days=8)
    _age(old_rejected, days=8)
    _age(recent_processed, days=6)
    old_new_file = _write(drop_dir / "10_waiting.json")
    _age(old_new_file, days=30)
    outside = _write(drop_dir.parent / "outside.json")
    _age(outside, days=30)

    removed = source.purge_expired()

    assert removed == 2
    assert not old_processed.exists()
    assert not old_rejected.exists()
    assert recent_processed.is_file()
    assert old_new_file.is_file()
    assert outside.is_file()
    assert len(list((drop_dir / "processed").iterdir())) == 7


def test_read_only_changes_nothing_on_disk(drop_dir: Path) -> None:
    (drop_dir / "processed").mkdir()
    stale = _write(drop_dir / "processed" / "old.json")
    _age(stale, days=30)
    before = _snapshot(drop_dir)
    source = DropFolderMailSource(drop_dir, retention_days=7, read_only=True)

    messages = list(source.fetch())
    for message in messages:
        source.acknowledge(message)
    removed = source.purge_expired()

    assert len(messages) == 8
    assert source.rejected == 1
    assert removed == 0
    assert _snapshot(drop_dir) == before
