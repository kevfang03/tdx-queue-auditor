import csv
import io
import json
import plistlib
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo

import pytest

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common.autostart import MacLaunchAgent, WindowsStartup
from ops_suite.common.llm import (
    LlmDayUsage,
    LlmFailed,
    LlmResult,
    LlmUnavailable,
    LlmUsage,
    set_api_key,
)
from ops_suite.tdx import watch as watch_module
from ops_suite.tdx.browser import (
    PROFILE_IN_USE_MESSAGE,
    BrowserStartError,
    FrameHtml,
    RequestRecord,
)
from ops_suite.tdx.dashboard import (
    DashboardRead,
    DashboardStructureError,
    NotSignedInError,
    WidgetNotFoundError,
    WidgetRead,
)
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.followup import FollowUpAnswer
from ops_suite.tdx.guard import BUILT_IN_ALLOWED, AllowedRequest
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.saved import load_saved_dashboard, save_dashboard
from ops_suite.tdx.session_log import SessionLog
from ops_suite.tdx.tickets import DESCRIPTION_SHAPE_KEYS, FeedRead

FIXTURE_DROP = Path(__file__).parent / "fixtures" / "mail_drop"

SUMMARY = "Scanned 8, flagged 3, skipped 5, duplicates 0, rejected 1"
SKIPPED = (
    "  skipped: auto_submitted 1, automated_local_part 1, automated_sender 1, "
    "list_mail 1, own_mailbox 1"
)


def _scan_args(test_config: Path, drop_dir: Path, *extra: str) -> list[str]:
    return [
        "mail",
        "scan",
        "--profile",
        "work",
        "--config",
        str(test_config),
        "--drop-dir",
        str(drop_dir),
        *extra,
    ]


def _private_strings() -> set[str]:
    """Sender, recipients, subject, header values and body phrases of every fixture."""
    private: set[str] = set()
    for path in FIXTURE_DROP.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        private.update(re.findall(r"[\w.+-]+@[\w.-]+\.(?:edu|com)", text, flags=re.IGNORECASE))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # The truncated fixture: take its values straight from the raw lines.
            data = dict(re.findall(r'"(from|subject|body)": "([^"\n]*)', text))
        private.add(data.get("subject", ""))
        private.update(data.get("headers", {}).values())
        for phrase in re.split(r"<[^>]+>|[.?!\n]", data.get("body", "")):
            if len(phrase.strip()) >= 15:
                private.add(phrase.strip())
    # Message ids may be logged, and they look like addresses.
    message_ids = set(re.findall(r"f0\d\.\d+@[\w.]+", " ".join(private)))
    return {value for value in private if value} - message_ids


def test_no_drop_folder_configured_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["mail", "scan", "--profile", "work"])

    captured = capsys.readouterr()
    assert code == 2
    assert "no drop folder set" in captured.err
    assert len(captured.err.strip().splitlines()) == 1
    assert captured.out == ""


def test_missing_drop_folder_exits_2(
    tmp_path: Path, test_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_scan_args(test_config, tmp_path / "nowhere"))

    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_empty_mailbox_address_exits_2(drop_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["mail", "scan", "--profile", "work", "--drop-dir", str(drop_dir)])

    assert code == 2
    assert "mailbox_address is empty" in capsys.readouterr().err
    assert (drop_dir / "01_student_wifi.json").is_file()


def test_invalid_settings_exit_2(
    tmp_path: Path, drop_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[mail.work]\nretention_days = 0\n", encoding="utf-8")

    code = main(_scan_args(config, drop_dir))

    assert code == 2
    assert "mail.work.retention_days" in capsys.readouterr().err


def test_unknown_profile_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["mail", "scan", "--profile", "personal"])

    assert exit_info.value.code == 2
    assert "--profile" in capsys.readouterr().err


def test_dry_run_prints_summary_and_writes_nothing(
    tmp_path: Path, drop_dir: Path, test_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(p.name for p in drop_dir.iterdir())

    code = main(_scan_args(test_config, drop_dir, "--dry-run"))

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0] == SUMMARY
    assert out[1] == SKIPPED
    assert out[2] == "Report: (dry run, nothing written)"
    assert out[3:] == [
        "  would report: 2026-10-01T14:03:22Z  <f01.20261001140322@mail.example.edu>",
        "  would report: 2026-09-30T13:15:00Z  <f02.20260930131500@mail.example.edu>",
        "  would report: 2026-10-01T16:45:10Z  AAMkAGI2synthetic0008",
    ]
    assert sorted(p.name for p in drop_dir.iterdir()) == before
    assert not (tmp_path / "appdata").exists()


def test_real_run_writes_report_under_the_work_profile(
    tmp_path: Path, drop_dir: Path, test_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "appdata" / "work" / "reports" / "direct-requests.csv"

    code = main(_scan_args(test_config, drop_dir))

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out == [SUMMARY, SKIPPED, f"Report: {report}"]
    with report.open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 3
    assert (tmp_path / "appdata" / "work" / "state" / "seen.json").is_file()


def test_drop_dir_can_come_from_the_environment(
    drop_dir: Path,
    test_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPS_SUITE_MAIL__WORK__DROP_DIR", str(drop_dir))

    code = main(["mail", "scan", "--profile", "work", "--config", str(test_config), "--dry-run"])

    assert code == 0
    assert capsys.readouterr().out.splitlines()[0] == SUMMARY


def test_log_file_holds_no_sender_subject_or_body_text(
    tmp_path: Path, drop_dir: Path, test_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private = _private_strings()
    assert "jordan.rivera@example.edu" in private
    assert "Priya.Natarajan@Example.EDU" in private
    assert "shared-mailbox@example.edu" in private
    assert "Laptop will not join the library Wi-Fi" in private
    assert "auto-replied" in private
    assert "Projector in room 204 shows no picture" in private
    assert len(private) > 40

    assert main(_scan_args(test_config, drop_dir, "--verbose")) == 0
    # A second pass exercises the duplicate path and the name-collision moves.
    for path in FIXTURE_DROP.glob("*.json"):
        (drop_dir / path.name).write_bytes(path.read_bytes())
    assert main(_scan_args(test_config, drop_dir, "--verbose")) == 0

    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    console_text = capsys.readouterr().err
    records = [json.loads(line) for line in log_text.splitlines()]
    assert any(record["message"].startswith("scan finished") for record in records)
    assert any("09_malformed.json" in record["message"] for record in records)
    for logged in (log_text, console_text):
        leaked = sorted(value for value in private if value.lower() in logged.lower())
        assert leaked == []


# --- tdx login / tdx capture -------------------------------------------------------------

TDX_BASE = "https://tdx.example.edu/TDNext/"
TDX_SYNTHETIC = FIXTURE_DROP.parent / "tdx_synthetic"


class FakeTdxBrowser:
    """Stands in for TdxBrowser: no Chromium, fixed pages, frames and requests."""

    def __init__(
        self,
        urls: list[str],
        frames: list[FrameHtml] | None = None,
        requests: list[RequestRecord] | None = None,
        start_error: str | None = None,
    ) -> None:
        self.urls = urls
        self.frames = frames or []
        self.requests = requests or []
        self.start_error = start_error
        self.skipped_frames = 0
        self.profile_dir: Path | None = None
        self.options: dict[str, object] = {}
        self.calls: list[dict[str, object]] = []  # options of every browser started
        self.opened = False
        self.closed = False

    def __call__(self, profile_dir: Path, base_url: str, **options: object) -> "FakeTdxBrowser":
        self.profile_dir = profile_dir
        self.options = options
        self.calls.append(options)
        return self

    def __enter__(self) -> "FakeTdxBrowser":
        if self.start_error:
            raise BrowserStartError(self.start_error)
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def open_start_page(self) -> None:
        self.opened = True

    def current_urls(self) -> list[str]:
        return list(self.urls)

    def collect_frames(self) -> list[FrameHtml]:
        self.skipped_frames = 1
        return list(self.frames)

    def requests_seen(self) -> list[RequestRecord]:
        return list(self.requests)


@pytest.fixture
def tdx_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", TDX_BASE)
    monkeypatch.setattr("builtins.input", lambda *args: "")
    # tdx login asks launchd whether the agent is loaded; never the real one in tests.
    monkeypatch.setattr(cli, "for_platform", lambda **kwargs: None)
    return monkeypatch


@pytest.mark.parametrize("command", [["login"], ["capture", "--name", "dashboard"], ["rows"]])
def test_tdx_commands_exit_2_without_a_base_url(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["tdx", *command])

    captured = capsys.readouterr()
    assert code == 2
    assert "tdx.base_url is empty" in captured.err
    assert len(captured.err.strip().splitlines()) == 1
    assert captured.out == ""


@pytest.mark.parametrize("command", [["login"], ["capture", "--name", "dashboard"], ["rows"]])
@pytest.mark.parametrize("base_url", ["file:///C:/tdx/index.html", "tdx.example.edu/TDNext"])
def test_tdx_commands_exit_2_for_a_non_web_base_url(
    command: list[str],
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", base_url)

    assert main(["tdx", *command]) == 2
    assert "http:// or https://" in capsys.readouterr().err


def test_tdx_login_signed_in(
    tmp_path: Path, tdx_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    browser = FakeTdxBrowser(["https://tdx.example.edu/TDNext/Home/Desktop/Default.aspx?x=1"])
    tdx_env.setattr(cli, "TdxBrowser", browser)

    code = main(["tdx", "login"])

    profile = tmp_path / "appdata" / "tdx" / "browser-profile"
    assert code == 0
    assert browser.opened and browser.closed
    assert browser.profile_dir == profile
    assert capsys.readouterr().out.splitlines() == [
        cli.LOGIN_PROMPT,
        f"Signed in. The session is saved in {profile}.",
    ]


def test_tdx_login_uses_the_configured_profile_dir(
    tmp_path: Path, tdx_env: pytest.MonkeyPatch
) -> None:
    tdx_env.setenv("OPS_SUITE_TDX__PROFILE_DIR", str(tmp_path / "elsewhere"))
    browser = FakeTdxBrowser([TDX_BASE])
    tdx_env.setattr(cli, "TdxBrowser", browser)

    assert main(["tdx", "login"]) == 0
    assert browser.profile_dir == tmp_path / "elsewhere"


@pytest.mark.parametrize("command", [["login"], ["capture", "--name", "dashboard"]])
def test_tdx_not_signed_in_exits_3(
    command: list[str],
    tmp_path: Path,
    tdx_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakeTdxBrowser(["https://login.example.edu/idp/SSO?SAMLRequest=abc", "about:blank"])
    tdx_env.setattr(cli, "TdxBrowser", browser)

    code = main(["tdx", *command])

    assert code == 3
    assert browser.closed
    assert "sign-in is not finished" in capsys.readouterr().err
    assert not (tmp_path / "appdata" / "tdx" / "captures").exists()


@pytest.mark.parametrize("command", [["login"], ["capture", "--name", "dashboard"]])
def test_tdx_browser_start_failure_exits_4(
    command: list[str], tdx_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tdx_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([], start_error="Executable doesn't exist"))

    code = main(["tdx", *command])

    err = capsys.readouterr().err
    assert code == 4
    assert "uv run playwright install chromium" in err
    assert len(err.strip().splitlines()) == 1


def test_tdx_capture_writes_the_folder_and_logs_no_private_text(
    tmp_path: Path, tdx_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket_url = "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID=3119232"
    frames = [
        FrameHtml(0, 0, ticket_url, (TDX_SYNTHETIC / "ticket.html").read_text(encoding="utf-8")),
        FrameHtml(1, 0, TDX_BASE, (TDX_SYNTHETIC / "widget.html").read_text(encoding="utf-8")),
    ]
    requests = [RequestRecord("GET", "document", ticket_url)] * 3
    browser = FakeTdxBrowser([ticket_url, TDX_BASE], frames, requests)
    tdx_env.setattr(cli, "TdxBrowser", browser)

    code = main(["tdx", "capture", "--name", "ticket", "--verbose"])

    captured = capsys.readouterr()
    out = captured.out.splitlines()
    assert code == 0
    assert browser.closed
    assert out[0] == cli.CAPTURE_PROMPT
    assert out[1] == "Captured 2 frame(s) from 2 page(s), 1 skipped, 3 request(s)."
    folder = Path(out[2].removeprefix("Saved to "))
    assert folder.parent == tmp_path / "appdata" / "tdx" / "captures"
    assert re.fullmatch(r"ticket-\d{8}-\d{6}", folder.name)
    assert (folder / "frame-1-0.html").is_file()
    assert out[3:] == cli.REVIEW_REMINDER.splitlines()

    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines()]
    assert any(record.get("exit_code") == 0 and record.get("frames") == 2 for record in records)
    assert any(
        "tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet" in line for line in log_text.splitlines()
    )
    for logged in (log_text, captured.err):
        for private in ["3119232", "TicketID", "Jordan Sample", "@example.edu", "<html"]:
            assert private not in logged


# --- tdx rows ----------------------------------------------------------------------------

TZ = ZoneInfo("America/New_York")
ROWS_BASE = "https://tdx.example.edu/TDWorkManagement/"
WIDGET_POST = "POST /TDWorkManagement/WorkManagement/Widgets/#"
# The log may name the host (describe_url); ticket links show up as "TicketDet".
INVENTED = ["Alex Example", "Pat Helpdesk", "Laptop will not connect", "TicketDet"]


def _row(kind: str, ticket_id: int) -> TicketRow:
    return TicketRow(
        widget=kind,
        ticket_id=ticket_id,
        title="Laptop will not connect to campus Wi-Fi",
        requestor="Alex Example",
        resp_group="IT Service Desk",
        created=datetime(2026, 10, 2, 15, 30, tzinfo=TZ),
        modified=datetime(2026, 10, 5, 9, 0, tzinfo=TZ) if kind == "claimed" else None,
        prim_resp="Pat Helpdesk" if kind == "claimed" else None,
        status="In Process" if kind == "claimed" else None,
        detail_url=f"https://tdx.example.edu/TDWorkManagement/TicketDet?TicketID={ticket_id}",
    )


def _dashboard_read(**overrides: object) -> DashboardRead:
    values: dict[str, object] = {
        "widgets": {
            "unclaimed": WidgetRead(
                "unclaimed", tuple(_row("unclaimed", 4410000 + i) for i in range(7)), 1, (), False
            ),
            "claimed": WidgetRead(
                "claimed", tuple(_row("claimed", 4409000 + i) for i in range(23)), 3, (), False
            ),
        },
        "blocked": (),
        "allowed_non_get": (WIDGET_POST,) * 4,
    }
    values.update(overrides)
    return DashboardRead(**values)


class FakeRead:
    """Stands in for read_dashboard: returns a fixed result or raises."""

    def __init__(self, result: DashboardRead | Exception) -> None:
        self.result = result
        self.settings = None

    def __call__(
        self, browser: object, settings: object, now: datetime, *, start_url: str
    ) -> DashboardRead:
        self.start_url = start_url
        self.settings = settings
        assert now.tzinfo is not None
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def rows_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", ROWS_BASE)
    return monkeypatch


def test_tdx_rows_help_shows_the_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["tdx", "rows", "--help"])

    out = capsys.readouterr().out
    assert exit_info.value.code == 0
    for option in ["--config PATH", "--headed", "--verbose"]:
        assert option in out


def test_tdx_rows_exits_2_for_a_dashboard_url_on_another_host(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TDX__DASHBOARD_URL", "https://elsewhere.example.edu/Dashboard")
    browser = FakeTdxBrowser([])
    rows_env.setattr(cli, "TdxBrowser", browser)

    code = main(["tdx", "rows"])

    captured = capsys.readouterr()
    assert code == 2
    assert "tdx.dashboard_url must be on the same host as tdx.base_url" in captured.err
    assert browser.profile_dir is None  # no browser was started


def test_tdx_rows_prints_counts_only(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TDX__ALLOWED_REQUESTS", '["POST /TDWorkManagement/x/#"]')
    browser = FakeTdxBrowser([])
    reader = FakeRead(_dashboard_read())
    rows_env.setattr(cli, "TdxBrowser", browser)
    rows_env.setattr(cli, "read_dashboard", reader)

    code = main(["tdx", "rows"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [
        "Dashboard: start page",
        "Unclaimed: 7 ticket(s) on 1 page(s).",
        "Claimed by others: 23 ticket(s) on 3 page(s).",
        "Unreadable dates: 0. Duplicates: 0. Blocked requests: 0. Allowed non-GET: 4.",
    ]
    assert browser.closed
    assert browser.profile_dir == tmp_path / "appdata" / "tdx" / "browser-profile"
    assert browser.options["headless"] is True
    assert browser.options["allowed_requests"] == BUILT_IN_ALLOWED + (
        AllowedRequest("POST", "/TDWorkManagement/x/#", "from tdx.allowed_requests"),
    )
    assert reader.settings.base_url == ROWS_BASE
    assert reader.start_url == ROWS_BASE

    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines()]
    finished = [r for r in records if r["message"] == "command finished"]
    assert finished[-1]["command"] == "tdx rows"
    assert finished[-1]["exit_code"] == 0
    assert (finished[-1]["unclaimed"], finished[-1]["claimed"]) == (7, 23)
    assert finished[-1]["allowed_non_get"] == 4
    for logged in (log_text, captured.out, captured.err):
        for invented in INVENTED + ["4410000", "4409000"]:
            assert invented not in logged


@pytest.mark.parametrize(("flags", "headless"), [([], True), (["--headed"], False)])
def test_tdx_rows_headed_overrides_the_setting(
    flags: list[str], headless: bool, rows_env: pytest.MonkeyPatch
) -> None:
    browser = FakeTdxBrowser([])
    rows_env.setattr(cli, "TdxBrowser", browser)
    rows_env.setattr(cli, "read_dashboard", FakeRead(_dashboard_read()))

    assert main(["tdx", "rows", *flags]) == 0
    assert browser.options["headless"] is headless


def test_rows_summary_reports_problems_without_ticket_text() -> None:
    read = _dashboard_read(
        widgets={
            "unclaimed": WidgetRead(
                "unclaimed",
                (_row("unclaimed", 4410001),),
                1,
                ("aaa 9/9/99 9:99 aa", "aaa 9/9/99 9:99 aa"),
                False,
            ),
            "claimed": WidgetRead(
                "claimed",
                tuple(_row("claimed", 4409000 + i) for i in range(40)),
                20,
                ("aaaaaaaaa",),
                True,
                duplicates=2,
                rows_without_id=1,
            ),
        },
        blocked=("POST /TDWorkManagement/api/#/feed",),
        allowed_non_get=(WIDGET_POST,) * 21,
    )

    summary = cli.format_rows_summary(read, 20)
    verbose = cli.format_rows_summary(read, 20, verbose=True)

    assert summary.splitlines() == [
        "Unclaimed: 1 ticket(s) on 1 page(s).",
        "Claimed by others: 40 ticket(s) on 20 page(s).",
        "Unreadable dates: 3. Duplicates: 2. Blocked requests: 1. Allowed non-GET: 21.",
        "Rows without a ticket id (skipped): 1.",
        "Unreadable date shapes: aaa 9/9/99 9:99 aa (2); aaaaaaaaa (1)",
        "Blocked: POST /TDWorkManagement/api/#/feed (1)",
        "Stopped at the page limit (20) for: claimed",
    ]
    assert f"Allowed non-GET: {WIDGET_POST} (21)" in verbose.splitlines()
    assert f"Allowed non-GET: {WIDGET_POST} (21)" not in summary.splitlines()
    for text in (summary, verbose):
        for invented in INVENTED + ["4410001", "4409000"]:
            assert invented not in text


def test_tdx_rows_not_signed_in_exits_3(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    browser = FakeTdxBrowser([])
    rows_env.setattr(cli, "TdxBrowser", browser)
    rows_env.setattr(cli, "read_dashboard", FakeRead(NotSignedInError()))

    code = main(["tdx", "rows"])

    captured = capsys.readouterr()
    assert code == 3
    assert browser.closed
    assert captured.err.strip() == f"ops-suite: {cli.SESSION_EXPIRED}"
    assert captured.out == "Dashboard: start page\n"


def test_tdx_rows_structure_problem_exits_5_and_lists_blocked_requests(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = DashboardStructureError(
        "paging did not advance for the claimed widget after page 1",
        ("POST /TDWorkManagement/WorkManagement/Grid/#/Page",) * 2,
    )
    rows_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([]))
    rows_env.setattr(cli, "read_dashboard", FakeRead(error))

    code = main(["tdx", "rows"])

    captured = capsys.readouterr()
    assert code == 5
    assert captured.err.splitlines() == [
        "ops-suite: the dashboard did not have the expected structure: "
        "paging did not advance for the claimed widget after page 1.",
        "Blocked: POST /TDWorkManagement/WorkManagement/Grid/#/Page (2)",
        cli.REPORT_BLOCKED,
    ]
    assert captured.out == "Dashboard: start page\n"


def test_tdx_rows_structure_problem_without_blocked_requests(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([]))
    rows_env.setattr(cli, "read_dashboard", FakeRead(DashboardStructureError("widget not found")))

    assert main(["tdx", "rows"]) == 5
    assert capsys.readouterr().err.splitlines() == [
        "ops-suite: the dashboard did not have the expected structure: widget not found."
    ]


@pytest.mark.parametrize(
    ("start_error", "expected"),
    [
        ("Executable doesn't exist", "uv run playwright install chromium"),
        (PROFILE_IN_USE_MESSAGE, "already in use"),
    ],
)
def test_tdx_rows_browser_failure_exits_4(
    start_error: str,
    expected: str,
    rows_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([], start_error=start_error))

    code = main(["tdx", "rows"])

    err = capsys.readouterr().err
    assert code == 4
    assert expected in err
    assert len(err.strip().splitlines()) == 1


# --- where tdx rows starts ---------------------------------------------------------------

SAVED_URL = "https://tdx.example.edu/TDWorkManagement/WorkManagement/Dashboard/Dashboard?d=40291"
SAVED_PATH = "/TDWorkManagement/WorkManagement/Dashboard/Dashboard"


def _saved_file(tmp_path: Path) -> Path:
    return tmp_path / "appdata" / "tdx" / "dashboard.json"


def _assert_no_saved_query(tmp_path: Path, *outputs: str) -> None:
    log_file = tmp_path / "applogs" / "ops-suite.log"
    log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    for text in (log_text, *outputs):
        assert "40291" not in text and "d=" not in text and "?" not in text.replace("? ", "")


@pytest.mark.parametrize(
    ("setting", "saved", "source", "start"),
    [
        ("https://tdx.example.edu/TDWorkManagement/Explicit", True, "setting", "Explicit"),
        ("", True, "saved (tdx set-dashboard)", SAVED_URL),
        ("", False, "start page", ROWS_BASE),
    ],
)
def test_tdx_rows_start_address_precedence(
    setting: str,
    saved: bool,
    source: str,
    start: str,
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if setting:
        rows_env.setenv("OPS_SUITE_TDX__DASHBOARD_URL", setting)
    if saved:
        save_dashboard(_saved_file(tmp_path), SAVED_URL, datetime.now(TZ))
    reader = FakeRead(_dashboard_read())
    rows_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([]))
    rows_env.setattr(cli, "read_dashboard", reader)

    assert main(["tdx", "rows"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"Dashboard: {source}"
    assert reader.start_url.endswith(start)
    _assert_no_saved_query(tmp_path, out)


def test_tdx_rows_refuses_a_saved_address_on_another_host(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    save_dashboard(_saved_file(tmp_path), "https://other.example.edu/Dashboard", datetime.now(TZ))
    browser = FakeTdxBrowser([])
    rows_env.setattr(cli, "TdxBrowser", browser)

    assert main(["tdx", "rows"]) == 2
    assert "tdx set-dashboard" in capsys.readouterr().err
    assert browser.calls == []


@pytest.mark.parametrize(("saved", "hint"), [(False, True), (True, False)])
def test_tdx_rows_suggests_set_dashboard_for_a_missing_widget_on_the_start_page(
    saved: bool,
    hint: bool,
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if saved:
        save_dashboard(_saved_file(tmp_path), SAVED_URL, datetime.now(TZ))
    error = WidgetNotFoundError('widget not found: "Claimed" (tdx.widgets.claimed_title)')
    rows_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([]))
    rows_env.setattr(cli, "read_dashboard", FakeRead(error))

    assert main(["tdx", "rows"]) == 5

    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("ops-suite: the dashboard did not have the expected structure")
    assert (cli.SET_DASHBOARD_HINT in err) is hint


# --- tdx set-dashboard -------------------------------------------------------------------


class FakeFind:
    def __init__(self, result: str | Exception) -> None:
        self.result = result

    def __call__(self, browser: object, settings: object) -> str:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCheck:
    def __init__(self, error: Exception | None = None, saved_file: Path | None = None) -> None:
        self.error = error
        self.saved_file = saved_file
        self.url: str | None = None
        self.file_existed_during_check = False

    def __call__(self, browser: object, settings: object, url: str) -> None:
        self.url = url
        if self.saved_file is not None:
            self.file_existed_during_check = self.saved_file.is_file()
        if self.error is not None:
            raise self.error


def _set_dashboard_env(
    monkeypatch: pytest.MonkeyPatch,
    find: FakeFind,
    check: FakeCheck,
    urls: list[str] | None = None,
) -> FakeTdxBrowser:
    browser = FakeTdxBrowser([ROWS_BASE] if urls is None else urls)
    monkeypatch.setattr("builtins.input", lambda *args: "")
    monkeypatch.setattr(cli, "TdxBrowser", browser)
    monkeypatch.setattr(cli, "find_dashboard_frame", find)
    monkeypatch.setattr(cli, "check_dashboard", check)
    return browser


def test_tdx_set_dashboard_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["tdx", "set-dashboard", "--help"])

    assert exit_info.value.code == 0
    assert "--config PATH" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "tdx.base_url is empty"),
        ({"OPS_SUITE_TDX__BASE_URL": "tdx.example.edu"}, "http:// or https://"),
        (
            {
                "OPS_SUITE_TDX__BASE_URL": ROWS_BASE,
                "OPS_SUITE_TDX__DASHBOARD_URL": "https://other.example.edu/x",
            },
            "tdx.dashboard_url must be on the same host",
        ),
        (
            {"OPS_SUITE_TDX__BASE_URL": ROWS_BASE, "OPS_SUITE_TDX__ALLOWED_REQUESTS": '["GET /x"]'},
            "tdx.allowed_requests",
        ),
    ],
)
def test_tdx_set_dashboard_exits_2_on_bad_config(
    env: dict[str, str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    browser = FakeTdxBrowser([])
    monkeypatch.setattr(cli, "TdxBrowser", browser)

    assert main(["tdx", "set-dashboard"]) == 2
    assert message in capsys.readouterr().err
    assert browser.calls == []


def test_tdx_set_dashboard_saves_a_checked_address(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check = FakeCheck(saved_file=_saved_file(tmp_path))
    browser = _set_dashboard_env(rows_env, FakeFind(SAVED_URL), check)

    code = main(["tdx", "set-dashboard", "--verbose"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [
        "Sign in if asked, then open the dashboard that shows both "
        '"Unclaimed Tickets in My Group(s)" and '
        '"Active Tickets Claimed by Others in My Group(s)". '
        "Press Enter here when both have loaded.",
        f"Saved. Both widgets load from the saved dashboard address ({SAVED_PATH}). "
        '"uv run ops-suite tdx rows" will start there.',
    ]
    # First the person's headed, unguarded window; then a guarded check, headless by default.
    assert browser.calls == [
        {},
        {"headless": True, "allowed_requests": BUILT_IN_ALLOWED},
    ]
    assert check.url == SAVED_URL and check.file_existed_during_check
    saved = load_saved_dashboard(_saved_file(tmp_path))
    assert saved is not None and saved.url == SAVED_URL
    _assert_no_saved_query(tmp_path, captured.out, captured.err)


def test_tdx_set_dashboard_notes_an_overriding_setting(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TDX__DASHBOARD_URL", ROWS_BASE + "Explicit")
    _set_dashboard_env(rows_env, FakeFind(SAVED_URL), FakeCheck())

    assert main(["tdx", "set-dashboard"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == cli.SET_DASHBOARD_OVERRIDDEN
    assert _saved_file(tmp_path).is_file()


@pytest.mark.parametrize(
    "error",
    [
        WidgetNotFoundError(
            'widget not found: "Active Tickets Claimed by Others in My Group(s)" '
            "(tdx.widgets.claimed_title)",
            ("POST /TDWorkManagement/api/#/feed",),
        ),
        NotSignedInError(),
    ],
)
def test_tdx_set_dashboard_forgets_an_address_that_fails_the_check(
    error: Exception,
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    check = FakeCheck(error, saved_file=_saved_file(tmp_path))
    _set_dashboard_env(rows_env, FakeFind(SAVED_URL), check)

    code = main(["tdx", "set-dashboard"])

    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert code == 5
    assert check.file_existed_during_check
    assert not _saved_file(tmp_path).exists()
    assert err[0] == cli.SET_DASHBOARD_NOT_STANDALONE
    assert err[1].startswith("Reason: ")
    if isinstance(error, WidgetNotFoundError):
        assert "claimed_title" in err[1]
        assert err[2] == "Blocked: POST /TDWorkManagement/api/#/feed (1)"
    else:
        assert "sign-in page" in err[1]
    assert "Saved." not in captured.out
    _assert_no_saved_query(tmp_path, captured.out, captured.err)


def test_tdx_set_dashboard_forgets_the_address_if_the_check_browser_fails(
    tmp_path: Path, rows_env: pytest.MonkeyPatch
) -> None:
    class StartsOnce(FakeTdxBrowser):
        def __enter__(self) -> "FakeTdxBrowser":
            if len(self.calls) > 1:
                raise BrowserStartError(PROFILE_IN_USE_MESSAGE)
            return self

    browser = StartsOnce([ROWS_BASE])
    rows_env.setattr("builtins.input", lambda *args: "")
    rows_env.setattr(cli, "TdxBrowser", browser)
    rows_env.setattr(cli, "find_dashboard_frame", FakeFind(SAVED_URL))

    assert main(["tdx", "set-dashboard"]) == 4
    assert not _saved_file(tmp_path).exists()


def test_tdx_set_dashboard_when_no_frame_shows_both_widgets(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = WidgetNotFoundError('widget not found: "Claimed" (tdx.widgets.claimed_title)')
    check = FakeCheck()
    browser = _set_dashboard_env(rows_env, FakeFind(error), check)

    code = main(["tdx", "set-dashboard"])

    assert code == 5
    assert capsys.readouterr().err.splitlines() == [
        'ops-suite: widget not found: "Claimed" (tdx.widgets.claimed_title).',
        cli.SET_DASHBOARD_RETRY,
    ]
    assert len(browser.calls) == 1 and check.url is None
    assert not _saved_file(tmp_path).exists()


def test_tdx_set_dashboard_refuses_a_frame_on_another_host(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check = FakeCheck()
    _set_dashboard_env(rows_env, FakeFind("https://other.example.edu/Dashboard?d=1"), check)

    assert main(["tdx", "set-dashboard"]) == 5
    assert "was not saved" in capsys.readouterr().err
    assert check.url is None
    assert not _saved_file(tmp_path).exists()


def test_tdx_set_dashboard_not_signed_in_exits_3(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    find = FakeFind(SAVED_URL)
    _set_dashboard_env(rows_env, find, FakeCheck(), urls=["https://login.example.edu/idp/SSO"])

    assert main(["tdx", "set-dashboard"]) == 3
    assert "sign-in is not finished" in capsys.readouterr().err
    assert not _saved_file(tmp_path).exists()


# --- tdx audit -----------------------------------------------------------------------------

FEED_POST = "POST /TDNext/Apps/Feed/Search"
SOCKET = "WS /TDSignalR/signalr/connect"
HUBS = "GET /TDSignalR/signalr/hubs"
MARKERS = {
    "entries": 5,
    "nested": 1,
    "private_present": 3,
    "private_visible": 1,
    "inherited_private": 0,
    "visibility_fallback": 0,
    "merge_notes": 0,
    "reassignments": 0,
    "signature_desk_replies": 0,
}
AUDIT_INVENTED = INVENTED + [
    "Riley Testperson",
    "Robotics club",
    "example.edu",
    "TicketID",
    "Zanzibar",  # in AUDIT_REPLY
]
# 4410001's newest reply (invented): never in output, logs or files.
AUDIT_REPLY = (
    "Hi Pat,\nI am at the Zanzibar workshop until Monday; I will try the new profile then and "
    "let you know.\nThanks,\nAlex Example\nalex.example@example.edu"
)


def _audit_row(kind: str, ticket_id: int, now: datetime, hours_ago: float, **fields: object):
    when = now - timedelta(hours=hours_ago)
    values: dict[str, object] = {
        "widget": kind,
        "ticket_id": ticket_id,
        "title": "Laptop will not connect to campus Wi-Fi",
        "requestor": "Alex Example",
        "resp_group": "IT Service Desk",
        "created": when if kind == "unclaimed" else None,
        "modified": when if kind == "claimed" else None,
        "prim_resp": "Pat Helpdesk" if kind == "claimed" else None,
        "status": "In Process" if kind == "claimed" else None,
        "detail_url": f"https://tdx.example.edu/TDWorkManagement/TicketDet?TicketID={ticket_id}",
    }
    values.update(fields)
    return TicketRow(**values)


def _audit_scenario(now: datetime) -> tuple[DashboardRead, FeedRead]:
    """5 unclaimed and 2 claimed tickets; 3 unclaimed ones need their feed, 1 fails."""
    unclaimed = (
        _audit_row("unclaimed", 4410001, now, 80),  # the requestor wrote last, 73h ago
        _audit_row("unclaimed", 4410002, now, 60),  # the desk replied last
        _audit_row("unclaimed", 4410003, now, 100),  # its feed could not be read
        _audit_row("unclaimed", 4410004, now, 2),  # recent: no feed
        _audit_row("unclaimed", 4410005, now, 1, title="EVENT [TBD] Robotics club meeting"),
    )
    claimed = (
        _audit_row("claimed", 4409001, now, 1),  # counted only
        _audit_row("claimed", 4409002, now, 300),  # counted only, however old
    )
    dashboard = _dashboard_read(
        widgets={
            "unclaimed": WidgetRead("unclaimed", unclaimed, 1, (), False),
            "claimed": WidgetRead("claimed", claimed, 1, (), False),
        }
    )

    def at(hours_ago: float) -> datetime:
        return now - timedelta(hours=hours_ago)

    feeds = FeedRead(
        feeds={
            4410001: TicketFeed(
                4410001,
                (
                    FeedEntry("Riley Testperson", at(70), "private"),
                    FeedEntry("Alex Example", at(73.5), "reply"),
                ),
                latest_reply_text=AUDIT_REPLY,
            ),
            4410002: TicketFeed(
                4410002,
                (
                    FeedEntry("Pat Helpdesk", at(50), "system"),
                    FeedEntry("Pat Helpdesk", at(55), "reply"),
                ),
                unreadable=1,
            ),
        },
        failed=(4410003,),
        deferred=(),
        blocked=(),
        allowed_non_get=(FEED_POST,) * 2,
        unreadable_shapes=("no author",),
        live_updates_blocked=(SOCKET, HUBS) * 3,
        marker_stats=MARKERS,
    )
    return dashboard, feeds


class FakeFeeds:
    """Stands in for read_feeds: records the ticket ids asked for, returns a fixed result."""

    def __init__(self, result: FeedRead | Exception) -> None:
        self.result = result
        self.ticket_ids: list[int] | None = None

    def __call__(
        self,
        browser,
        settings,
        rows_by_id,
        ticket_ids,
        now,
        progress=None,
        *,
        form_ticket_ids=(),
        form_options=None,
    ):
        assert now.tzinfo is not None
        self.ticket_ids = list(ticket_ids)
        self.rows_by_id = rows_by_id
        self.form_ticket_ids = set(form_ticket_ids)
        self.form_options = form_options
        if isinstance(self.result, Exception):
            raise self.result
        for done in range(1, len(self.ticket_ids) + 1):
            if progress is not None:
                progress(done, len(self.ticket_ids))
        return self.result


def _audit_env(rows_env: pytest.MonkeyPatch, feeds: FeedRead | Exception | None = None):
    now = datetime.now(TZ)
    dashboard, scenario_feeds = _audit_scenario(now)
    fake_feeds = FakeFeeds(scenario_feeds if feeds is None else feeds)
    browser = FakeTdxBrowser([])
    rows_env.setattr(watch_module, "TdxBrowser", browser)
    rows_env.setattr(watch_module, "read_dashboard", FakeRead(dashboard))
    rows_env.setattr(watch_module, "read_feeds", fake_feeds)
    return browser, fake_feeds


AUDIT_SUMMARY = [
    "Dashboard: start page",
    "Unclaimed: 5 ticket(s) on 1 page(s).",
    "Claimed by others: 2 ticket(s) on 1 page(s).",
    "Feeds: 3 needed, 2 read, 1 failed, 0 deferred. Entries: 4 (2 reply, 1 private, 1 system), "
    "1 unreadable.",
    "Findings: client waiting 1, follow up with client 0, event outreach due 0, "
    "event details pending 0, event over 0, location TBD soon 0, needs review 0, "
    "ready to close 0.",
    'LLM: off; 1 "requestor wrote last" finding keeps its wording.',
    "Checked: 5 unclaimed (3 feeds needed, 1 missing), 2 claimed (counted only). Events: 1, "
    "0 recurring, 0 multi-date, 1 with an unreadable date, 0 without a location. "
    "Form-titled tickets: 0 (0 recurring, 0 unreadable); renamed: 0 with a form, 0 without. "
    "Event states (no finding): 0 waiting on requester, 0 in progress, 0 outreach not due yet, "
    "0 marketing only, 0 no outreach. Desk members known: 2.",
    "Unreadable dates: 0. Duplicates: 0. Blocked requests: 0. Live updates blocked: 6. "
    "Allowed non-GET: 6.",
]


def test_tdx_audit_help_shows_the_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["tdx", "audit", "--help"])

    out = capsys.readouterr().out
    assert exit_info.value.code == 0
    for option in ["--config PATH", "--headed", "--show-ids", "--verbose"]:
        assert option in out


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        (
            "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet.aspx?TicketID=",
            'tdx.ticket_url_template must contain "{id}"',
        ),
        (
            "https://elsewhere.example.edu/TDNext/Apps/43/Tickets/TicketDet.aspx?TicketID={id}",
            "tdx.ticket_url_template must be on the same host as tdx.base_url",
        ),
    ],
)
def test_tdx_audit_exits_2_for_a_bad_ticket_template(
    template: str,
    expected: str,
    rows_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows_env.setenv("OPS_SUITE_TDX__TICKET_URL_TEMPLATE", template)
    browser, _ = _audit_env(rows_env)

    code = main(["tdx", "audit"])

    assert code == 2
    assert expected in capsys.readouterr().err
    assert browser.profile_dir is None  # no browser was started


def test_tdx_audit_prints_counts_only(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TDX__RULES__EXTRA_STAFF_NAMES", '["Riley Testperson"]')
    browser, feeds = _audit_env(rows_env)

    code = main(["tdx", "audit"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == AUDIT_SUMMARY
    # Only the stale unclaimed, non-event tickets, oldest Created first.
    assert feeds.ticket_ids == [4410003, 4410001, 4410002]
    assert browser.closed
    assert browser.options["headless"] is True

    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines()]
    started = next(r for r in records if r["message"] == "tdx audit started")
    assert "<1 names>" in started["rules"]
    finished = [r for r in records if r["message"] == "command finished"][-1]
    assert (finished["command"], finished["exit_code"], finished["findings"]) == (
        "tdx audit",
        0,
        1,
    )
    for text in (captured.out, captured.err, log_text):
        for invented in AUDIT_INVENTED:
            assert invented not in text
    for ticket_id in ("4410001", "4410003", "4409002"):
        assert ticket_id not in captured.out


def test_tdx_audit_show_ids_adds_ids_and_codes_only(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--show-ids"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out == [
        *AUDIT_SUMMARY[:7],
        "  client_waiting  4410001  requestor_last  73h",
        "  event_date_unreadable  4410005",
        "  feed_failed  4410003",
        AUDIT_SUMMARY[7],
    ]
    for line in out:
        for invented in AUDIT_INVENTED:
            assert invented not in line


def test_tdx_audit_verbose_adds_shapes_and_paths(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--verbose"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[: len(AUDIT_SUMMARY)] == AUDIT_SUMMARY
    assert out[len(AUDIT_SUMMARY) :] == [
        "Unreadable entries: no author (1)",
        "Entry markers: 5 entries, 1 nested; private marker present on 3, visible on 1; "
        "inherited-private 0; merge notes 0; reassignments 0; "
        "signature desk replies 0; visibility fallback 0.",
        f"Live update blocked: {SOCKET} (3)",
        f"Live update blocked: {HUBS} (3)",
        f"Allowed non-GET: {WIDGET_POST} (4)",
        f"Allowed non-GET: {FEED_POST} (2)",
        "Desk members remembered: 2.",
    ]


def test_the_description_lines_are_counts_and_place_names_only() -> None:
    shapes = dict.fromkeys(DESCRIPTION_SHAPE_KEYS, 0) | {
        "read": 5,
        "hidden": 5,
        "toggle": 5,
        "space_container": 5,
        "services_container": 5,
        "space_anywhere": 5,
        "services_anywhere": 5,
        "forms": 5,
        "blocks": 10,
        "complete": 5,
        "from_container": 5,
    }

    assert cli.format_description_lines(shapes) == [
        "Event form descriptions: 5 read; with hidden text 5, clipped 0, with a toggle 5.",
        "Event form labels (space/services) found in: shown 0/0, container 5/5, outside 0/0, "
        "text box 0/0, template 0/0, attribute 0/0, script 0/0, anywhere 5/5.",
        "Event form blocks: 5 forms, 10 Response Summary blocks, 5 with all 6 answers; used "
        "from container 5, outside 0, text box 0, template 0, attribute 0, script 0.",
    ]


# --- the desk-member memory --------------------------------------------------------------

ASSISTANT = "Quinn Samplehelper"
MEMORY_INVENTED = [*AUDIT_INVENTED, ASSISTANT, "Quinn", "Samplehelper"]


def _memory_run(rows_env: pytest.MonkeyPatch, run: int) -> None:
    """Run 1: the assistant writes a private note on ticket A. Run 2: the assistant's public
    reply, 3 hours old, is the latest entry on ticket B, and they appear nowhere else."""
    now = datetime.now(TZ)
    ticket_id = 4420000 + run
    rows = (_audit_row("unclaimed", ticket_id, now, 100),)
    dashboard = _dashboard_read(
        widgets={
            "unclaimed": WidgetRead("unclaimed", rows, 1, (), False),
            "claimed": WidgetRead("claimed", (), 1, (), False),
        }
    )
    if run == 1:
        entries = (
            FeedEntry("Samplehelper, Quinn", now - timedelta(hours=70), "private"),
            FeedEntry("Alex Example", now - timedelta(hours=90), "reply"),
        )
    else:
        entries = (
            FeedEntry(ASSISTANT, now - timedelta(hours=3), "reply"),
            FeedEntry("Alex Example", now - timedelta(hours=90), "reply"),
        )
    feeds = FeedRead(
        feeds={ticket_id: TicketFeed(ticket_id, entries)},
        failed=(),
        deferred=(),
        blocked=(),
        allowed_non_get=(),
    )
    rows_env.setattr(watch_module, "TdxBrowser", FakeTdxBrowser([]))
    rows_env.setattr(watch_module, "read_dashboard", FakeRead(dashboard))
    rows_env.setattr(watch_module, "read_feeds", FakeFeeds(feeds))


def _summary_counts(tmp_path: Path) -> list[dict]:
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    return [
        json.loads(line)
        for line in log_text.splitlines()
        if json.loads(line)["message"] == "tdx audit summary"
    ]


def test_tdx_audit_remembers_a_desk_member_across_runs(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _memory_run(rows_env, 1)
    assert main(["tdx", "audit", "--verbose"]) == 0
    first = capsys.readouterr()
    _memory_run(rows_env, 2)
    assert main(["tdx", "audit", "--verbose"]) == 0
    second = capsys.readouterr()

    assert "Desk members remembered: 1." in first.out.splitlines()
    assert "Desk members remembered: 1." in second.out.splitlines()
    assert (
        "Findings: client waiting 0, follow up with client 0, event outreach due 0, "
        "event details pending 0, event over 0, location TBD soon 0, needs review 0, "
        "ready to close 0."
    ) in second.out.splitlines()
    records = _summary_counts(tmp_path)
    assert [r["desk_remembered"] for r in records] == [1, 1]
    counts = records[1]["counts"]
    assert (counts["desk_replied_last"], counts["other_recent"], counts["needs_review"]) == (
        1,
        0,
        0,
    )
    assert counts["desk_members_known"] == 0  # known from the memory, not from this run

    memory_file = tmp_path / "appdata" / "tdx" / "desk-members.json"
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    texts = (first.out, first.err, second.out, second.err, log_text, memory_file.read_text())
    for text in texts:
        for invented in MEMORY_INVENTED:
            assert invented.casefold() not in text.casefold()


def test_tdx_audit_with_the_memory_off_uses_the_gate_alone(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TDX__RULES__DESK_MEMORY", "false")
    _memory_run(rows_env, 1)
    assert main(["tdx", "audit", "--verbose"]) == 0
    _memory_run(rows_env, 2)
    assert main(["tdx", "audit", "--verbose"]) == 0
    out = capsys.readouterr().out

    assert "Desk members remembered" not in out
    records = _summary_counts(tmp_path)
    assert [r["desk_remembered"] for r in records] == [None, None]
    counts = records[1]["counts"]
    assert (counts["desk_replied_last"], counts["other_recent"], counts["needs_review"]) == (
        0,
        1,
        0,
    )
    assert not list((tmp_path / "appdata").rglob("desk-members.json*"))


def test_tdx_audit_without_verbose_has_no_memory_line(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _memory_run(rows_env, 1)
    assert main(["tdx", "audit"]) == 0
    assert "Desk members remembered" not in capsys.readouterr().out


def test_tdx_audit_lists_blocked_requests_apart_from_live_updates(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, feeds = _audit_scenario(datetime.now(TZ))
    stray = "POST /TDNext/Apps/43/Tickets/Update"
    _audit_env(rows_env, replace(feeds, blocked=(stray,)))

    assert main(["tdx", "audit"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[-2] == (
        "Unreadable dates: 0. Duplicates: 0. Blocked requests: 1. Live updates blocked: 6. "
        "Allowed non-GET: 6."
    )
    assert out[-1] == f"Blocked: {stray} (1)"


def test_tdx_audit_feed_structure_problem_exits_5(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = DashboardStructureError(
        "no activity feed was found on 3 tickets in a row (step: open each ticket and wait up "
        "to 30 s for its feed)",
        ("POST /TDNext/Apps/43/Tickets/Update",),
    )
    _audit_env(rows_env, error)

    code = main(["tdx", "audit"])

    captured = capsys.readouterr()
    assert code == 5
    assert captured.err.splitlines() == [
        f"ops-suite: the ticket pages did not have the expected structure: {error}.",
        "Blocked: POST /TDNext/Apps/43/Tickets/Update (1)",
        cli.REPORT_BLOCKED,
    ]
    assert captured.out == "Dashboard: start page\n"


def test_tdx_audit_not_signed_in_on_a_ticket_exits_3(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    browser, _ = _audit_env(rows_env, NotSignedInError())

    assert main(["tdx", "audit"]) == 3
    assert capsys.readouterr().err.strip() == f"ops-suite: {cli.SESSION_EXPIRED}"
    assert browser.closed


def test_tdx_audit_browser_failure_exits_4(rows_env: pytest.MonkeyPatch) -> None:
    rows_env.setattr(
        watch_module, "TdxBrowser", FakeTdxBrowser([], start_error="Executable missing")
    )

    assert main(["tdx", "audit"]) == 4


def test_tdx_audit_progress_goes_to_stderr_only(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _audit_env(rows_env)

    assert main(["tdx", "audit"]) == 0

    captured = capsys.readouterr()
    # Not a terminal: printed every 5 tickets and at the end; here 3 feeds are read.
    assert captured.err.splitlines() == ["Reading feeds: 3/3"]
    assert "Reading feeds" not in captured.out
    assert captured.out.splitlines() == AUDIT_SUMMARY


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_feed_progress_rewrites_one_line_on_a_terminal() -> None:
    stream = _Stream(tty=True)
    progress = cli.FeedProgress(stream)
    for done in range(1, 4):
        progress(done, 3)

    assert stream.getvalue() == ("\rReading feeds: 1/3\rReading feeds: 2/3\rReading feeds: 3/3\n")


def test_feed_progress_prints_every_five_otherwise() -> None:
    stream = _Stream(tty=False)
    progress = cli.FeedProgress(stream)
    for done in range(1, 13):
        progress(done, 12)

    assert stream.getvalue().splitlines() == [
        "Reading feeds: 5/12",
        "Reading feeds: 10/12",
        "Reading feeds: 12/12",
    ]


# --- teams set-webhook / teams test ----------------------------------------------------------

WEBHOOK = "https://prod-01.westus.logic.azure.com:443/workflows/abc/invoke?sig=SIGSECRET42"


def _log_text(tmp_path: Path) -> str:
    log_file = tmp_path / "applogs" / "ops-suite.log"
    return log_file.read_text(encoding="utf-8") if log_file.exists() else ""


def _fake_getpass(monkeypatch: pytest.MonkeyPatch, value: str) -> list[str]:
    prompts: list[str] = []

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        return value

    monkeypatch.setattr(cli.getpass, "getpass", fake)
    return prompts


def test_teams_set_webhook_reads_with_getpass_and_never_echoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = _fake_getpass(monkeypatch, WEBHOOK)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("no question when none saved"))

    assert main(["teams", "set-webhook"]) == 0

    captured = capsys.readouterr()
    assert prompts == ["Webhook URL (input hidden): "]
    assert captured.out == "Webhook saved in the system keychain.\n"
    assert cli.get_webhook_url() == WEBHOOK
    for text in (captured.out, captured.err, _log_text(tmp_path)):
        assert "SIGSECRET42" not in text and "logic.azure.com" not in text


def test_teams_set_webhook_rejects_a_bad_address_without_quoting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_getpass(monkeypatch, "http://prod-01.westus.logic.azure.com/x?sig=SIGSECRET42")

    assert main(["teams", "set-webhook"]) == 2

    captured = capsys.readouterr()
    assert "not an https Power Automate address" in captured.err
    assert cli.get_webhook_url() is None
    for text in (captured.out, captured.err, _log_text(tmp_path)):
        assert "SIGSECRET42" not in text


@pytest.mark.parametrize(("answer", "replaced"), [("y", True), ("", False), ("no", False)])
def test_teams_set_webhook_asks_before_replacing(
    answer: str,
    replaced: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    new = WEBHOOK.replace("prod-01", "prod-02")
    prompts = _fake_getpass(monkeypatch, new)
    questions: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": questions.append(prompt) or answer)

    assert main(["teams", "set-webhook"]) == 0

    assert questions == ["Replace the saved webhook? [y/N] "]
    assert cli.get_webhook_url() == (new if replaced else WEBHOOK)
    assert prompts == (["Webhook URL (input hidden): "] if replaced else [])
    out = capsys.readouterr().out
    assert ("Webhook saved" in out) is replaced
    assert "SIGSECRET42" not in out


def test_teams_test_without_a_webhook_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["teams", "test"]) == 2
    assert 'run "uv run ops-suite teams set-webhook" first' in capsys.readouterr().err


class FakePost:
    """Stands in for post_card: records the cards, answers 202 or raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.cards: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, url: str, card: dict, *, timeout: float) -> int:
        self.urls.append(url)
        self.cards.append(card)
        if self.error is not None:
            raise self.error
        return 202


def test_teams_test_posts_the_synthetic_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    monkeypatch.setattr(cli, "post_card", post)

    assert main(["teams", "test"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "Test card sent.\n"
    assert post.urls == [WEBHOOK]
    assert post.cards[0]["body"][0]["text"] == "ops-suite test card (synthetic)"
    for text in (captured.out, captured.err, _log_text(tmp_path)):
        assert "SIGSECRET42" not in text


def test_teams_test_prints_the_reason_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    monkeypatch.setattr(cli, "post_card", FakePost(cli.TeamsPostError(404, "http_404")))

    assert main(["teams", "test"]) == cli.EXIT_TEAMS

    captured = capsys.readouterr()
    assert captured.err == "ops-suite: the test card was not sent (http_404).\n"
    assert captured.out == ""


# --- tdx audit --send / tdx watch ------------------------------------------------------------

AUDIT_PRIVATE = AUDIT_INVENTED + ["SIGSECRET42", "logic.azure.com", "Wi-Fi"]


def _state_file(tmp_path: Path) -> Path:
    return tmp_path / "appdata" / "tdx" / "alerts-state.json"


def _assert_private_text_absent(tmp_path: Path, *texts: str) -> None:
    log_text = _log_text(tmp_path)
    assert log_text  # the log file was written
    for text in (*texts, log_text):
        for private in AUDIT_PRIVATE:
            assert private not in text


def test_tdx_audit_without_send_sends_nothing_and_leaves_no_state(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)

    assert main(["tdx", "audit"]) == 0

    assert capsys.readouterr().out.splitlines() == AUDIT_SUMMARY
    assert post.cards == []
    assert not _state_file(tmp_path).exists()


def test_tdx_audit_send_posts_once_then_nothing_new(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--send"]) == 0
    first = capsys.readouterr()
    assert first.out.splitlines() == [*AUDIT_SUMMARY, "Teams: sent 1 card (1 new)."]

    assert main(["tdx", "audit", "--send"]) == 0
    second = capsys.readouterr()
    assert second.out.splitlines() == [*AUDIT_SUMMARY, "Teams: nothing new."]

    assert len(post.cards) == 1 and post.urls == [WEBHOOK]
    card_text = json.dumps(post.cards[0])
    assert "Laptop will not connect to campus Wi-Fi" in card_text  # the card carries the title
    assert "Alex Example" not in card_text and "Pat Helpdesk" not in card_text
    state_text = _state_file(tmp_path).read_text(encoding="utf-8")
    assert "4410001:client_waiting" in state_text
    _assert_private_text_absent(tmp_path, first.out, first.err, second.out, second.err, state_text)


def test_tdx_audit_send_failure_keeps_the_findings_due(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    rows_env.setattr(watch_module, "post_card", FakePost(cli.TeamsPostError(400, "http_400")))
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--send"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == "Teams: not sent (http_400); will retry next run."
    assert "4410001" not in _state_file(tmp_path).read_text(encoding="utf-8")
    _assert_private_text_absent(tmp_path, captured.out, captured.err)


def test_tdx_audit_send_without_a_webhook_hints_and_exits_0(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--send"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == AUDIT_SUMMARY
    assert captured.err.splitlines()[-1] == (
        'Teams: not sent; run "uv run ops-suite teams set-webhook" first.'
    )
    assert not _state_file(tmp_path).exists()


def test_tdx_watch_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["tdx", "watch", "--help"])
    out = capsys.readouterr().out
    for option in ["--config PATH", "--headed", "--verbose"]:
        assert option in out


def test_tdx_watch_without_a_webhook_exits_2_before_any_browser(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    browser, _ = _audit_env(rows_env)

    assert main(["tdx", "watch"]) == 2

    assert 'run "uv run ops-suite teams set-webhook" first' in capsys.readouterr().err
    assert browser.profile_dir is None


def test_tdx_watch_runs_a_cycle_and_prints_counts_only(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    browser, _ = _audit_env(rows_env)
    real_watch = watch_module.watch
    friday = datetime(2026, 10, 16, 14, 5, tzinfo=TZ)

    def stop(seconds: float) -> None:
        raise KeyboardInterrupt

    def fixed_watch(
        settings, *, clock, sleep, run_cycle, notify, destination="personal", shadow=None
    ):
        assert shadow is None  # the default source runs no shadow step
        assert clock().tzinfo is not None
        return real_watch(
            settings,
            clock=lambda: friday,
            sleep=stop,
            run_cycle=run_cycle,
            notify=notify,
            destination=destination,
        )

    rows_env.setattr(cli, "watch", fixed_watch)

    assert main(["tdx", "watch"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Watching the TDX queue every 30 min during desk hours (America/New_York). Ctrl+C stops.",
        "14:05 5 unclaimed, 2 feeds, 1 finding; Teams: sent 1 card (1 new). Next run 14:35.",
        "Stopped.",
    ]
    assert captured.err == ""  # no progress line in watch mode
    assert len(post.cards) == 1
    assert browser.options["headless"] is True
    records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
    cycle = next(r for r in records if r["message"] == "tdx watch cycle")
    assert (cycle["send"], cycle["new"], cycle["findings"]) == ("sent", 1, 1)
    _assert_private_text_absent(tmp_path, captured.out, captured.err)


def test_feed_progress_uses_line_mode_under_verbose() -> None:
    stream = _Stream(tty=True)
    progress = cli.FeedProgress(stream, verbose=True)
    for done in range(1, 7):
        progress(done, 6)

    assert stream.getvalue() == "Reading feeds: 5/6\nReading feeds: 6/6\n"


def test_tdx_audit_verbose_progress_is_line_mode_on_a_terminal(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _audit_env(rows_env)
    rows_env.setattr(cli.sys.stderr, "isatty", lambda: True, raising=False)

    assert main(["tdx", "audit", "--verbose"]) == 0

    assert "\r" not in capsys.readouterr().err


# --- the follow-up classifier in tdx audit / watch, and llm eval --------------------------

LLM_ON = {"OPS_SUITE_LLM__ENABLED": "true", "OPS_SUITE_LLM__ALLOW_INSTITUTIONAL_DATA": "true"}


class FakeLlmClient:
    """Stands in for LlmClient (built as LlmClient(settings, ...)); scripted answers."""

    instances: list["FakeLlmClient"] = []
    answers: list[object] = []

    def __init__(self, settings, **options) -> None:
        self.settings = settings
        self.calls: list[dict] = []
        FakeLlmClient.instances.append(self)

    def classify(self, text, **options):
        self.calls.append({"text": text.text, **options})
        answer = FakeLlmClient.answers.pop(0) if FakeLlmClient.answers else "expects_reply"
        if isinstance(answer, Exception):
            raise answer
        usage = LlmUsage("claude-haiku-5-5", 700, 9, 650, 0, 0.0001)
        return LlmResult(FollowUpAnswer(verdict=answer), "ok", usage)

    def used_today(self) -> LlmDayUsage:
        calls = sum(len(instance.calls) for instance in FakeLlmClient.instances)
        return LlmDayUsage("2026-10-16", calls, 700 * calls, 9 * calls, 650 * calls, 0, 0.0001)


@pytest.fixture
def fake_llm(rows_env: pytest.MonkeyPatch) -> type[FakeLlmClient]:
    FakeLlmClient.instances = []
    FakeLlmClient.answers = []
    rows_env.setattr(watch_module, "LlmClient", FakeLlmClient)
    return FakeLlmClient


def _llm_env(rows_env: pytest.MonkeyPatch, **extra: str) -> None:
    for name, value in {**LLM_ON, **extra}.items():
        rows_env.setenv(name, value)


def _verdict_file(tmp_path: Path) -> Path:
    return tmp_path / "appdata" / "tdx" / "llm-verdicts.json"


LLM_FOLLOW_UP_FINDINGS = (
    "Findings: client waiting 0, follow up with client 1, event outreach due 0, "
    "event details pending 0, event over 0, location TBD soon 0, needs review 0, "
    "ready to close 0."
)


def test_tdx_audit_with_the_llm_changes_the_finding_and_prints_counts_only(
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    fake_llm: type[FakeLlmClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _llm_env(rows_env)
    _audit_env(rows_env)
    fake_llm.answers = ["client_will_follow_up"]

    assert main(["tdx", "audit", "--show-ids"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[4:7] == [
        LLM_FOLLOW_UP_FINDINGS,
        "LLM: 1 to check, 0 cached, 1 classified (expects reply 0, will follow up 1, no reply "
        "needed 0, unsure 0); skipped 0, failed 0. Today 774 of 100,000 tokens (~$0.0001).",
        AUDIT_SUMMARY[6],
    ]
    assert "  follow_up_with_client  4410001  client_follow_up  73h" in out
    (call,) = fake_llm.instances[0].calls
    assert call["data_class"] == "institutional"  # hard-coded for the live queue
    assert call["purpose"] == "follow_up" and call["ref"] == "4410001"
    # Scrubbed, with the sign-off stripped: no name or address is sent.
    assert call["text"] == (
        "Hi [NAME],\nI am at the Zanzibar workshop until Monday; I will try the new profile "
        "then and let you know."
    )
    verdict_text = _verdict_file(tmp_path).read_text(encoding="utf-8")
    verdicts = json.loads(verdict_text)
    assert set(verdicts["verdicts"]) == {"4410001"}
    assert verdicts["verdicts"]["4410001"]["verdict"] == "client_will_follow_up"
    _assert_private_text_absent(tmp_path, "\n".join(out), verdict_text)


def test_a_second_audit_uses_the_cached_verdict_and_makes_no_call(
    rows_env: pytest.MonkeyPatch,
    fake_llm: type[FakeLlmClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _llm_env(rows_env)
    _audit_env(rows_env)
    fake_llm.answers = ["no_reply_needed"]
    assert main(["tdx", "audit"]) == 0
    capsys.readouterr()

    assert main(["tdx", "audit"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert fake_llm.instances[1].calls == []
    assert out[4] == LLM_FOLLOW_UP_FINDINGS
    assert out[5].startswith("LLM: 1 to check, 1 cached, 0 classified (expects reply 0, ")


def test_tdx_audit_send_posts_the_follow_up_wording(
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    fake_llm: type[FakeLlmClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _llm_env(rows_env)
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)
    fake_llm.answers = ["client_will_follow_up"]

    assert main(["tdx", "audit", "--send"]) == 0

    card_text = json.dumps(post.cards[0])
    assert "Follow up with requester (1)" in card_text
    assert "The requester said they'd follow up; quiet 3 days: check in" in card_text
    assert "Requester waiting" not in card_text
    state_text = _state_file(tmp_path).read_text(encoding="utf-8")
    assert "4410001:follow_up_with_client" in state_text
    captured = capsys.readouterr()
    _assert_private_text_absent(tmp_path, captured.out, captured.err, state_text)


def test_the_closed_gate_shows_as_a_skip_and_changes_nothing_else(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real client: the gate stops it before the key or the network are looked at.
    _llm_env(rows_env, OPS_SUITE_LLM__ALLOW_INSTITUTIONAL_DATA="false")
    _audit_env(rows_env)

    assert main(["tdx", "audit"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[5] == (
        "LLM: 1 to check, 0 cached, 0 classified (expects reply 0, will follow up 0, no reply "
        "needed 0, unsure 0); skipped 1 (gate), failed 0. Today 0 of 100,000 tokens (~$0.0000)."
    )
    assert out[:5] + out[6:] == AUDIT_SUMMARY[:5] + AUDIT_SUMMARY[6:]
    assert not _verdict_file(tmp_path).read_text(encoding="utf-8").count("4410001")


def test_no_llm_turns_the_step_off_for_one_run(
    rows_env: pytest.MonkeyPatch,
    fake_llm: type[FakeLlmClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _llm_env(rows_env)
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--no-llm"]) == 0

    assert capsys.readouterr().out.splitlines() == AUDIT_SUMMARY
    assert fake_llm.instances == []  # no client was built


def test_tdx_audit_verbose_adds_the_llm_detail_line(
    rows_env: pytest.MonkeyPatch,
    fake_llm: type[FakeLlmClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _llm_env(rows_env)
    _audit_env(rows_env)

    assert main(["tdx", "audit", "--verbose"]) == 0

    assert (
        "LLM detail: this run 700 in, 9 out, 650 cache-read tokens; 0 cut at llm.max_input_chars."
    ) in capsys.readouterr().out.splitlines()


def test_tdx_watch_uses_the_same_llm_path_and_takes_no_llm(
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    fake_llm: type[FakeLlmClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _llm_env(rows_env)
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)
    real_watch = watch_module.watch
    friday = datetime(2026, 10, 16, 14, 5, tzinfo=TZ)
    fake_llm.answers = ["no_reply_needed"]

    def stop(seconds: float) -> None:
        raise KeyboardInterrupt

    def fixed_watch(
        settings, *, clock, sleep, run_cycle, notify, destination="personal", shadow=None
    ):
        assert shadow is None  # the default source runs no shadow step
        return real_watch(
            settings,
            clock=lambda: friday,
            sleep=stop,
            run_cycle=run_cycle,
            notify=notify,
            destination=destination,
        )

    rows_env.setattr(cli, "watch", fixed_watch)

    assert main(["tdx", "watch"]) == 0
    assert "needed no reply; quiet 3 days: check in or resolve" in json.dumps(post.cards[0])
    records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
    cycle = next(r for r in records if r["message"] == "tdx watch cycle")
    assert cycle["llm"]["classified"] == 1

    assert main(["tdx", "watch", "--no-llm"]) == 0
    assert len(fake_llm.instances) == 1
    captured = capsys.readouterr()
    _assert_private_text_absent(tmp_path, captured.out, captured.err)


def test_the_audit_and_watch_help_show_no_llm(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("audit", "watch"):
        with pytest.raises(SystemExit):
            main(["tdx", command, "--help"])
        assert "--no-llm" in capsys.readouterr().out


class FakeEvalClient(FakeLlmClient):
    def __init__(self, settings, **options) -> None:
        assert options.get("api_key")
        super().__init__(settings)


@pytest.fixture
def eval_env(monkeypatch: pytest.MonkeyPatch) -> type[FakeLlmClient]:
    FakeLlmClient.instances = []
    FakeLlmClient.answers = []
    monkeypatch.setattr(cli, "LlmClient", FakeEvalClient)
    return FakeLlmClient


def test_llm_eval_without_a_key_exits_2(
    eval_env: type[FakeLlmClient], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["llm", "eval"]) == 2

    assert 'run "uv run ops-suite llm set-key" first' in capsys.readouterr().err
    assert eval_env.instances == []


def test_llm_eval_prints_counts_only(
    tmp_path: Path, eval_env: type[FakeLlmClient], capsys: pytest.CaptureFixture[str]
) -> None:
    set_api_key("sk-ant-test-0000")
    path = cli.EVAL_DIR / "follow_up_holdout.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    eval_env.answers = [case["expected"] for case in cases if case["text"] != "..."]

    assert main(["llm", "eval", "--set", "holdout"]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    calls = eval_env.instances[0].calls
    assert lines[0] == (
        "Eval holdout: 12 of 12 right (100%); unsafe errors 0; 1 empty after stripping (unsure)."
    )
    assert {call["data_class"] for call in calls} == {"synthetic"}
    assert all(call["force_enabled"] for call in calls)
    assert lines[-1].startswith("Tokens: 11 calls, ")
    shown = captured.out + captured.err + _log_text(tmp_path)
    for case in cases:
        if len(case["text"]) > 5:
            assert case["text"] not in shown
    assert "sk-ant-test" not in shown


def test_llm_eval_exits_7_after_three_failures(
    eval_env: type[FakeLlmClient], capsys: pytest.CaptureFixture[str]
) -> None:
    set_api_key("sk-ant-test-0000")
    eval_env.answers = [LlmFailed("timeout")] * 3

    assert main(["llm", "eval"]) == 7

    assert "LLM eval: stopped (failed)." in capsys.readouterr().err
    assert len(eval_env.instances[0].calls) == 3


def test_llm_eval_exits_7_when_the_budget_is_spent(
    eval_env: type[FakeLlmClient], capsys: pytest.CaptureFixture[str]
) -> None:
    set_api_key("sk-ant-test-0000")
    eval_env.answers = [LlmUnavailable("budget")]

    assert main(["llm", "eval"]) == 7
    assert "LLM eval: stopped (budget)." in capsys.readouterr().err


# --- the desk chat --------------------------------------------------------------------------

DESK_WEBHOOK = WEBHOOK.replace("prod-01", "prod-09").replace("SIGSECRET42", "SIGSECRET42DESK")


def _desk_state_file(tmp_path: Path) -> Path:
    return tmp_path / "appdata" / "tdx" / "alerts-state-desk.json"


@pytest.mark.parametrize(
    "command",
    [
        ["teams", "set-webhook"],
        ["teams", "test"],
        ["tdx", "audit"],
        ["tdx", "watch"],
        ["tdx", "demo"],
    ],
)
def test_to_is_offered_with_personal_and_desk(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([*command, "--help"])
    out = capsys.readouterr().out
    assert "--to CHAT" in out and "personal" in out and "desk" in out
    with pytest.raises(SystemExit):
        main([*command, "--to", "everyone"])
    assert "invalid choice" in capsys.readouterr().err


def test_teams_set_webhook_to_desk_saves_the_desk_entry_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    prompts = _fake_getpass(monkeypatch, DESK_WEBHOOK)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("no desk webhook saved yet"))

    assert main(["teams", "set-webhook", "--to", "desk"]) == 0

    captured = capsys.readouterr()
    assert prompts == ["Desk webhook URL (input hidden): "]
    assert captured.out == "Desk webhook saved in the system keychain.\n"
    assert memory_keyring.passwords == {
        ("ops-suite", "teams-webhook"): WEBHOOK,
        ("ops-suite", "teams-webhook-desk"): DESK_WEBHOOK,
    }
    for text in (captured.out, captured.err, _log_text(tmp_path)):
        assert "SIGSECRET42" not in text and "logic.azure.com" not in text


def test_teams_set_webhook_to_desk_asks_before_replacing_the_desk_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    questions: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": questions.append(prompt) or "")

    assert main(["teams", "set-webhook", "--to", "desk"]) == 0

    assert questions == ["Replace the saved desk webhook? [y/N] "]
    assert capsys.readouterr().out == "Kept the saved desk webhook.\n"
    assert cli.get_webhook_url("desk") == DESK_WEBHOOK


def test_teams_test_to_desk_needs_the_desk_webhook(capsys: pytest.CaptureFixture[str]) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])  # personal only

    assert main(["teams", "test", "--to", "desk"]) == 2

    assert capsys.readouterr().err == (
        'ops-suite: no desk webhook is saved; run "uv run ops-suite teams set-webhook --to desk" '
        "first\n"
    )


def test_teams_test_to_desk_is_synthetic_and_needs_no_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    post = FakePost()
    monkeypatch.setattr(cli, "post_card", post)

    assert main(["teams", "test", "--to", "desk"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "Test card sent to the desk chat.\n"
    assert post.urls == [DESK_WEBHOOK]
    assert post.cards[0]["body"][0]["text"] == "ops-suite test card (synthetic)"
    for text in (captured.out, captured.err, _log_text(tmp_path)):
        assert "SIGSECRET42" not in text


@pytest.mark.parametrize("command", [["tdx", "audit", "--send"], ["tdx", "watch"]])
def test_real_tdx_to_desk_without_approval_exits_2_before_reading_tdx(
    command: list[str],
    tmp_path: Path,
    rows_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    browser, feeds = _audit_env(rows_env)

    assert main([*command, "--to", "desk"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "teams.desk.approved" in captured.err
    assert browser.profile_dir is None  # no browser, so TDX was never read
    assert post.cards == []
    assert not _state_file(tmp_path).exists() and not _desk_state_file(tmp_path).exists()


def test_tdx_audit_to_desk_without_send_is_just_the_audit(
    rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _audit_env(rows_env)
    assert main(["tdx", "audit", "--to", "desk"]) == 0
    assert capsys.readouterr().out.splitlines() == AUDIT_SUMMARY


def test_tdx_audit_send_to_approved_desk_posts_without_titles_and_its_own_state(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TEAMS__DESK__APPROVED", "true")
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)

    # The personal chat already has the finding; the desk chat still gets it as new.
    assert main(["tdx", "audit", "--send"]) == 0
    personal_text = _state_file(tmp_path).read_text(encoding="utf-8")
    assert main(["tdx", "audit", "--send", "--to", "desk"]) == 0
    assert main(["tdx", "audit", "--send", "--to", "desk"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out.count("Teams: sent 1 card (1 new).") == 1
    assert out.count("Teams (desk): sent 1 card (1 new).") == 1
    assert out.count("Teams (desk): nothing new.") == 1
    assert post.urls == [WEBHOOK, DESK_WEBHOOK]
    assert "Laptop will not connect to campus Wi-Fi" in json.dumps(post.cards[0])
    desk_card = json.dumps(post.cards[1])
    assert "Wi-Fi" not in desk_card and "Laptop" not in desk_card
    assert "#4410001" in desk_card
    assert _state_file(tmp_path).read_text(encoding="utf-8") == personal_text
    assert "4410001:client_waiting" in _desk_state_file(tmp_path).read_text(encoding="utf-8")


def test_tdx_watch_to_approved_desk_posts_to_desk_and_warns_without_personal(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows_env.setenv("OPS_SUITE_TEAMS__DESK__APPROVED", "true")
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)
    real_watch = watch_module.watch
    friday = datetime(2026, 10, 16, 14, 5, tzinfo=TZ)

    def stop(seconds: float) -> None:
        raise KeyboardInterrupt

    def fixed_watch(
        settings, *, clock, sleep, run_cycle, notify, destination="personal", shadow=None
    ):
        assert shadow is None  # the default source runs no shadow step
        return real_watch(
            settings,
            clock=lambda: friday,
            sleep=stop,
            run_cycle=run_cycle,
            notify=notify,
            destination=destination,
        )

    rows_env.setattr(cli, "watch", fixed_watch)

    assert main(["tdx", "watch", "--to", "desk"]) == 0

    captured = capsys.readouterr()
    assert "Cards go to the desk chat." in captured.out
    assert "Teams (desk): sent 1 card (1 new)." in captured.out
    assert captured.err.count("sign-in notices need a personal webhook") == 1
    assert post.urls == [DESK_WEBHOOK]
    assert "Wi-Fi" not in json.dumps(post.cards[0])
    assert not _state_file(tmp_path).exists()
    assert _desk_state_file(tmp_path).exists()
    _assert_private_text_absent(tmp_path, captured.out, captured.err)


def test_tdx_demo_send_to_desk_needs_the_desk_webhook(capsys: pytest.CaptureFixture[str]) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])

    assert main(["tdx", "demo", "--send", "--to", "desk"]) == 2

    assert "no desk webhook is saved" in capsys.readouterr().err


# tdx agent: launchd is faked, so these run on every OS.

AGENT_UV = "/Users/owner/.local/bin/uv"


class FakeLaunchctl:
    """launchctl with one job that is loaded or not; records the verbs it was called with."""

    def __init__(self) -> None:
        self.loaded = False
        self.verbs: list[str] = []

    def __call__(self, args) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.verbs.append(args[1])
        if args[1] == "print":
            if self.loaded:
                return subprocess.CompletedProcess(args, 0, "\tlast exit code = 0\n", "")
            return subprocess.CompletedProcess(args, 113, "", "Could not find service\n")
        self.loaded = args[1] == "bootstrap"
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def launchctl() -> FakeLaunchctl:
    return FakeLaunchctl()


@pytest.fixture
def mac_agent(
    tmp_path: Path, launchctl: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch
) -> MacLaunchAgent:
    """Makes the CLI see macOS, with a fake launchctl and a LaunchAgents folder in tmp_path."""
    agent = MacLaunchAgent(
        repo=cli.REPO_DIR,
        log_dir=tmp_path / "applogs",
        agents_dir=tmp_path / "LaunchAgents",
        uid=501,
        runner=launchctl,
        which=lambda name: AGENT_UV,
        sleep=lambda seconds: None,
    )
    monkeypatch.setattr(cli, "for_platform", lambda **kwargs: agent)
    return agent


LOADED_WARNING = (
    "The start-at-login loop pauses while this window is open and resumes by itself afterwards."
)


@pytest.mark.parametrize("loaded", [True, False])
def test_tdx_login_warns_only_when_the_agent_is_loaded(
    loaded: bool,
    tdx_env: pytest.MonkeyPatch,
    mac_agent: MacLaunchAgent,
    launchctl: FakeLaunchctl,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launchctl.loaded = loaded
    browser = FakeTdxBrowser([TDX_BASE])
    tdx_env.setattr(cli, "TdxBrowser", browser)

    assert main(["tdx", "login"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert (LOADED_WARNING in out) is loaded
    if loaded:
        assert out[0] == LOADED_WARNING  # before the browser opens
    assert launchctl.verbs == ["print"]  # only asked; nothing loaded or unloaded


def _agent_arguments(agent: MacLaunchAgent) -> list[str]:
    with agent.plist_path.open("rb") as file:
        return plistlib.load(file)["ProgramArguments"]


@pytest.mark.parametrize("verb", ["install", "uninstall", "status"])
def test_tdx_agent_on_linux_exits_2(
    verb: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    monkeypatch.setattr(cli, "for_platform", lambda **kwargs: None)

    assert main(["tdx", "agent", verb]) == 2

    assert capsys.readouterr().err == (
        "ops-suite: start at login is for macOS and Windows only; "
        'run "uv run ops-suite tdx watch" in a terminal\n'
    )


def test_tdx_agent_install_writes_the_agent_and_prints_where_its_logs_go(
    tmp_path: Path,
    mac_agent: MacLaunchAgent,
    launchctl: FakeLaunchctl,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])

    assert main(["tdx", "agent", "install"]) == 0

    assert _agent_arguments(mac_agent)[-3:] == ["ops-suite", "tdx", "watch"]
    assert launchctl.loaded
    out = capsys.readouterr().out
    assert out.startswith("Installed: tdx watch starts now and at every login.\n")
    assert f"Agent file: {mac_agent.plist_path}\n" in out
    assert str(tmp_path / "applogs" / "watch.out.log") in out
    assert str(tmp_path / "applogs" / "watch.err.log") in out
    assert 'run "uv run ops-suite tdx login" in a Terminal' in out
    assert "SIGSECRET42" not in out


def test_tdx_agent_install_passes_to_headed_and_config_to_watch(
    test_config: Path,
    mac_agent: MacLaunchAgent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPS_SUITE_TEAMS__DESK__APPROVED", "true")
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")

    args = ["tdx", "agent", "install", "--to", "desk", "--headed", "--config", str(test_config)]
    assert main(args) == 0

    assert _agent_arguments(mac_agent)[-6:] == [
        "watch",
        "--to",
        "desk",
        "--headed",
        "--config",
        str(test_config.resolve()),
    ]
    assert "Cards go to the desk chat." in capsys.readouterr().out


def test_tdx_agent_install_needs_the_destinations_webhook(
    mac_agent: MacLaunchAgent, launchctl: FakeLaunchctl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tdx", "agent", "install"]) == 2

    assert 'run "uv run ops-suite teams set-webhook" first' in capsys.readouterr().err
    assert not mac_agent.plist_path.exists()
    assert launchctl.verbs == []


def test_tdx_agent_install_to_desk_needs_the_desk_webhook(
    mac_agent: MacLaunchAgent, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPS_SUITE_TEAMS__DESK__APPROVED", "true")
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])  # personal only

    assert main(["tdx", "agent", "install", "--to", "desk"]) == 2

    assert "no desk webhook is saved" in capsys.readouterr().err
    assert not mac_agent.plist_path.exists()


def test_tdx_agent_install_to_desk_needs_the_approval(
    mac_agent: MacLaunchAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")

    assert main(["tdx", "agent", "install", "--to", "desk"]) == 2

    assert "teams.desk.approved" in capsys.readouterr().err
    assert not mac_agent.plist_path.exists()


@pytest.mark.parametrize(("answer", "replaced"), [("y", True), ("", False), ("no", False)])
def test_tdx_agent_install_asks_before_replacing(
    answer: str,
    replaced: bool,
    mac_agent: MacLaunchAgent,
    launchctl: FakeLaunchctl,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    assert main(["tdx", "agent", "install"]) == 0
    capsys.readouterr()
    launchctl.verbs.clear()
    questions: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": questions.append(prompt) or answer)

    assert main(["tdx", "agent", "install", "--headed"]) == 0

    assert questions == ["Replace the installed start-at-login agent? [y/N] "]
    assert ("--headed" in _agent_arguments(mac_agent)) is replaced
    out = capsys.readouterr().out
    if replaced:
        assert "bootout" in launchctl.verbs
        assert out.startswith("Installed:")
    else:
        assert out == "Kept the installed agent.\n"
        assert "bootout" not in launchctl.verbs


def test_tdx_agent_install_without_uv_exits_2(
    mac_agent: MacLaunchAgent, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    monkeypatch.setattr(mac_agent, "_which", lambda name: None)

    assert main(["tdx", "agent", "install"]) == 2

    assert "uv was not found on PATH" in capsys.readouterr().err
    assert not mac_agent.plist_path.exists()


def test_tdx_agent_status_and_uninstall(
    tmp_path: Path,
    mac_agent: MacLaunchAgent,
    launchctl: FakeLaunchctl,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tdx", "agent", "status"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "Agent: installed no; loaded no; no exit recorded."
    )
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    assert main(["tdx", "agent", "install"]) == 0
    capsys.readouterr()

    assert main(["tdx", "agent", "status"]) == 0
    logs = tmp_path / "applogs"
    assert capsys.readouterr().out == (
        "Agent: installed yes; loaded yes; last exit code 0.\n"
        f"Agent file: {mac_agent.plist_path}\n"
        f"Logs: {logs / 'watch.out.log'}\n"
        f"      {logs / 'watch.err.log'}\n"
        "Last session: unknown (no expiry seen after a tdx login yet).\n"
    )

    assert main(["tdx", "agent", "uninstall"]) == 0
    assert capsys.readouterr().out == (
        "Removed the start-at-login agent; its tdx watch has stopped.\n"
    )
    assert not mac_agent.plist_path.exists()
    assert not launchctl.loaded

    assert main(["tdx", "agent", "uninstall"]) == 0
    assert capsys.readouterr().out == "No start-at-login agent is installed.\n"


# --- shift summaries and mentions --------------------------------------------------------

MENTION_ADDRESS = "mf123@example.edu"


def _summary_config(tmp_path: Path, body: str) -> None:
    """Write the config file the isolated settings point at (OPS_SUITE_CONFIG)."""
    (tmp_path / "absent-config.toml").write_text(body, encoding="utf-8")


def test_tdx_watch_posts_a_due_summary_instead_of_the_alert_card(
    tmp_path: Path, rows_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _summary_config(
        tmp_path,
        "[tdx.summary]\n"
        "enabled = true\n"
        'destination = "personal"\n'
        'mention = "people"\n'
        f'mention_people = ["{MENTION_ADDRESS}"]\n'
        "[tdx.summary.times]\n"
        'fri = ["14:00"]\n',
    )
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    post = FakePost()
    rows_env.setattr(watch_module, "post_card", post)
    _audit_env(rows_env)
    real_watch = watch_module.watch
    friday = datetime(2026, 10, 16, 14, 5, tzinfo=TZ)

    def stop(seconds: float) -> None:
        raise KeyboardInterrupt

    def fixed_watch(
        settings, *, clock, sleep, run_cycle, notify, destination="personal", shadow=None
    ):
        assert shadow is None  # the default source runs no shadow step
        return real_watch(
            settings,
            clock=lambda: friday,
            sleep=stop,
            run_cycle=run_cycle,
            notify=notify,
            destination=destination,
        )

    rows_env.setattr(cli, "watch", fixed_watch)

    assert main(["tdx", "watch"]) == 0

    captured = capsys.readouterr()
    assert "14:05 5 unclaimed, 2 feeds, 1 finding; Teams: summary posted (1 open, 1 new)." in (
        captured.out
    )
    assert len(post.cards) == 1
    card = post.cards[0]
    assert card["body"][0]["text"] == "TDX queue at 2:00 PM: 1 ticket for you (1 new)"
    assert card["msteams"]["entities"][0]["mentioned"]["id"] == MENTION_ADDRESS
    records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
    summary = next(r for r in records if r["message"] == "tdx summary")
    assert (summary["outcome"], summary["open"], summary["mention"]) == ("sent", 1, "people")
    for text in (captured.out, captured.err, _log_text(tmp_path)):
        assert MENTION_ADDRESS not in text and "mf123" not in text
    _assert_private_text_absent(tmp_path, captured.out, captured.err)


def test_teams_test_mention_needs_a_mention_setting(capsys: pytest.CaptureFixture[str]) -> None:
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")

    assert main(["teams", "test", "--to", "desk", "--mention"]) == 2

    assert 'tdx.summary.mention is "none"' in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config", "expected", "entity_id"),
    [
        ('mention = "everyone"\n', "with a mention (everyone)", "everyone"),
        (
            f'mention = "people"\nmention_people = ["{MENTION_ADDRESS}"]\n',
            "with a mention (1 person)",
            MENTION_ADDRESS,
        ),
    ],
)
def test_teams_test_mention_sends_a_synthetic_card_with_the_configured_mention(
    config: str,
    expected: str,
    entity_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _summary_config(tmp_path, "[tdx.summary]\n" + config)
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    post = FakePost()
    monkeypatch.setattr(cli, "post_card", post)

    # Synthetic: no teams.desk.approved needed.
    assert main(["teams", "test", "--to", "desk", "--mention"]) == 0

    out = capsys.readouterr().out
    assert out == f"Test card sent to the desk chat {expected}; check in Teams who was notified.\n"
    card = post.cards[0]
    assert card["body"][0]["text"] == "ops-suite test card (synthetic)"
    assert card["msteams"]["entities"][0]["mentioned"]["id"] == entity_id
    assert MENTION_ADDRESS not in out + _log_text(tmp_path)


def test_a_summary_time_outside_desk_hours_is_a_config_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _summary_config(
        tmp_path, '[tdx.summary]\nenabled = true\n[tdx.summary.times]\nsun = ["18:00"]\n'
    )

    assert main(["teams", "test"]) == 2

    assert capsys.readouterr().err == (
        "ops-suite: invalid settings (Value error, tdx.summary.times: 18:00 on sun is outside "
        "desk hours (desk hours 12:00-17:00))\n"
    )


# --- unattended Windows and the session lifetime -----------------------------------------

WIN_UV = r"C:\Users\Desk Owner\.local\bin\uv.exe"
WIN_REPO = PureWindowsPath(r"C:\Users\Desk Owner\Documents\tdx-queue-auditor")


class FakeTasklist:
    def __init__(self) -> None:
        self.running = False

    def __call__(self, args) -> subprocess.CompletedProcess[str]:
        found = '"cmd.exe","4242","Console","1","4,000 K"\r\n'
        none = "INFO: No tasks are running which match the specified criteria.\r\n"
        return subprocess.CompletedProcess(list(args), 0, found if self.running else none, "")


@pytest.fixture
def tasklist() -> FakeTasklist:
    return FakeTasklist()


@pytest.fixture
def win_agent(
    tmp_path: Path, tasklist: FakeTasklist, monkeypatch: pytest.MonkeyPatch
) -> WindowsStartup:
    """Makes the CLI see Windows, with a fake tasklist and a Startup folder in tmp_path. The
    repo and uv are Windows paths on any OS; the Startup and log folders are real."""
    agent = WindowsStartup(
        repo=WIN_REPO,
        log_dir=tmp_path / "applogs",
        startup_dir=tmp_path / "Startup",
        runner=tasklist,
        which=lambda name: WIN_UV,
    )
    monkeypatch.setattr(cli, "for_platform", lambda **kwargs: agent)
    return agent


def _write_sessions(tmp_path: Path, *sessions: tuple[datetime, datetime | None]) -> Path:
    path = tmp_path / "appdata" / "tdx" / "session-log.json"
    log = SessionLog(path)
    for signed_in_at, expired_seen_at in sessions:
        log.record_sign_in(signed_in_at)
        if expired_seen_at is not None:
            log.record_expiry(expired_seen_at)
    return path


def test_tdx_agent_install_on_windows_writes_the_startup_script(
    tmp_path: Path, win_agent: WindowsStartup, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])

    assert main(["tdx", "agent", "install"]) == 0

    script = win_agent.plist_path.read_text(encoding="utf-8")
    assert f'"{WIN_UV}" "run" "ops-suite" "tdx" "watch" >>' in script
    assert f'cd /d "{WIN_REPO}"' in script
    logs = tmp_path / "applogs"
    assert capsys.readouterr().out == (
        "Installed: tdx watch starts at every sign-in, in a minimised window.\n"
        f"Startup file: {win_agent.plist_path}\n"
        f"Logs: {logs / 'watch.out.log'}\n"
        f"      {logs / 'watch.err.log'}\n"
        f"{cli.AGENT_WINDOWS_START}\n"
        f"{cli.AGENT_WINDOWS_REMINDER}\n"
    )


def test_tdx_agent_install_on_windows_passes_to_desk(
    win_agent: WindowsStartup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPS_SUITE_TEAMS__DESK__APPROVED", "true")
    cli.set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")

    assert main(["tdx", "agent", "install", "--to", "desk"]) == 0

    assert '"watch" "--to" "desk" >>' in win_agent.plist_path.read_text(encoding="utf-8")
    assert "Cards go to the desk chat." in capsys.readouterr().out


def test_tdx_agent_status_and_uninstall_on_windows(
    tmp_path: Path,
    win_agent: WindowsStartup,
    tasklist: FakeTasklist,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    assert main(["tdx", "agent", "install"]) == 0
    capsys.readouterr()
    tasklist.running = True
    (tmp_path / "applogs" / "watch.out.log").write_text("tdx watch started\n", encoding="utf-8")
    _write_sessions(
        tmp_path,
        (datetime(2026, 10, 16, 7, 0, tzinfo=TZ), datetime(2026, 10, 16, 18, 48, tzinfo=TZ)),
        (datetime(2026, 10, 19, 7, 12, tzinfo=TZ), datetime(2026, 10, 19, 19, 42, tzinfo=TZ)),
    )

    assert main(["tdx", "agent", "status"]) == 0
    logs = tmp_path / "applogs"
    assert capsys.readouterr().out == (
        "Agent: installed yes; tdx watch window running yes.\n"
        f"Startup file: {win_agent.plist_path}\n"
        f"Logs: {logs / 'watch.out.log'}\n"
        f"      {logs / 'watch.err.log'}\n"
        "Last session: signed in Mon 07:12, expiry seen Mon 19:42 (12.5 h). "
        "Median of the last 2: 12.2 h.\n"
    )

    assert main(["tdx", "agent", "uninstall"]) == 0
    assert capsys.readouterr().out == f"{cli.AGENT_WINDOWS_REMOVED}\n"
    assert not win_agent.plist_path.exists()
    tasklist.running = False
    assert main(["tdx", "agent", "status"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "Agent: installed no; tdx watch window running not found."
    )


def test_tdx_agent_status_on_windows_warns_when_the_loop_wrote_no_log(
    tmp_path: Path,
    win_agent: WindowsStartup,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A case seen on a real PC: installed, but no watch.out.log, because cmd could not
    # append to a log in a missing folder (a missing folder has no log either).
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    assert main(["tdx", "agent", "install"]) == 0
    assert not (tmp_path / "applogs" / "watch.out.log").exists()
    capsys.readouterr()

    assert main(["tdx", "agent", "status"]) == 0

    lines = capsys.readouterr().out.splitlines()
    warning = (
        "The start-at-login loop has not written its log yet. Sign out and back in, or run the "
        f"script once by hand: {win_agent.plist_path}."
    )
    assert lines[4:-1] == [warning]
    assert lines[-1].startswith("Last session:")


def test_tdx_agent_status_on_windows_does_not_warn_when_nothing_is_installed(
    win_agent: WindowsStartup,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tdx", "agent", "status"]) == 0

    assert "has not written its log" not in capsys.readouterr().out


def test_tdx_login_warns_when_the_windows_loop_is_running(
    tdx_env: pytest.MonkeyPatch,
    win_agent: WindowsStartup,
    tasklist: FakeTasklist,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasklist.running = True
    tdx_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([TDX_BASE]))

    assert main(["tdx", "login"]) == 0

    assert capsys.readouterr().out.splitlines()[0] == LOADED_WARNING


def test_tdx_login_records_the_sign_in_time_only(
    tmp_path: Path, tdx_env: pytest.MonkeyPatch
) -> None:
    tdx_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([TDX_BASE]))

    assert main(["tdx", "login"]) == 0

    data = json.loads((tmp_path / "appdata" / "tdx" / "session-log.json").read_text("utf-8"))
    assert list(data) == ["schema", "sessions"]
    assert len(data["sessions"]) == 1
    session = data["sessions"][0]
    assert list(session) == ["signed_in_at", "expired_seen_at"]
    assert session["expired_seen_at"] is None
    signed_in_at = datetime.fromisoformat(session["signed_in_at"])
    assert abs((datetime.now(TZ) - signed_in_at).total_seconds()) < 60


def test_tdx_login_that_does_not_finish_records_nothing(
    tmp_path: Path, tdx_env: pytest.MonkeyPatch
) -> None:
    tdx_env.setattr(cli, "TdxBrowser", FakeTdxBrowser(["https://login.example.edu/"]))

    assert main(["tdx", "login"]) == 3

    assert not (tmp_path / "appdata" / "tdx" / "session-log.json").exists()


@pytest.mark.parametrize("command", ["rows", "audit"])
def test_an_expired_session_in_rows_or_audit_closes_the_session(
    command: str, tmp_path: Path, rows_env: pytest.MonkeyPatch
) -> None:
    signed_in_at = datetime.now(TZ) - timedelta(hours=9)
    path = _write_sessions(tmp_path, (signed_in_at, None))
    rows_env.setattr(cli, "TdxBrowser", FakeTdxBrowser([]))
    rows_env.setattr(cli, "read_dashboard", FakeRead(NotSignedInError()))
    _audit_env(rows_env, NotSignedInError())

    assert main(["tdx", command]) == 3

    hours = SessionLog(path).sessions[-1].hours
    assert hours is not None and 8.9 < hours < 9.1
    records = [json.loads(line) for line in _log_text(tmp_path).splitlines()]
    expired = next(r for r in records if r["message"] == "tdx session expired")
    assert expired["lasted_hours"] == 9.0


def test_tdx_session_prints_the_lifetime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tdx", "session"]) == 0
    assert capsys.readouterr().out == (
        "Last session: unknown (no expiry seen after a tdx login yet).\n"
    )

    _write_sessions(
        tmp_path,
        (datetime(2026, 10, 19, 7, 12, tzinfo=TZ), datetime(2026, 10, 19, 19, 42, tzinfo=TZ)),
        (datetime(2026, 10, 20, 7, 5, tzinfo=TZ), None),
    )
    assert main(["tdx", "session"]) == 0
    assert capsys.readouterr().out == (
        "Current session: signed in Tue 07:05, no expiry seen.\n"
        "Last session: signed in Mon 07:12, expiry seen Mon 19:42 (12.5 h). "
        "Median: unknown until 2 sessions have ended.\n"
    )


def test_tdx_watch_keeps_the_computer_awake_while_it_runs(
    rows_env: pytest.MonkeyPatch,
) -> None:
    cli.set_webhook_url(WEBHOOK, [".logic.azure.com"])
    events: list[str] = []

    @contextmanager
    def fake_keep_awake():
        events.append("awake")
        yield True
        events.append("released")

    def fake_watch(settings, **kwargs) -> int:
        events.append("watch")
        return 0

    rows_env.setattr(cli, "keep_awake", fake_keep_awake)
    rows_env.setattr(cli, "watch", fake_watch)

    assert main(["tdx", "watch"]) == 0
    assert events == ["awake", "watch", "released"]


# --- the Microsoft Store Python --------------------------------------------------------------

STORE_EXECUTABLE = (
    r"C:\Users\owner\AppData\Local\Microsoft\WindowsApps"
    r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
)
STORE_REFUSED = (
    "ops-suite: This Python is the Microsoft Store build, which hides the data folder from other "
    'programs. Rebuild the environment: see USAGE, "Windows: use uv\'s Python".\n'
)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Store Python is on Windows")


@pytest.fixture
def store_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", STORE_EXECUTABLE)


@windows_only
@pytest.mark.parametrize(
    "command",
    [
        ["tdx", "login"],
        ["tdx", "set-dashboard"],
        ["tdx", "rows"],
        ["tdx", "audit"],
        ["tdx", "watch"],
        ["tdx", "agent", "install"],
        ["tdx", "capture", "--name", "dashboard"],
    ],
)
def test_live_commands_refuse_to_run_on_the_store_python(
    command: list[str],
    tmp_path: Path,
    store_python: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(command) == 2

    captured = capsys.readouterr()
    assert captured.err == STORE_REFUSED
    assert captured.out == ""
    assert not (tmp_path / "appdata").exists() and not (tmp_path / "applogs").exists()


@windows_only
def test_tdx_demo_warns_on_the_store_python_and_runs(
    monkeypatch: pytest.MonkeyPatch, store_python: None, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "read_and_judge", lambda *a, **k: pytest.fail("no audit may start"))

    # The demo's own check runs: --send without a saved webhook.
    assert main(["tdx", "demo", "--send"]) == 2

    err = capsys.readouterr().err.splitlines()
    assert err[0] == f"ops-suite: warning: {cli.STORE_PYTHON_WARNING}"
    assert 'run "uv run ops-suite teams set-webhook" first' in err[1]


@windows_only
def test_llm_status_warns_on_the_store_python_and_runs(
    store_python: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["llm", "status"]) == 0

    captured = capsys.readouterr()
    assert captured.err == f"ops-suite: warning: {cli.STORE_PYTHON_WARNING}\n"
    assert captured.out.startswith("LLM: enabled no;")


def test_no_store_warning_on_a_regular_python(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["llm", "status"]) == 0

    assert capsys.readouterr().err == ""
