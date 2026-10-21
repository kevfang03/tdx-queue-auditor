import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from ops_suite.common.logsetup import LOG_FILE_NAME, configure_logging


def _records(log_dir: Path) -> list[dict]:
    lines = (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_file_gets_one_json_object_per_line(tmp_path: Path) -> None:
    log_dir = tmp_path / "applogs"
    configure_logging(log_dir)
    log = logging.getLogger("ops_suite.test")

    log.info("scan finished: %d flagged", 3, extra={"flagged": 3})
    log.warning("rejected drop file %s (%s)", "09_malformed.json", "invalid_json")

    first, second = _records(log_dir)
    assert first["level"] == "INFO"
    assert first["logger"] == "ops_suite.test"
    assert first["message"] == "scan finished: 3 flagged"
    assert first["flagged"] == 3
    assert first["time"].endswith("+00:00")
    assert second["level"] == "WARNING"
    assert second["message"] == "rejected drop file 09_malformed.json (invalid_json)"


def test_console_is_readable_and_quiet_unless_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(tmp_path / "applogs")
    log = logging.getLogger("ops_suite.test")

    log.info("routine detail")
    log.warning("needs attention")

    err = capsys.readouterr().err
    assert "WARNING ops_suite.test: needs attention" in err
    assert "routine detail" not in err


def test_debug_is_recorded_only_when_verbose(tmp_path: Path) -> None:
    log = logging.getLogger("ops_suite.test")

    configure_logging(tmp_path / "quiet")
    log.debug("fine detail")
    configure_logging(tmp_path / "verbose", verbose=True)
    log.debug("fine detail")

    assert (tmp_path / "quiet" / LOG_FILE_NAME).read_text(encoding="utf-8") == ""
    assert [r["message"] for r in _records(tmp_path / "verbose")] == ["fine detail"]


def test_configuring_twice_does_not_duplicate_handlers(tmp_path: Path) -> None:
    log_dir = tmp_path / "applogs"
    configure_logging(log_dir)
    configure_logging(log_dir)

    logging.getLogger("ops_suite.test").info("once")

    handlers = logging.getLogger("ops_suite").handlers
    assert len(handlers) == 2
    assert sum(isinstance(h, RotatingFileHandler) for h in handlers) == 1
    assert len(_records(log_dir)) == 1


def test_exceptions_are_logged_by_type_only(tmp_path: Path) -> None:
    log_dir = tmp_path / "applogs"
    configure_logging(log_dir)

    try:
        raise ValueError("person@example.edu wrote something private")
    except ValueError:
        logging.getLogger("ops_suite.test").exception("could not handle a message")

    (record,) = _records(log_dir)
    assert record["exception"] == "ValueError"
    assert "person@example.edu" not in (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
