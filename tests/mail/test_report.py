import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops_suite.mail.report import COLUMNS, ReportWriter

REPORTED = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_header_written_once_and_each_append_adds_one_row(tmp_path: Path, make_message) -> None:
    path = tmp_path / "reports" / "direct-requests.csv"
    writer = ReportWriter(path)

    writer.append(make_message(id="<m1@mail.example.edu>", subject="First"), REPORTED)
    lines_after_one = path.read_text(encoding="utf-8-sig").splitlines()
    writer.append(make_message(id="<m2@mail.example.edu>", subject="Second"), REPORTED)
    lines_after_two = path.read_text(encoding="utf-8-sig").splitlines()

    assert lines_after_one[0] == ",".join(COLUMNS)
    assert len(lines_after_one) == 2
    assert len(lines_after_two) == 3
    assert lines_after_two[:2] == lines_after_one
    assert sum(line.startswith("received_at_utc") for line in lines_after_two) == 1


def test_row_values(tmp_path: Path, make_message) -> None:
    path = tmp_path / "direct-requests.csv"

    ReportWriter(path).append(make_message(), REPORTED)

    assert _rows(path) == [
        {
            "received_at_utc": "2026-10-01T14:00:00Z",
            "sender": "person@example.edu",
            "subject": "Hello",
            "message_id": "<m1@mail.example.edu>",
            "reported_at_utc": "2026-10-01T15:00:00Z",
        }
    ]


def test_file_starts_with_one_byte_order_mark(tmp_path: Path, make_message) -> None:
    path = tmp_path / "direct-requests.csv"
    writer = ReportWriter(path)

    writer.append(make_message(), REPORTED)
    writer.append(make_message(), REPORTED)

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1


def test_subject_whitespace_is_collapsed(tmp_path: Path, make_message) -> None:
    path = tmp_path / "direct-requests.csv"

    ReportWriter(path).append(make_message(subject="  Wi-Fi\r\n is   down\tagain \n"), REPORTED)

    assert _rows(path)[0]["subject"] == "Wi-Fi is down again"
    assert len(path.read_text(encoding="utf-8-sig").splitlines()) == 2


def test_subject_is_cut_to_200_characters(tmp_path: Path, make_message) -> None:
    path = tmp_path / "direct-requests.csv"

    ReportWriter(path).append(make_message(subject="word " * 100), REPORTED)

    subject = _rows(path)[0]["subject"]
    assert len(subject) == 200
    assert subject == ("word " * 100)[:200]


@pytest.mark.parametrize("start", ["=", "+", "-", "@"])
def test_formula_like_subject_gets_a_leading_quote(
    tmp_path: Path, make_message, start: str
) -> None:
    path = tmp_path / "direct-requests.csv"
    subject = f'{start}HYPERLINK("http://example.com","open")'

    ReportWriter(path).append(make_message(subject=subject), REPORTED)

    assert _rows(path)[0]["subject"] == "'" + subject


@pytest.mark.parametrize("sender", ["=cmd@example.com", "\tperson@example.com"])
def test_formula_like_sender_gets_a_leading_quote(
    tmp_path: Path, make_message, sender: str
) -> None:
    path = tmp_path / "direct-requests.csv"

    ReportWriter(path).append(make_message(sender=sender), REPORTED)

    assert _rows(path)[0]["sender"] == "'" + sender


def test_body_never_appears_in_the_file(tmp_path: Path, make_message) -> None:
    path = tmp_path / "direct-requests.csv"
    body = "My student record number is 00000000 and my phone is 215-555-0142."

    ReportWriter(path).append(make_message(body=body), REPORTED)

    text = path.read_text(encoding="utf-8-sig")
    assert body not in text
    assert "00000000" not in text
    assert "215-555-0142" not in text
