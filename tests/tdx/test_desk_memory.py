import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ops_suite.tdx.desk_memory import MEMORY_FILE_NAME, DeskMemory

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
ASSISTANT = "Quinn Samplehelper"
OTHER_ASSISTANT = "Drew Fixtureworth"
# Invented names that must never show up in plain text in the file, logs or repr().
NAMES = [ASSISTANT, OTHER_ASSISTANT, "Avery Q. Testalot"]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "data" / MEMORY_FILE_NAME


def test_a_new_memory_knows_nobody_and_writes_nothing_until_saved(path: Path) -> None:
    memory = DeskMemory(path)
    assert len(memory) == 0
    assert not memory.knows(ASSISTANT)
    assert not path.exists()


def test_round_trip(path: Path) -> None:
    memory = DeskMemory(path)
    memory.remember([ASSISTANT, OTHER_ASSISTANT], NOW)
    memory.save(NOW)

    again = DeskMemory(path)
    assert len(again) == 2
    assert again.knows(ASSISTANT)
    assert again.knows(OTHER_ASSISTANT)
    assert not again.knows("Sam Synthetic")
    assert not path.with_name(path.name + ".tmp").exists()


def test_the_file_has_the_documented_shape(path: Path) -> None:
    memory = DeskMemory(path)
    memory.remember([ASSISTANT], NOW)
    memory.save(NOW)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"schema", "salt", "members"}
    assert data["schema"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", data["salt"])
    [(key, seen)] = data["members"].items()
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert datetime.fromisoformat(seen) == NOW


def test_last_first_and_first_last_are_the_same_member(path: Path) -> None:
    memory = DeskMemory(path)
    memory.remember(["Samplehelper, Quinn"], NOW)
    assert memory.knows(ASSISTANT)
    assert memory.knows("quinn  SAMPLEHELPER")
    memory.remember([ASSISTANT], NOW)
    assert len(memory) == 1


def test_empty_names_are_ignored(path: Path) -> None:
    memory = DeskMemory(path)
    memory.remember(["", "  ", ", ."], NOW)
    assert len(memory) == 0
    assert not memory.knows("")


def test_members_unseen_for_forget_days_are_dropped_on_save(path: Path) -> None:
    memory = DeskMemory(path, forget_days=180)
    memory.remember([ASSISTANT], NOW - timedelta(days=181))
    memory.remember([OTHER_ASSISTANT], NOW - timedelta(days=180))
    memory.save(NOW)

    again = DeskMemory(path, forget_days=180)
    assert not again.knows(ASSISTANT)
    assert again.knows(OTHER_ASSISTANT)
    assert len(again) == 1


def test_seeing_a_member_again_refreshes_last_seen(path: Path) -> None:
    memory = DeskMemory(path, forget_days=30)
    memory.remember([ASSISTANT], NOW - timedelta(days=100))
    memory.remember([ASSISTANT], NOW - timedelta(days=1))
    memory.remember([ASSISTANT], NOW - timedelta(days=50))  # an older sighting never wins
    memory.save(NOW)
    assert DeskMemory(path, forget_days=30).knows(ASSISTANT)


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"schema": 2, "salt": "' + "a" * 32 + '", "members": {}}',
        '{"schema": 1, "members": {}}',
        '{"schema": 1, "salt": "short", "members": {}}',
        '{"schema": 1, "salt": "' + "a" * 32 + '", "members": {"Quinn Samplehelper": '
        '"2026-10-16T14:00:00+00:00"}}',
        '{"schema": 1, "salt": "' + "a" * 32 + '", "members": {"' + "b" * 64 + '": "nope"}}',
        '{"schema": 1, "salt": "' + "a" * 32 + '", "members": {"' + "b" * 64 + '": '
        '"2026-10-16T14:00:00"}}',
    ],
)
def test_a_corrupt_file_is_moved_aside_and_starts_empty_with_a_new_salt(
    path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="ops_suite"):
        memory = DeskMemory(path)

    assert len(memory) == 0
    assert not path.exists()
    assert path.with_name(path.name + ".bad").read_text(encoding="utf-8") == content
    assert [r.getMessage() for r in caplog.records] == ["desk memory unreadable"]
    memory.remember([ASSISTANT], NOW)
    memory.save(NOW)
    salt = json.loads(path.read_text(encoding="utf-8"))["salt"]
    assert salt != "a" * 32
    assert re.fullmatch(r"[0-9a-f]{32}", salt)


def test_two_new_files_get_different_salts(tmp_path: Path) -> None:
    salts = []
    for folder in ("one", "two"):
        memory = DeskMemory(tmp_path / folder / MEMORY_FILE_NAME)
        memory.remember([ASSISTANT], NOW)
        memory.save(NOW)
        salts.append(json.loads(memory.path.read_text(encoding="utf-8"))["salt"])
    assert salts[0] != salts[1]


def test_the_salt_is_kept_across_saves(path: Path) -> None:
    memory = DeskMemory(path)
    memory.save(NOW)
    first = json.loads(path.read_text(encoding="utf-8"))["salt"]
    again = DeskMemory(path)
    again.remember([ASSISTANT], NOW)
    again.save(NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["salt"] == first


def test_no_name_in_plain_text_in_the_file_or_repr(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="ops_suite"):
        memory = DeskMemory(path)
        memory.remember([*NAMES, "Samplehelper, Quinn"], NOW)
        memory.save(NOW)
        text = (path.read_text(encoding="utf-8") + repr(memory) + caplog.text).casefold()

    assert repr(memory) == "DeskMemory(<3 members>)"
    words = {word for name in NAMES for word in re.split(r"[\s,.]+", name) if len(word) > 1}
    for needle in [*NAMES, *words]:
        assert needle.casefold() not in text
