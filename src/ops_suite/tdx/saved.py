"""The dashboard address saved by ``tdx set-dashboard``. Pure file handling; no Playwright.

TDX opens each person's homepage dashboard, and switching to another dashboard changes only
the inner frame, not the page address. ``tdx set-dashboard`` saves that inner frame's full
address so ``tdx rows`` can start there. The address is per-person local state, like the
browser profile: it lives in the data folder, and it is only ever shown through
``describe_url`` or ``describe_path`` (no query string, long digit runs masked).
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ops_suite.tdx.session import describe_url

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, repr=False)
class SavedDashboard:
    url: str  # raw; only ever shown through describe_url
    saved_at: datetime  # aware UTC

    def __repr__(self) -> str:
        return (
            f"SavedDashboard(url={describe_url(self.url)!r}, "
            f"saved_at={self.saved_at.isoformat()!r})"
        )


def load_saved_dashboard(path: Path) -> SavedDashboard | None:
    """The saved address, or None if the file is missing, unreadable or malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        log.warning("saved dashboard file unreadable", extra={"error": type(error).__name__})
        return None
    try:
        data = json.loads(text)
        url = data["url"]
        saved_at = datetime.fromisoformat(data["saved_at"])
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise ValueError("url is not an http(s) address")
        if saved_at.tzinfo is None:
            raise ValueError("saved_at has no time zone")
    except (ValueError, KeyError, TypeError) as error:
        # The type only: the message could quote the file's contents.
        log.warning("saved dashboard file malformed", extra={"error": type(error).__name__})
        return None
    return SavedDashboard(url, saved_at.astimezone(UTC))


def save_dashboard(path: Path, url: str, now: datetime) -> SavedDashboard:
    """Write the address atomically: a temporary file in the same folder, then a replace."""
    saved = SavedDashboard(url, now.astimezone(UTC))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".dashboard-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            json.dump({"url": saved.url, "saved_at": saved.saved_at.isoformat()}, temp)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    log.info("dashboard address saved")
    return saved


def forget_dashboard(path: Path) -> None:
    """Remove the saved address; no error if there is none."""
    path.unlink(missing_ok=True)
    log.info("saved dashboard address removed")
