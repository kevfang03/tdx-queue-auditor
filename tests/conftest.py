import logging
import os
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import keyring
import keyring.backend
import pytest

from ops_suite.mail.models import MailMessage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from the real config, .env, data and log folders."""
    for name in list(os.environ):
        if name.startswith("OPS_SUITE_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPS_SUITE_CONFIG", str(tmp_path / "absent-config.toml"))
    monkeypatch.setenv("OPS_SUITE_PATHS__DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("OPS_SUITE_PATHS__LOG_DIR", str(tmp_path / "applogs"))


@pytest.fixture(autouse=True)
def regular_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as on a regular Python, also where the venv still sits on the Microsoft
    Store build, whose base prefix would make live commands refuse to run (payload 20)."""
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A keyring that lives in a dict, so no test ever touches the real system keychain."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.passwords.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    """Every test gets a fresh, empty in-memory keyring."""
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    return backend


@pytest.fixture(autouse=True)
def close_log_handlers():
    """Release the log file after each test so tmp_path can be cleaned up on Windows."""
    yield
    logger = logging.getLogger("ops_suite")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture
def test_config() -> Path:
    return FIXTURES / "config.test.toml"


@pytest.fixture
def drop_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the fixture drop folder."""
    return Path(shutil.copytree(FIXTURES / "mail_drop", tmp_path / "drop"))


@pytest.fixture
def make_message() -> Callable[..., MailMessage]:
    def _make(**overrides: object) -> MailMessage:
        fields: dict[str, object] = {
            "id": "<m1@mail.example.edu>",
            "conversation_id": None,
            "received_at": datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
            "sender": "person@example.edu",
            "to": ("shared-mailbox@example.edu",),
            "cc": (),
            "subject": "Hello",
            "body": "Body text",
            "is_html": False,
            "has_attachments": False,
            "headers": {},
            "source_ref": "m1.json",
        }
        fields.update(overrides)
        return MailMessage(**fields)

    return _make
