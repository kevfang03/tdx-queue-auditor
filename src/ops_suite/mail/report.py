"""The local CSV report of flagged messages.

The report holds sender and subject, so it lives in the data folder, never in the repo.
The message body is never written.
"""

import csv
from datetime import UTC, datetime
from pathlib import Path

from ops_suite.mail.models import MailMessage

COLUMNS = ("received_at_utc", "sender", "subject", "message_id", "reported_at_utc")

_SUBJECT_MAX = 200
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _utc_text(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_subject(subject: str) -> str:
    return " ".join(subject.split())[:_SUBJECT_MAX]


def _guard(value: str) -> str:
    """Stop a spreadsheet from running a cell that mail content made look like a formula."""
    return "'" + value if value.startswith(_FORMULA_STARTS) else value


class ReportWriter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, message: MailMessage, reported_at: datetime) -> None:
        """Add one row, creating the file with its header row if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        # utf-8-sig writes the byte-order mark Excel needs; only the first write may add it.
        encoding = "utf-8-sig" if is_new else "utf-8"
        with self.path.open("a", encoding=encoding, newline="") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(COLUMNS)
            writer.writerow(
                [
                    _utc_text(message.received_at),
                    _guard(message.sender),
                    _guard(_clean_subject(message.subject)),
                    _guard(message.id),
                    _utc_text(reported_at),
                ]
            )
