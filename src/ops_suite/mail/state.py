"""Which message ids have already been handled, so a message is reported once."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = 1


class SeenState:
    """A small JSON file: ``{"schema": 1, "seen": {"<message id>": "<ISO time first seen>"}}``."""

    def __init__(self, path: Path, *, keep_days: int = 90) -> None:
        self.path = path
        self.keep_days = keep_days
        self._seen: dict[str, datetime] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._seen = {
                message_id: datetime.fromisoformat(when)
                for message_id, when in data["seen"].items()
            }

    def has(self, message_id: str) -> bool:
        return message_id in self._seen

    def add(self, message_id: str, when: datetime) -> None:
        """Record a message id. The first time seen is kept if it is added again."""
        self._seen.setdefault(message_id, when.astimezone(UTC))

    def save(self) -> None:
        """Drop entries older than ``keep_days`` and write the file atomically."""
        cutoff = datetime.now(UTC) - timedelta(days=self.keep_days)
        self._seen = {message_id: when for message_id, when in self._seen.items() if when >= cutoff}
        data = {
            "schema": _SCHEMA,
            "seen": {message_id: when.isoformat() for message_id, when in self._seen.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)
