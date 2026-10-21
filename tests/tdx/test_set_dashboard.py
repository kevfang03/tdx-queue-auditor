"""``ops-suite tdx set-dashboard`` end to end against the synthetic app on a local server.

A person's hand is simulated: when the command waits for Enter, the test switches the shell's
dashboard menu with ``page.evaluate``. The first window is headed, as in the real command.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.tdx.browser import TdxBrowser
from ops_suite.tdx.demo.app import WorkMgmtServer

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        sys.platform.startswith("linux")
        and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        reason="set-dashboard opens a headed window and no display is available",
    ),
]


def _run(
    server: WorkMgmtServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    switch_to: str,
    timeout: int = 10,
) -> tuple[int, str, str]:
    opened: list[TdxBrowser] = []

    class RecordingBrowser(TdxBrowser):
        def __enter__(self) -> TdxBrowser:
            opened.append(super().__enter__())
            return self

    def press_enter(*args: object) -> str:
        browser = opened[0]
        for _ in range(40):
            if len(browser.live_frames()) >= 2:
                break
            browser.wait(100)
        browser.live_frames()[0].evaluate(f"switchDashboard({switch_to!r})")
        return ""

    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", server.base_url)
    monkeypatch.setenv("OPS_SUITE_TDX__PAGE_TIMEOUT_SECONDS", str(timeout))
    monkeypatch.setattr("builtins.input", press_enter)
    monkeypatch.setattr(cli, "TdxBrowser", RecordingBrowser)
    code = main(["tdx", "set-dashboard"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_saves_the_it_tickets_frame_and_rows_start_there(
    workmgmt_server: WorkMgmtServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    code, out, err = _run(workmgmt_server, monkeypatch, capsys, "d=2")

    saved_file = tmp_path / "appdata" / "tdx" / "dashboard.json"
    assert code == 0, err
    assert out.splitlines()[-1] == (
        "Saved. Both widgets load from the saved dashboard address "
        '(/TDWorkManagement/WorkManagement/Dashboard/Dashboard). "uv run ops-suite tdx rows" will '
        "start there."
    )
    assert json.loads(saved_file.read_text(encoding="utf-8"))["url"] == workmgmt_server.frame("d=2")

    monkeypatch.setattr(cli, "TdxBrowser", TdxBrowser)
    assert main(["tdx", "rows"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:3] == [
        "Dashboard: saved (tdx set-dashboard)",
        "Unclaimed: 4 ticket(s) on 1 page(s).",
        "Claimed by others: 23 ticket(s) on 3 page(s).",
    ]


def test_a_frame_that_needs_its_parent_is_not_saved(
    workmgmt_server: WorkMgmtServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    code, out, err = _run(workmgmt_server, monkeypatch, capsys, "d=3", timeout=5)

    assert code == 5
    assert err.splitlines()[0] == cli.SET_DASHBOARD_NOT_STANDALONE
    assert "widget not found" in err
    assert not (tmp_path / "appdata" / "tdx" / "dashboard.json").exists()
    assert "Saved." not in out
