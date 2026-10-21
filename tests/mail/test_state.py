import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops_suite.mail.state import SeenState


def test_new_state_is_empty_and_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "state" / "seen.json"

    state = SeenState(path)

    assert not state.has("<m1@mail.example.edu>")
    assert not path.parent.exists()


def test_add_save_reload(tmp_path: Path) -> None:
    path = tmp_path / "state" / "seen.json"
    now = datetime.now(UTC)
    state = SeenState(path)

    state.add("<m1@mail.example.edu>", now)
    state.add("AAMkAGI2synthetic0008", now)
    state.save()
    reloaded = SeenState(path)

    assert reloaded.has("<m1@mail.example.edu>")
    assert reloaded.has("AAMkAGI2synthetic0008")
    assert not reloaded.has("<other@mail.example.edu>")


def test_file_shape(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    now = datetime.now(UTC)
    state = SeenState(path)
    state.add("<m1@mail.example.edu>", now)

    state.save()

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": 1,
        "seen": {"<m1@mail.example.edu>": now.isoformat()},
    }
    assert [p.name for p in tmp_path.iterdir()] == ["seen.json"]


def test_old_entries_are_dropped_on_save(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    now = datetime.now(UTC)
    state = SeenState(path, keep_days=90)
    state.add("<old@mail.example.edu>", now - timedelta(days=91))
    state.add("<recent@mail.example.edu>", now - timedelta(days=89))

    state.save()

    assert not state.has("<old@mail.example.edu>")
    reloaded = SeenState(path)
    assert not reloaded.has("<old@mail.example.edu>")
    assert reloaded.has("<recent@mail.example.edu>")


def test_adding_again_keeps_the_first_time(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    first = datetime.now(UTC) - timedelta(days=2)
    state = SeenState(path)

    state.add("<m1@mail.example.edu>", first)
    state.add("<m1@mail.example.edu>", datetime.now(UTC))
    state.save()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["seen"]["<m1@mail.example.edu>"] == first.isoformat()
