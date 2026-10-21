"""The local desk-member memory: people seen as desk staff in earlier audits.

Only the strong signals are remembered (``rules.strong_desk_names``: Prim Resp, private-note
authors, merge-notice authors, authors of replies with the desk signature), never the
2+-tickets guess, so a frequently copied outsider
cannot become desk for good. The rules are in plain language in docs/tdx-rules.md.

``desk-members.json``: ``{"schema": 1, "salt": "<32 hex>", "members": {"<64 hex sha256>":
"<ISO UTC last seen>"}}``. A member is the salted SHA-256 of the name's ``name_key``, so
"Example, Alex" and "Alex Example" are the same member. Entries not seen for
``forget_days`` are dropped on save.

Personal data rule: names are student data. The file, log records, ``repr()`` and exceptions
carry hashes, times and counts only; the salt is never logged.
"""

import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops_suite.tdx.names import name_key

log = logging.getLogger(__name__)

MEMORY_FILE_NAME = "desk-members.json"
DEFAULT_FORGET_DAYS = 180
_SCHEMA = 1
_SALT = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class DeskMemory:
    """Hashed desk members and when each was last seen.

    An unreadable file (corrupt, or an unknown schema) is moved aside to ``.bad`` and the
    memory starts empty with a new salt: the run falls back to the in-run rules alone.
    """

    def __init__(self, path: Path, *, forget_days: int = DEFAULT_FORGET_DAYS) -> None:
        self.path = path
        self.forget_days = forget_days
        self._salt = secrets.token_hex(16)
        self._members: dict[str, datetime] = {}
        if path.exists():
            try:
                self._salt, self._members = _parse(path.read_text(encoding="utf-8"))
            except (ValueError, KeyError, TypeError, AttributeError):
                log.warning("desk memory unreadable")
                try:
                    os.replace(path, path.with_name(path.name + ".bad"))
                except OSError as error:
                    log.warning(
                        "desk memory not moved aside", extra={"error": type(error).__name__}
                    )

    def __repr__(self) -> str:
        return f"DeskMemory(<{len(self._members)} members>)"

    def __len__(self) -> int:
        return len(self._members)

    def _key(self, name: str) -> str | None:
        words = name_key(name)
        if not words:
            return None
        text = self._salt + "\x1f" + " ".join(words)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def knows(self, name: str) -> bool:
        key = self._key(name)
        return key is not None and key in self._members

    def remember(self, names: Iterable[str], now: datetime) -> None:
        """Store each name, or refresh its last-seen time."""
        seen = now.astimezone(UTC)
        for name in names:
            key = self._key(name)
            if key is not None:
                self._members[key] = max(seen, self._members.get(key, seen))

    def save(self, now: datetime) -> None:
        """Forget members not seen for ``forget_days``, then write the file atomically."""
        cutoff = now.astimezone(UTC) - timedelta(days=self.forget_days)
        self._members = {key: seen for key, seen in self._members.items() if seen >= cutoff}
        data = {
            "schema": _SCHEMA,
            "salt": self._salt,
            "members": {key: seen.isoformat() for key, seen in sorted(self._members.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)


def _parse(text: str) -> tuple[str, dict[str, datetime]]:
    data = json.loads(text)
    if data["schema"] != _SCHEMA:
        raise ValueError("unknown schema")
    salt = data["salt"]
    if not isinstance(salt, str) or not _SALT.fullmatch(salt):
        raise ValueError("bad salt")
    members: dict[str, datetime] = {}
    for key, seen_text in data["members"].items():
        if not _DIGEST.fullmatch(key):
            raise ValueError("bad member key")
        seen = datetime.fromisoformat(seen_text)
        if seen.tzinfo is None:
            raise ValueError("naive time")
        members[key] = seen.astimezone(UTC)
    return salt, members
