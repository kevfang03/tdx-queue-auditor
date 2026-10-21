"""Mail adapter that reads one JSON file per message from a local folder.

A Power Automate flow writes the files into a synced OneDrive folder; this adapter only ever
sees the local copy, so the same code serves sample mail and real mail.

Folder layout:
    <drop_dir>/*.json       new files (only this level is read, sorted by file name)
    <drop_dir>/processed/   files that were handled
    <drop_dir>/rejected/    files that could not be parsed or lack a required field
"""

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from ops_suite.mail.models import MailMessage

log = logging.getLogger(__name__)

PROCESSED_DIR = "processed"
REJECTED_DIR = "rejected"

_SECONDS_PER_DAY = 86_400
_ANGLE_ADDRESS = re.compile(r"<([^<>]*)>\s*$")


class DropFileError(Exception):
    """A drop file cannot become a message. The text is a short code that is safe to log."""


def _address(value: str) -> str:
    """``Name <Address>`` or a bare address, reduced to the lower-cased address."""
    match = _ANGLE_ADDRESS.search(value)
    return (match.group(1) if match else value).strip().lower()


def _required_text(data: dict, field: str) -> str:
    value = data.get(field)
    if value is None or value == "":
        raise DropFileError(f"missing_{field}")
    if not isinstance(value, str):
        raise DropFileError(f"bad_{field}")
    return value


def _optional_text(data: dict, field: str) -> str:
    value = data.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DropFileError(f"bad_{field}")
    return value


def _addresses(data: dict, field: str) -> tuple[str, ...]:
    value = data.get(field)
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(";")
    elif isinstance(value, list) and all(isinstance(part, str) for part in value):
        parts = value
    else:
        raise DropFileError(f"bad_{field}")
    return tuple(address for address in map(_address, parts) if address)


def _flag(data: dict, field: str) -> bool:
    value = data.get(field)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    # Flow expressions often write booleans as text.
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise DropFileError(f"bad_{field}")


def _received_at(data: dict) -> datetime:
    try:
        parsed = datetime.fromisoformat(_required_text(data, "received_at"))
    except ValueError:
        raise DropFileError("bad_received_at") from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _headers(data: dict) -> MappingProxyType[str, str]:
    value = data.get("headers")
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, dict):
        raise DropFileError("bad_headers")
    return MappingProxyType({str(name).lower(): str(text) for name, text in value.items()})


def parse_drop_file(path: Path) -> MailMessage:
    """Read one drop file. Raises DropFileError with a reason code if it is unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        raise DropFileError("invalid_encoding") from None
    except json.JSONDecodeError:
        raise DropFileError("invalid_json") from None
    if not isinstance(data, dict):
        raise DropFileError("not_an_object")

    flow_id = _required_text(data, "id")
    received_at = _received_at(data)
    sender = _address(_required_text(data, "from"))
    if not sender:
        raise DropFileError("missing_from")

    return MailMessage(
        id=_optional_text(data, "internet_message_id").strip() or flow_id,
        conversation_id=_optional_text(data, "conversation_id") or None,
        received_at=received_at,
        sender=sender,
        to=_addresses(data, "to"),
        cc=_addresses(data, "cc"),
        subject=_optional_text(data, "subject"),
        body=_optional_text(data, "body"),
        is_html=_flag(data, "is_html"),
        has_attachments=_flag(data, "has_attachments"),
        headers=_headers(data),
        source_ref=path.name,
    )


class DropFolderMailSource:
    """Reads messages from ``drop_dir`` and files them away once handled."""

    def __init__(self, drop_dir: Path, *, retention_days: int = 7, read_only: bool = False) -> None:
        self.drop_dir = drop_dir
        self.retention_days = retention_days
        self.read_only = read_only
        self.rejected = 0

    def fetch(self) -> Iterator[MailMessage]:
        self.rejected = 0
        new_files = sorted(
            (path for path in self.drop_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.name,
        )
        for path in new_files:
            try:
                message = parse_drop_file(path)
            except DropFileError as error:
                self.rejected += 1
                log.warning("rejected drop file %s (%s)", path.name, error)
                if not self.read_only:
                    self._move(path, REJECTED_DIR)
                continue
            yield message

    def acknowledge(self, message: MailMessage) -> None:
        """Move the message's file to ``processed/``."""
        if self.read_only:
            return
        path = self.drop_dir / message.source_ref
        if not path.is_file():
            log.warning("cannot acknowledge %s: not in the drop folder", message.source_ref)
            return
        self._move(path, PROCESSED_DIR)

    def purge_expired(self) -> int:
        """Delete handled files older than ``retention_days``. Returns how many were removed."""
        if self.read_only:
            return 0
        cutoff = time.time() - self.retention_days * _SECONDS_PER_DAY
        removed = 0
        for subfolder in (PROCESSED_DIR, REJECTED_DIR):
            folder = self.drop_dir / subfolder
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        if removed:
            log.info("purged %d expired file(s) from the drop folder", removed)
        return removed

    def _move(self, path: Path, subfolder: str) -> None:
        target_dir = self.drop_dir / subfolder
        target_dir.mkdir(exist_ok=True)
        target = target_dir / path.name
        suffix = 1
        while target.exists():
            target = target_dir / f"{path.stem}-{suffix}{path.suffix}"
            suffix += 1
        path.replace(target)
        # Retention counts from handling, not from when the flow wrote the file.
        os.utime(target)
