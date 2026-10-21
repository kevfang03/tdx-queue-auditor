"""Logging setup: a readable console handler and a JSON-lines file.

Logging rule: records may carry counts, reason codes, message ids and file names. They must
never carry a sender address, recipient, subject or body text.
"""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "ops_suite"
LOG_FILE_NAME = "ops-suite.log"

_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 5
_CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Attributes every LogRecord has; anything else was passed through ``extra=``.
_STANDARD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonLineFormatter(logging.Formatter):
    """One JSON object per record: time, level, logger, message and any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                entry[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            # The type only: exception messages and tracebacks can quote the data being handled.
            entry["exception"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(log_dir: Path, *, verbose: bool = False) -> None:
    """Attach the console and file handlers to the ``ops_suite`` logger.

    Safe to call again: handlers from an earlier call are closed and replaced.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(console)

    log_file = RotatingFileHandler(
        log_dir / LOG_FILE_NAME, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    log_file.setFormatter(JsonLineFormatter())
    logger.addHandler(log_file)
