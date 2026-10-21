import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops_suite.tdx import saved as saved_module
from ops_suite.tdx.saved import (
    SavedDashboard,
    forget_dashboard,
    load_saved_dashboard,
    save_dashboard,
)

URL = "https://tdx.example.edu/TDWorkManagement/WorkManagement/Dashboard/Dashboard?d=40291"
NOW = datetime(2026, 10, 8, 17, 0, tzinfo=timezone(timedelta(hours=-4)))


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "tdx" / "dashboard.json"

    written = save_dashboard(path, URL, NOW)
    loaded = load_saved_dashboard(path)

    assert loaded == written == SavedDashboard(URL, datetime(2026, 10, 8, 21, 0, tzinfo=UTC))
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "url": URL,
        "saved_at": "2026-10-08T21:00:00+00:00",
    }
    assert sorted(p.name for p in path.parent.iterdir()) == ["dashboard.json"]


def test_saving_again_replaces_the_address(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.json"
    save_dashboard(path, URL, NOW)

    save_dashboard(path, URL.replace("40291", "40292"), NOW)

    loaded = load_saved_dashboard(path)
    assert loaded is not None and loaded.url.endswith("d=40292")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"url": "https://tdx.example.edu/x"}',
        '{"saved_at": "2026-10-08T21:00:00+00:00"}',
        '{"url": 7, "saved_at": "2026-10-08T21:00:00+00:00"}',
        '{"url": "file:///C:/x.html", "saved_at": "2026-10-08T21:00:00+00:00"}',
        '{"url": "https://tdx.example.edu/x", "saved_at": "yesterday"}',
        '{"url": "https://tdx.example.edu/x", "saved_at": "2026-10-08T21:00:00"}',
    ],
)
def test_missing_empty_and_malformed_files_give_none(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "dashboard.json"
    assert load_saved_dashboard(path) is None

    path.write_text(content, encoding="utf-8")

    assert load_saved_dashboard(path) is None
    assert "tdx.example.edu" not in caplog.text


def test_a_failed_write_leaves_the_old_file_and_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "dashboard.json"
    save_dashboard(path, URL, NOW)
    before = path.read_bytes()

    def broken_replace(source: str, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(saved_module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        save_dashboard(path, URL.replace("40291", "40292"), NOW)

    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dashboard.json"]


def test_a_failed_first_write_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "dashboard.json"

    def broken_fsync(fd: int) -> None:
        raise OSError("io error")

    monkeypatch.setattr(saved_module.os, "fsync", broken_fsync)
    with pytest.raises(OSError):
        save_dashboard(path, URL, NOW)

    assert list(tmp_path.iterdir()) == []


def test_forget_removes_the_file_and_tolerates_none(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.json"
    save_dashboard(path, URL, NOW)

    forget_dashboard(path)
    forget_dashboard(path)

    assert not path.exists()
    assert load_saved_dashboard(path) is None


def test_repr_has_no_query_string() -> None:
    text = repr(SavedDashboard(URL, NOW.astimezone(UTC)))

    assert "?" not in text and "40291" not in text and "d=" not in text
    assert "tdx.example.edu/TDWorkManagement/WorkManagement/Dashboard/Dashboard" in text
