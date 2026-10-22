import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.common.config import Settings
from ops_suite.common.exitcodes import (
    EXIT_BROWSER,
    EXIT_CONFIG,
    EXIT_NOT_SIGNED_IN,
    EXIT_STRUCTURE,
)
from ops_suite.common.teams import TeamsPostError, set_webhook_url
from ops_suite.tdx import browser as browser_module
from ops_suite.tdx import watch as watch_module
from ops_suite.tdx.alerts import AlertState, urgent_event, urgent_heading
from ops_suite.tdx.browser import PROFILE_IN_USE_MESSAGE, BrowserStartError, TdxBrowser
from ops_suite.tdx.dashboard import (
    DashboardRead,
    DashboardStructureError,
    NotSignedInError,
    WidgetRead,
)
from ops_suite.tdx.event_form import EventForm
from ops_suite.tdx.feed_types import TicketFeed
from ops_suite.tdx.followup import FollowUpOutcome
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import AuditResult, EventStatus, Finding, RuleSettings, evaluate
from ops_suite.tdx.session_log import SessionLog, session_log_path
from ops_suite.tdx.tickets import FeedRead
from ops_suite.tdx.watch import (
    AuditRun,
    SendOutcome,
    busy_line,
    format_send_outcome,
    rule_settings,
    send_alerts,
    sign_in_notice,
    watch,
    webhook_notifier,
)

NY = ZoneInfo("America/New_York")
WEBHOOK = "https://prod-01.westus.logic.azure.com/workflows/abc/invoke?sig=SIGSECRET42"
TITLE = "Laptop will not connect to campus Wi-Fi"
REQUESTOR = "Jordan Sampleton"


def at(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


RUN_TIME = at(10, 16, 14, 5)


# --- synthetic audit runs ----------------------------------------------------------------


def make_row(ticket_id: int) -> TicketRow:
    return TicketRow(
        widget="unclaimed",
        ticket_id=ticket_id,
        title=TITLE,
        requestor=REQUESTOR,
        resp_group="IT Service Desk",
        created=None,
        modified=None,
        prim_resp=None,
        status=None,
        detail_url=f"https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID={ticket_id}",
    )


def make_run(
    findings: list[Finding],
    *,
    ids: tuple[int, ...] = (101, 102, 103),
    now: datetime = RUN_TIME,
    failed: tuple[int, ...] = (),
    hit_page_limit: bool = False,
) -> AuditRun:
    rows = tuple(make_row(i) for i in ids)
    dashboard = DashboardRead(
        widgets={
            "unclaimed": WidgetRead("unclaimed", rows, 1, (), hit_page_limit),
            "claimed": WidgetRead("claimed", (), 1, (), False),
        },
        blocked=(),
    )
    feeds = FeedRead(feeds={}, failed=failed, deferred=(), blocked=(), allowed_non_get=())
    counts = {"rows_unclaimed": len(rows)}
    return AuditRun(dashboard, feeds, AuditResult(tuple(findings), counts), rows, now)


def waiting(ticket_id: int, age: int = 73) -> Finding:
    return Finding(ticket_id, "client_waiting", "high", "requestor_last", age)


class Poster:
    def __init__(self, *results: int | Exception) -> None:
        self.results = list(results)
        self.cards: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, url: str, card: dict, *, timeout: float) -> int:
        self.cards.append(card)
        self.urls.append(url)
        result = self.results.pop(0) if self.results else 202
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def settings() -> Settings:
    return Settings(tdx={"base_url": "https://tdx.example.edu/TDNext/"})


# --- the follow-up step -------------------------------------------------------------------


class NoClient:
    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("no LLM client may be built")


@pytest.mark.parametrize(("enabled", "use_llm"), [(False, True), (True, False)])
def test_follow_up_is_off_without_building_a_client(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, use_llm: bool
) -> None:
    monkeypatch.setattr(watch_module, "LlmClient", NoClient)
    settings = Settings(llm={"enabled": enabled, "allow_institutional_data": True})
    run = make_run([waiting(101), waiting(102)])

    result, outcome, today = watch_module.follow_up(
        settings, run.result, run.rows, run.feeds, RuleSettings(), RUN_TIME, use_llm=use_llm
    )

    assert result is run.result
    assert (outcome.state, outcome.counts["candidates"], dict(outcome.verdicts)) == ("off", 2, {})
    assert today is None


def test_follow_up_runs_as_institutional_and_protects_failed_and_deferred_feeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_classify(result, rows, feeds, desk_names, **options):
        seen.update(options, desk_names=list(desk_names))
        return FollowUpOutcome({101: "client_will_follow_up"}, {"candidates": 1}, "ran")

    class Client:
        def __init__(self, settings) -> None:
            pass

        def used_today(self) -> str:
            return "today"

    monkeypatch.setattr(watch_module, "LlmClient", Client)
    monkeypatch.setattr(watch_module, "classify_follow_ups", fake_classify)
    settings = Settings(llm={"enabled": True})  # the gate is the client's job
    run = make_run([waiting(101)], failed=(102,))
    feeds = FeedRead(feeds={}, failed=(102,), deferred=(103,), blocked=(), allowed_non_get=())
    rules = RuleSettings(extra_staff_names=("Morgan Fixture",))

    result, outcome, today = watch_module.follow_up(
        settings, run.result, run.rows, feeds, rules, RUN_TIME, use_llm=True
    )

    assert seen["data_class"] == "institutional"
    assert seen["protect"] == [102, 103]
    assert seen["desk_names"] == ["Morgan Fixture"]
    assert seen["max_input_chars"] == settings.llm.max_input_chars
    assert [(f.kind, f.reason) for f in result.findings] == [
        ("follow_up_with_client", "client_follow_up")
    ]
    assert outcome.state == "ran" and today == "today"


# --- send_alerts -------------------------------------------------------------------------


def test_send_without_a_webhook_touches_nothing(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "alerts-state.json"
    outcome = send_alerts(make_run([waiting(101)]), settings, path, None)
    assert outcome == SendOutcome("no_webhook")
    assert not path.exists()


def test_send_records_only_after_a_successful_post(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "alerts-state.json"
    poster = Poster(TeamsPostError(400, "http_400"), 202)
    monkeypatch.setattr(watch_module, "post_card", poster)
    run = make_run([waiting(101), waiting(102)])

    failed = send_alerts(run, settings, path, WEBHOOK)
    assert failed == SendOutcome("failed", 2, 0, 0, "http_400")
    assert len(AlertState(path)) == 0

    sent = send_alerts(run, settings, path, WEBHOOK)
    assert sent == SendOutcome("sent", 2, 0, 0)
    assert AlertState(path).keys() == [(101, "client_waiting"), (102, "client_waiting")]
    assert poster.cards[1]["body"][0]["text"] == "TDX queue: 2 new, 0 reminders"

    again = send_alerts(make_run([waiting(101), waiting(102)]), settings, path, WEBHOOK)
    assert again == SendOutcome("nothing_new")
    assert len(poster.cards) == 2


def test_send_saves_cleared_records_even_when_the_post_fails(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "alerts-state.json"
    monkeypatch.setattr(watch_module, "post_card", Poster(202, TeamsPostError(None, "timeout")))
    send_alerts(make_run([waiting(101)]), settings, path, WEBHOOK)

    later = make_run([waiting(102)], now=at(10, 16, 15, 0))
    assert send_alerts(later, settings, path, WEBHOOK).status == "failed"

    assert AlertState(path).keys() == []  # 101 cleared; 102 not recorded


def test_send_keeps_records_for_failed_feeds_and_a_truncated_queue(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "alerts-state.json"
    monkeypatch.setattr(watch_module, "post_card", Poster())
    send_alerts(make_run([waiting(101), waiting(102)]), settings, path, WEBHOOK)

    # 101's feed failed; 102 is gone, but a widget hit its page limit.
    run = make_run([], ids=(101,), failed=(101,), hit_page_limit=True, now=at(10, 16, 15))
    assert send_alerts(run, settings, path, WEBHOOK).status == "nothing_new"
    assert len(AlertState(path)) == 2


def test_rule_settings_carries_skip_desk_requestors() -> None:
    on = Settings(tdx={"base_url": "https://tdx.example.edu/TDNext/"})
    off = Settings(
        tdx={
            "base_url": "https://tdx.example.edu/TDNext/",
            "rules": {"skip_desk_requestors": False},
        }
    )
    assert rule_settings(on.tdx).skip_desk_requestors is True
    assert rule_settings(off.tdx).skip_desk_requestors is False


def test_rule_settings_carries_near_event_days() -> None:
    base = "https://tdx.example.edu/TDNext/"
    default = Settings(tdx={"base_url": base})
    changed = Settings(tdx={"base_url": base, "events": {"near_event_days": 1}})

    assert rule_settings(default.tdx).near_event_days == 2
    assert rule_settings(changed.tdx).near_event_days == 1


def test_a_ticket_a_desk_member_opened_clears_its_alert_record(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "alerts-state.json"
    monkeypatch.setattr(watch_module, "post_card", Poster())
    rows = (make_row(101),)
    feeds = {101: TicketFeed(101, ())}

    def judged(rules: RuleSettings) -> AuditRun:
        result = evaluate(rows, feeds, RUN_TIME, rules, NY)
        return replace(make_run([], ids=(101,)), result=result)

    first = judged(rule_settings(settings.tdx))
    assert [f.reason for f in first.result.findings] == ["no_reply_yet"]
    send_alerts(first, settings, path, WEBHOOK)
    assert AlertState(path).keys() == [(101, "client_waiting")]

    # The requestor turns out to be desk staff (here through extra_staff_names).
    later = judged(replace(rule_settings(settings.tdx), extra_staff_names=(REQUESTOR,)))
    assert later.result.findings == ()
    assert later.result.counts["internal_requestor"] == 1
    send_alerts(later, settings, path, WEBHOOK)
    assert AlertState(path).keys() == []


def test_send_outcome_lines() -> None:
    assert (
        format_send_outcome(SendOutcome("sent", 3, 1)) == "Teams: sent 1 card (3 new, 1 reminder)."
    )
    assert format_send_outcome(SendOutcome("nothing_new")) == "Teams: nothing new."
    assert format_send_outcome(SendOutcome("failed", reason="http_400")) == (
        "Teams: not sent (http_400); will retry next run."
    )
    assert format_send_outcome(SendOutcome("no_webhook")) == (
        'Teams: not sent; run "uv run ops-suite teams set-webhook" first.'
    )


# --- watch: a fake clock -----------------------------------------------------------------


class Clock:
    """Fake time: ``sleep`` advances ``now``; ``wake`` jumps it (a Mac waking from sleep)."""

    def __init__(self, start: datetime, stop_at: datetime) -> None:
        self.now = start
        self.stop_at = stop_at
        self.sleeps: list[float] = []
        self.jumps: dict[datetime, datetime] = {}

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= 30
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        for when, to in list(self.jumps.items()):
            if self.now >= when:
                self.now = max(self.now, to)
                del self.jumps[when]
        if self.now >= self.stop_at:
            raise KeyboardInterrupt


class Cycles:
    """Fake run_cycle: records when it ran; answers from a script of outcomes or errors."""

    def __init__(self, clock: Clock, *script: SendOutcome | BaseException) -> None:
        self.clock = clock
        self.script = list(script)
        self.times: list[datetime] = []

    def __call__(self) -> tuple[AuditRun, SendOutcome]:
        self.times.append(self.clock.now)
        result = self.script.pop(0) if self.script else SendOutcome("nothing_new")
        if isinstance(result, BaseException):
            raise result
        run = make_run([waiting(101), waiting(102)])
        return run, result


@pytest.fixture
def webhook() -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])


def run_watch(settings: Settings, clock: Clock, cycles: Cycles) -> tuple[int, list[str]]:
    notices: list[str] = []
    code = watch(settings, clock=clock, sleep=clock.sleep, run_cycle=cycles, notify=notices.append)
    return code, notices


@pytest.mark.usefixtures("webhook")
def test_no_cycle_outside_desk_hours(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 22, 30), stop_at=at(10, 17, 8, 1))  # Friday night
    cycles = Cycles(clock)

    code, _ = run_watch(settings, clock, cycles)

    assert code == 0
    assert cycles.times == [at(10, 17, 8, 0)]  # first run at Saturday's open
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        "Watching the TDX queue every 30 min during desk hours (America/New_York). Ctrl+C stops."
    )
    assert out[1] == "Outside desk hours; next run Sat 08:00."
    assert out[-1] == "Stopped."


@pytest.mark.usefixtures("webhook")
def test_cycles_run_30_minutes_apart(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 15, 30))
    cycles = Cycles(clock, SendOutcome("sent", 2, 0), SendOutcome("nothing_new"))

    run_watch(settings, clock, cycles)

    assert cycles.times == [at(10, 16, 14, 5), at(10, 16, 14, 35), at(10, 16, 15, 5)]
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == (
        "14:05 3 unclaimed, 0 feeds, 2 findings; Teams: sent 1 card (2 new, 0 reminders). "
        "Next run 14:35."
    )
    assert lines[2] == "14:35 3 unclaimed, 0 feeds, 2 findings; Teams: nothing new. Next run 15:05."
    assert max(clock.sleeps) <= 30


@pytest.mark.usefixtures("webhook")
def test_a_cycle_near_closing_sleeps_until_the_next_open(settings: Settings) -> None:
    clock = Clock(at(10, 16, 21, 30), stop_at=at(10, 17, 8, 1))
    cycles = Cycles(clock)

    run_watch(settings, clock, cycles)

    # 21:30 + 30 min is 22:00, closing time, so the next run is Saturday 08:00.
    assert cycles.times == [at(10, 16, 21, 30), at(10, 17, 8, 0)]


@pytest.mark.usefixtures("webhook")
def test_a_mac_that_slept_through_a_run_runs_at_once(settings: Settings) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 16, 30))
    clock.jumps[at(10, 16, 14, 10)] = at(10, 16, 16, 1)  # asleep from 14:10 to 16:01
    cycles = Cycles(clock)

    run_watch(settings, clock, cycles)

    assert cycles.times == [at(10, 16, 14, 5), at(10, 16, 16, 1)]


NOTICE = sign_in_notice(10)
EXPIRED_LINE = 'TDX sign-in expired; run "uv run ops-suite tdx login". Checking again every 10 min.'
BUSY_LINE = 'TDX browser profile in use (a "tdx login" window?); checking again every 10 min.'


def in_use() -> BrowserStartError:
    return BrowserStartError(PROFILE_IN_USE_MESSAGE, reason="profile_in_use")


def signed_out(count: int) -> list[NotSignedInError]:
    return [NotSignedInError() for _ in range(count)]


def test_the_sign_in_notice_is_runnable_and_takes_the_minutes_from_the_setting() -> None:
    assert NOTICE == (
        'TDX sign-in expired. On the Mac run "uv run ops-suite tdx login"; the auditor keeps '
        "running and picks up again within 10 minutes."
    )
    assert sign_in_notice(15).endswith("within 15 minutes.")


@pytest.mark.usefixtures("webhook")
def test_the_first_expiry_after_a_sign_in_is_logged_with_its_lifetime(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    log_path = session_log_path(settings)
    SessionLog(log_path).record_sign_in(at(10, 16, 7, 12))
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 15, 0))
    cycles = Cycles(clock, SendOutcome("nothing_new"), *signed_out(5))

    with caplog.at_level("INFO", logger="ops_suite"):
        run_watch(settings, clock, cycles)

    sessions = SessionLog(log_path).sessions
    assert len(sessions) == 1
    # The first failed check (14:35) closes it; the later ones change nothing.
    assert sessions[0].expired_seen_at == at(10, 16, 14, 35)
    expired = [r for r in caplog.records if r.getMessage() == "tdx session expired"]
    assert len(expired) == 1
    assert expired[0].lasted_hours == 7.4  # 07:12 to 14:35


@pytest.mark.usefixtures("webhook")
def test_an_expired_session_sends_one_notice_and_keeps_checking_quietly(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 15, 20))
    cycles = Cycles(clock, SendOutcome("nothing_new"), *signed_out(10))

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0  # stopped by Ctrl+C
    assert code != EXIT_NOT_SIGNED_IN
    assert notices == [NOTICE]
    assert cycles.times == [
        at(10, 16, 14, 5),
        at(10, 16, 14, 35),
        at(10, 16, 14, 45),
        at(10, 16, 14, 55),
        at(10, 16, 15, 5),
        at(10, 16, 15, 15),
    ]
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"14:35 {EXPIRED_LINE}"]
    assert captured.out.splitlines()[2:] == ["Stopped."]  # the retries print nothing
    assert TITLE not in captured.out + captured.err


@pytest.mark.usefixtures("webhook")
def test_signed_out_checks_follow_desk_hours_and_a_new_day_sends_one_more_notice(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 21, 40), stop_at=at(10, 17, 8, 25))  # Friday, closes at 22:00
    cycles = Cycles(clock, *signed_out(10))

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0
    # Every 10 min inside desk hours, none overnight, then every 10 min from Saturday 08:00.
    assert cycles.times == [
        at(10, 16, 21, 40),
        at(10, 16, 21, 50),
        at(10, 17, 8, 0),
        at(10, 17, 8, 10),
        at(10, 17, 8, 20),
    ]
    assert notices == [NOTICE, NOTICE]  # Friday's, and one more on Saturday
    assert capsys.readouterr().err.splitlines() == [
        f"21:40 {EXPIRED_LINE}",
        f"08:00 {EXPIRED_LINE}",
    ]


@pytest.mark.usefixtures("webhook")
def test_profile_in_use_while_signed_out_is_not_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/", "watch": {"failures_before_stop": 1}}
    )
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 14, 40))
    cycles = Cycles(clock, NotSignedInError(), in_use(), in_use(), SendOutcome("nothing_new"))

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0
    assert notices == [NOTICE]
    assert cycles.times == [
        at(10, 16, 14, 5),
        at(10, 16, 14, 15),
        at(10, 16, 14, 25),
        at(10, 16, 14, 35),
    ]
    captured = capsys.readouterr()
    assert "cycle failed" not in captured.out + captured.err
    out = captured.out.splitlines()
    assert out[1:3] == [f"14:15 {BUSY_LINE}", "14:35 Signed in again; resuming."]


@pytest.mark.usefixtures("webhook")
def test_other_errors_while_signed_out_still_count_as_failures() -> None:
    settings = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/", "watch": {"failures_before_stop": 2}}
    )
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    error = DashboardStructureError("the widget grid was not found")
    cycles = Cycles(clock, NotSignedInError(), error, error)

    code, notices = run_watch(settings, clock, cycles)

    assert code == EXIT_STRUCTURE
    assert cycles.times == [at(10, 16, 14, 5), at(10, 16, 14, 15), at(10, 16, 14, 25)]
    assert notices == [
        NOTICE,
        "The TDX auditor stopped after 2 failed runs (DashboardStructureError). Check the Mac.",
    ]


@pytest.mark.usefixtures("webhook")
def test_signing_in_again_resumes_the_30_minute_interval_and_resets_failures(
    settings: Settings, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 16, 46))
    error = BrowserStartError("Executable missing")
    cycles = Cycles(
        clock,
        error,
        error,  # two failures of three
        NotSignedInError(),
        SendOutcome("nothing_new"),
        error,
        error,  # two more: the count was reset, so no stop
        SendOutcome("nothing_new"),
    )

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0
    assert notices == [NOTICE]
    assert cycles.times == [
        at(10, 16, 14, 5),
        at(10, 16, 14, 35),
        at(10, 16, 15, 5),
        at(10, 16, 15, 15),  # 10 min after the expiry
        at(10, 16, 15, 45),  # back to 30 min
        at(10, 16, 16, 15),
        at(10, 16, 16, 45),
    ]
    out = capsys.readouterr().out.splitlines()
    resumed = out.index("15:15 Signed in again; resuming.")
    assert out[resumed + 1] == (
        "15:15 3 unclaimed, 0 feeds, 2 findings; Teams: nothing new. Next run 15:45."
    )
    events = [record.getMessage() for record in caplog.records]
    assert events.count("tdx watch signed out") == 1
    assert events.count("tdx watch signed in again") == 1


@pytest.mark.usefixtures("webhook")
def test_expiry_never_triggers_the_failure_stop(settings: Settings) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 16, 0))
    error = BrowserStartError("Executable missing")
    cycles = Cycles(clock, error, error, *signed_out(20))  # limit 3; the expiry would be 3rd

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0
    assert notices == [NOTICE]
    assert len(cycles.times) > 3


@pytest.mark.usefixtures("webhook")
def test_three_failures_in_a_row_stop_with_one_notice(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    error = DashboardStructureError("the widget grid was not found")
    cycles = Cycles(clock, error, error, error)

    code, notices = run_watch(settings, clock, cycles)

    assert code == EXIT_STRUCTURE
    message = (
        "The TDX auditor stopped after 3 failed runs (DashboardStructureError). Check the Mac."
    )
    assert notices == [message]
    captured = capsys.readouterr()
    assert captured.out.splitlines()[1:] == [
        "14:05 cycle failed (DashboardStructureError); trying again at 14:35.",
        "14:35 cycle failed (DashboardStructureError); trying again at 15:05.",
    ]
    assert captured.err.strip() == f"ops-suite: {message}"
    assert "widget grid" not in captured.out + captured.err


def test_the_busy_line_takes_the_minutes_from_the_setting() -> None:
    assert busy_line(10) == BUSY_LINE
    assert busy_line(15).endswith("checking again every 15 min.")


@pytest.mark.usefixtures("webhook")
def test_profile_in_use_while_signed_in_is_not_a_failure(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    settings = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/", "watch": {"failures_before_stop": 1}}
    )
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 15, 6))
    cycles = Cycles(clock, in_use(), in_use(), in_use(), SendOutcome("nothing_new"))

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0  # stopped by Ctrl+C, not by failures
    assert notices == []
    assert cycles.times == [
        at(10, 16, 14, 5),
        at(10, 16, 14, 15),  # quiet checks every signed_out_retry_minutes
        at(10, 16, 14, 25),
        at(10, 16, 14, 35),
        at(10, 16, 15, 5),  # back to 30 min after the success
    ]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines()[1:3] == [
        f"14:05 {BUSY_LINE}",  # once for the whole spell
        "14:35 3 unclaimed, 0 feeds, 2 findings; Teams: nothing new. Next run 15:05.",
    ]
    events = [record.getMessage() for record in caplog.records]
    assert events.count("tdx watch profile busy") == 1
    assert "tdx watch cycle failed" not in events


@pytest.mark.usefixtures("webhook")
def test_the_sunday_sequence_never_stops_the_loop(capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-10-18: busy at 13:00, expired at 14:17, busy at 16:02, then a success. Even with a
    # limit of one failure, none of these counts.
    settings = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/", "watch": {"failures_before_stop": 1}}
    )
    clock = Clock(at(10, 18, 13, 0), stop_at=at(10, 18, 16, 13))
    clock.jumps[at(10, 18, 13, 1)] = at(10, 18, 14, 17)
    clock.jumps[at(10, 18, 14, 18)] = at(10, 18, 16, 2)
    cycles = Cycles(clock, in_use(), NotSignedInError(), in_use(), SendOutcome("nothing_new"))

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0
    assert notices == [NOTICE]
    assert cycles.times == [
        at(10, 18, 13, 0),
        at(10, 18, 14, 17),
        at(10, 18, 16, 2),
        at(10, 18, 16, 12),
    ]
    captured = capsys.readouterr()
    assert "stopped after" not in captured.out + captured.err
    assert "cycle failed" not in captured.out
    assert captured.err.splitlines() == [f"14:17 {EXPIRED_LINE}"]
    assert captured.out.splitlines()[1:4] == [
        f"13:00 {BUSY_LINE}",
        f"16:02 {BUSY_LINE}",  # a new spell: the expiry came between
        "16:12 Signed in again; resuming.",
    ]


@pytest.mark.usefixtures("webhook")
def test_three_launch_timeouts_stop_and_name_the_reason(
    settings: Settings, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    timeout = BrowserStartError("Timeout 60000ms exceeded.", reason="launch_timeout")
    cycles = Cycles(clock, timeout, timeout, timeout)

    code, notices = run_watch(settings, clock, cycles)

    assert code == EXIT_BROWSER
    message = (
        "The TDX auditor stopped after 3 failed runs (BrowserStartError: launch_timeout). "
        "Check the Mac."
    )
    assert notices == [message]
    captured = capsys.readouterr()
    assert captured.out.splitlines()[1:] == [
        "14:05 cycle failed (BrowserStartError: launch_timeout); trying again at 14:35.",
        "14:35 cycle failed (BrowserStartError: launch_timeout); trying again at 15:05.",
    ]
    assert captured.err.strip() == f"ops-suite: {message}"
    failed = [r for r in caplog.records if r.getMessage() == "tdx watch cycle failed"]
    assert [(r.error, r.reason, r.failures) for r in failed] == [
        ("BrowserStartError", "launch_timeout", 1),
        ("BrowserStartError", "launch_timeout", 2),
        ("BrowserStartError", "launch_timeout", 3),
    ]


@pytest.mark.usefixtures("webhook")
def test_an_expired_cycle_resets_the_failure_count(settings: Settings) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 16, 0))
    error = BrowserStartError("Browser closed.", reason="other")
    cycles = Cycles(clock, error, error, NotSignedInError(), error)  # limit 3

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0  # stopped by Ctrl+C: the expiry reset the count
    assert notices == [NOTICE]
    assert len(cycles.times) > 4


@pytest.mark.usefixtures("webhook")
def test_a_profile_path_in_playwrights_message_never_reaches_output_or_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    home = "/Users/jordan.sampleton"
    message = (
        "BrowserType.launch_persistent_context: Executable doesn't exist at "
        f"{home}/Library/Caches/ms-playwright/chromium-1140/chrome-mac/Chromium.app"
    )

    class NoChromium:
        def start(self) -> "NoChromium":
            raise browser_module.PlaywrightError(message)

    monkeypatch.setattr(browser_module, "sync_playwright", NoChromium)
    settings = Settings(tdx={"base_url": "https://tdx.example.edu/TDNext/"})
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))

    def run_cycle() -> tuple[AuditRun, SendOutcome]:
        with TdxBrowser(tmp_path / "profile", settings.tdx.base_url, headless=True):
            raise AssertionError("the browser may not start")

    notices: list[str] = []
    code = watch(
        settings, clock=clock, sleep=clock.sleep, run_cycle=run_cycle, notify=notices.append
    )

    assert code == EXIT_BROWSER
    captured = capsys.readouterr()
    assert "(BrowserStartError: not_installed)" in captured.out
    logged = " ".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)
    for text in (captured.out, captured.err, " ".join(notices), logged):
        assert "jordan" not in text
        # The fake home, not the literal "/Users/": a macOS runner's own paths start with it.
        assert home not in text
        assert "Executable" not in text


def test_the_launch_timeout_setting_reaches_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Recorder:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.update(kwargs)

        def __enter__(self) -> "Recorder":
            raise BrowserStartError("stop here")

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(watch_module, "TdxBrowser", Recorder)
    settings = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/", "browser_launch_timeout_seconds": 90}
    )

    with pytest.raises(BrowserStartError):
        watch_module.read_and_judge(settings, headless=True, progress=None)

    assert seen["launch_timeout_seconds"] == 90


@pytest.mark.usefixtures("webhook")
def test_two_failures_then_a_success_keep_going(settings: Settings) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 16, 40))
    error = BrowserStartError("Executable missing")
    cycles = Cycles(clock, error, error, SendOutcome("nothing_new"), error, error)

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0  # stopped by Ctrl+C, not by failures
    assert notices == []
    assert len(cycles.times) == 6


@pytest.mark.usefixtures("webhook")
def test_a_teams_failure_is_not_a_cycle_failure(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 16, 0))
    failed = SendOutcome("failed", 2, 0, 0, "http_502")
    cycles = Cycles(clock, failed, failed, failed, failed)

    code, notices = run_watch(settings, clock, cycles)

    assert code == 0
    assert notices == []
    assert len(cycles.times) == 4
    assert "Teams: not sent (http_502); will retry next run." in capsys.readouterr().out


def test_no_webhook_means_exit_config_before_any_cycle(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    cycles = Cycles(clock)

    code, _ = run_watch(settings, clock, cycles)

    assert code == EXIT_CONFIG
    assert cycles.times == []
    assert 'run "uv run ops-suite teams set-webhook" first' in capsys.readouterr().err


@pytest.mark.usefixtures("webhook")
def test_ctrl_c_during_a_cycle_returns_0(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    cycles = Cycles(clock, KeyboardInterrupt())

    code, _ = run_watch(settings, clock, cycles)

    assert code == 0
    assert capsys.readouterr().out.splitlines()[-1] == "Stopped."


def test_the_notifier_posts_a_plain_card_and_only_logs_failures(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster(202, TeamsPostError(None, "timeout"))
    monkeypatch.setattr(watch_module, "post_card", poster)
    notify = webhook_notifier(settings)

    notify(NOTICE)  # no webhook saved: nothing posted
    assert poster.cards == []

    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    notify(NOTICE)
    notify(NOTICE)  # the post times out: no exception

    assert len(poster.cards) == 2
    assert [block["text"] for block in poster.cards[0]["body"]] == [
        "ops-suite: TDX auditor",
        NOTICE,
    ]
    assert poster.cards[0]["fallbackText"] == "ops-suite: TDX auditor notice"


# --- the desk destination -------------------------------------------------------------------

DESK_WEBHOOK = "https://prod-02.westus.logic.azure.com/workflows/desk/invoke?sig=SIGSECRET43"


@pytest.fixture
def approved() -> Settings:
    return Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/"}, teams={"desk": {"approved": True}}
    )


def test_each_destination_has_its_own_state_file(settings: Settings) -> None:
    personal = watch_module.alert_state_path(settings)
    desk = watch_module.alert_state_path(settings, "desk")
    assert personal == watch_module.alert_state_path(settings, "personal")
    assert (personal.name, desk.name) == ("alerts-state.json", "alerts-state-desk.json")
    assert personal.parent == desk.parent


def test_desk_is_refused_without_approval_and_touches_no_state(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    path = tmp_path / "alerts-state-desk.json"

    outcome = send_alerts(make_run([waiting(101)]), settings, path, DESK_WEBHOOK, "desk")

    assert outcome == SendOutcome("not_approved", destination="desk")
    assert format_send_outcome(outcome) == ("Teams (desk): not sent; teams.desk.approved is false.")
    assert poster.cards == []
    assert not path.exists()


def test_approved_desk_sends_a_card_without_titles(
    approved: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    path = tmp_path / "alerts-state-desk.json"

    outcome = send_alerts(make_run([waiting(101)]), approved, path, DESK_WEBHOOK, "desk")

    assert outcome == SendOutcome("sent", 1, 0, 0, "", "desk")
    assert format_send_outcome(outcome) == "Teams (desk): sent 1 card (1 new, 0 reminders)."
    card = json.dumps(poster.cards[0])
    assert TITLE not in card and REQUESTOR not in card
    assert "#101" in card
    assert AlertState(path).keys() == [(101, "client_waiting")]


def test_desk_titles_follow_the_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    titled = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/"},
        teams={"desk": {"approved": True, "show_titles": True}},
    )
    send_alerts(make_run([waiting(101)]), titled, tmp_path / "s.json", DESK_WEBHOOK, "desk")
    assert TITLE in json.dumps(poster.cards[0])


def test_desk_and_personal_states_do_not_touch_each_other(
    approved: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    personal = watch_module.alert_state_path(approved)
    desk = watch_module.alert_state_path(approved, "desk")
    run = make_run([waiting(101), waiting(102)])

    assert send_alerts(run, approved, personal, WEBHOOK).status == "sent"
    personal_text = personal.read_text(encoding="utf-8")
    assert not desk.exists()

    # The desk chat starts fresh: the same findings are new there.
    first = send_alerts(run, approved, desk, DESK_WEBHOOK, "desk")
    assert (first.status, first.new) == ("sent", 2)
    assert personal.read_text(encoding="utf-8") == personal_text

    # And the other way round: a later personal run leaves the desk state alone.
    desk_text = desk.read_text(encoding="utf-8")
    later = make_run([waiting(101), waiting(102), waiting(103)], now=at(10, 16, 15))
    assert send_alerts(later, approved, personal, WEBHOOK).new == 1
    assert desk.read_text(encoding="utf-8") == desk_text


def test_the_personal_card_keeps_its_titles(
    approved: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    send_alerts(make_run([waiting(101)]), approved, tmp_path / "s.json", WEBHOOK)
    assert TITLE in json.dumps(poster.cards[0])


def test_desk_outcome_lines_name_the_destination() -> None:
    assert format_send_outcome(SendOutcome("nothing_new", destination="desk")) == (
        "Teams (desk): nothing new."
    )
    assert format_send_outcome(SendOutcome("failed", reason="http_400", destination="desk")) == (
        "Teams (desk): not sent (http_400); will retry next run."
    )
    assert format_send_outcome(SendOutcome("no_webhook", destination="desk")) == (
        'Teams (desk): not sent; run "uv run ops-suite teams set-webhook --to desk" first.'
    )


def run_desk_watch(settings: Settings, clock: Clock, cycles: Cycles) -> tuple[int, list[str]]:
    notices: list[str] = []
    code = watch(
        settings,
        clock=clock,
        sleep=clock.sleep,
        run_cycle=cycles,
        notify=notices.append,
        destination="desk",
    )
    return code, notices


def test_watch_to_desk_is_refused_at_start_without_approval(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    cycles = Cycles(clock)

    code, _ = run_desk_watch(settings, clock, cycles)

    assert code == EXIT_CONFIG
    assert cycles.times == []
    err = capsys.readouterr().err
    assert "teams.desk.approved" in err and len(err.splitlines()) == 1


def test_watch_to_desk_needs_the_desk_webhook(
    approved: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])  # personal only
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 20))
    cycles = Cycles(clock)

    code, _ = run_desk_watch(approved, clock, cycles)

    assert code == EXIT_CONFIG
    assert cycles.times == []
    assert 'run "uv run ops-suite teams set-webhook --to desk" first' in capsys.readouterr().err


def test_watch_to_desk_without_a_personal_webhook_warns_once_and_runs(
    approved: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 15, 30))
    cycles = Cycles(clock, SendOutcome("sent", 2, 0, destination="desk"))

    code, _ = run_desk_watch(approved, clock, cycles)

    assert code == 0
    assert len(cycles.times) == 3
    out, err = capsys.readouterr()
    assert err.count("sign-in notices need a personal webhook") == 1
    assert "Cards go to the desk chat." in out
    assert "Teams (desk): sent 1 card (2 new, 0 reminders)." in out


def test_watch_to_desk_with_both_webhooks_prints_no_warning(
    approved: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    clock = Clock(at(10, 16, 14, 5), stop_at=at(10, 16, 15))

    code, _ = run_desk_watch(approved, clock, Cycles(clock))

    assert code == 0
    assert "personal webhook" not in capsys.readouterr().err


def test_notices_go_to_the_personal_webhook_only(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def poster(url: str, card: dict, *, timeout: float) -> int:
        urls.append(url)
        return 202

    monkeypatch.setattr(watch_module, "post_card", poster)
    notify = webhook_notifier(settings)

    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    notify(NOTICE)  # only a desk webhook: the notice goes nowhere
    assert urls == []

    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    notify(NOTICE)
    assert urls == [WEBHOOK]


# --- shift summaries ---------------------------------------------------------------------


def summary_settings(**summary: object) -> Settings:
    return Settings(
        tdx={
            "base_url": "https://tdx.example.edu/TDNext/",
            "summary": {"enabled": True, **summary},
        },
        teams={"desk": {"approved": True}},
    )


class SummaryCycles:
    """A fake run_cycle that posts for real (through a fake poster and ``send_cycle``, as
    ``tdx watch --to desk`` does): the summary when asked, otherwise the alert card wherever
    the settings send it. ``runs`` builds each cycle's audit run from the time."""

    def __init__(
        self,
        clock: Clock,
        settings: Settings,
        *script: BaseException,
        runs: Callable[[datetime], AuditRun] | None = None,
    ) -> None:
        self.clock = clock
        self.settings = settings
        self.script = list(script)
        self.runs = runs or (lambda now: make_run([waiting(101), waiting(102)], now=now))
        self.times: list[datetime] = []
        self.requests: list[watch_module.SummaryRequest | None] = []
        self.state_path = watch_module.alert_state_path(settings, "desk")

    def __call__(self, summary=None) -> tuple[AuditRun, SendOutcome]:
        self.times.append(self.clock.now)
        self.requests.append(summary)
        if self.script:
            raise self.script.pop(0)
        run = self.runs(self.clock.now)
        return run, watch_module.send_cycle(run, self.settings, "desk", summary)


@pytest.fixture
def both_webhooks() -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")


def heading(card: dict) -> str:
    return card["body"][0]["text"]


@pytest.mark.usefixtures("both_webhooks")
def test_a_due_summary_replaces_the_alert_card_and_records_its_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings(desk_alerts=True)
    clock = Clock(at(10, 19, 7, 0), stop_at=at(10, 19, 8, 5))  # Monday, before opening
    cycles = SummaryCycles(clock, settings)

    code, notices = run_desk_watch(settings, clock, cycles)

    assert code == 0 and notices == []
    assert cycles.times == [at(10, 19, 7, 30), at(10, 19, 8, 0)]
    assert [r.slot if r else None for r in cycles.requests] == [at(10, 19, 7, 30), None]
    assert len(poster.cards) == 1  # one card for the shift, not two
    assert heading(poster.cards[0]) == "TDX queue at 07:30: 2 open (2 new)"
    assert TITLE not in json.dumps(poster.cards[0])  # desk default: no titles
    out = capsys.readouterr().out
    assert "Shift summaries go to the desk chat; next Mon 07:30." in out
    assert (
        "07:30 3 unclaimed, 0 feeds, 2 findings; Teams (desk): summary posted (2 open, 2 new)."
        in out
    )
    assert "08:00 3 unclaimed, 0 feeds, 2 findings; Teams (desk): nothing new." in out
    assert sorted(AlertState(cycles.state_path).keys()) == [
        (101, "client_waiting"),
        (102, "client_waiting"),
    ]
    summary_state = json.loads(
        watch_module.summary_state_path(settings, "desk").read_text(encoding="utf-8")
    )
    assert summary_state == {
        "schema": 3,
        "last_slot": "2026-10-19T07:30:00-04:00",
        "last_posted": "2026-10-19T07:30:00-04:00",
        "open": ["101:client_waiting", "102:client_waiting"],
        "known": ["101:client_waiting", "102:client_waiting"],
    }


@pytest.mark.usefixtures("both_webhooks")
def test_the_loop_wakes_at_a_summary_slot_between_interval_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watch_module, "post_card", Poster())
    settings = summary_settings()
    clock = Clock(at(10, 19, 11, 45), stop_at=at(10, 19, 12, 31))
    cycles = SummaryCycles(clock, settings)

    run_desk_watch(settings, clock, cycles)

    assert cycles.times == [at(10, 19, 11, 45), at(10, 19, 12, 0), at(10, 19, 12, 30)]
    assert [r is not None for r in cycles.requests] == [False, True, False]


@pytest.mark.usefixtures("both_webhooks")
def test_a_restart_in_the_same_slot_does_not_post_again(monkeypatch: pytest.MonkeyPatch) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings(desk_alerts=True)
    first = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 7, 40))
    run_desk_watch(settings, first, SummaryCycles(first, settings))
    assert len(poster.cards) == 1

    again = Clock(at(10, 19, 7, 45), stop_at=at(10, 19, 7, 50))
    cycles = SummaryCycles(again, settings)
    run_desk_watch(settings, again, cycles)

    assert cycles.requests == [None]
    assert len(poster.cards) == 1  # the alert cycle found nothing new


@pytest.mark.usefixtures("both_webhooks")
def test_a_late_summary_within_grace_is_marked_late_with_one_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 8, 5))
    cycles = SummaryCycles(clock, settings, *signed_out(3))  # 07:30, 07:40, 07:50

    _, notices = run_desk_watch(settings, clock, cycles)

    assert cycles.times[-1] == at(10, 19, 8, 0)
    assert notices == [NOTICE, "07:30 summary posted late at 08:00"]
    assert heading(poster.cards[0]) == "TDX queue at 08:00 (late: due 07:30): 2 open (2 new)"


@pytest.mark.usefixtures("both_webhooks")
def test_signed_out_past_the_grace_window_skips_the_slot_and_names_the_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings(desk_alerts=True)
    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 9, 25))
    cycles = SummaryCycles(clock, settings, *signed_out(11))  # 07:30 ... 09:10

    _, notices = run_desk_watch(settings, clock, cycles)

    assert notices == [NOTICE, "07:30 summary not posted: TDX sign-in expired"]
    # Back at 09:20, past grace: an alert card, not a summary.
    assert cycles.requests[-1] is None and cycles.times[-1] == at(10, 19, 9, 20)
    assert [heading(card) for card in poster.cards] == ["TDX queue: 2 new, 0 reminders"]
    assert "09:00 07:30 summary not posted: TDX sign-in expired." in capsys.readouterr().out
    assert not watch_module.summary_state_path(settings, "desk").exists()


@pytest.mark.usefixtures("both_webhooks")
def test_a_failed_summary_post_is_retried_and_its_reason_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster(TeamsPostError(400, "http_400"), TeamsPostError(400, "http_400"))
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings(grace_minutes=45, desk_alerts=True)
    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 8, 35))
    cycles = SummaryCycles(clock, settings)

    _, notices = run_desk_watch(settings, clock, cycles)

    assert cycles.times == [at(10, 19, 7, 30), at(10, 19, 8, 0), at(10, 19, 8, 30)]
    assert [r is not None for r in cycles.requests] == [True, True, False]
    assert notices == ["07:30 summary not posted: Teams post failed (http_400)"]
    assert "Teams (desk): summary not posted (http_400); will retry next run." in (
        capsys.readouterr().out
    )
    # Nothing was recorded, so the 08:30 alert card carries both findings as new.
    assert heading(poster.cards[-1]) == "TDX queue: 2 new, 0 reminders"


@pytest.mark.usefixtures("both_webhooks")
def test_an_empty_queue_still_posts_queue_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    state_path = watch_module.alert_state_path(settings, "desk")
    run = make_run([], now=at(10, 19, 12, 1))
    request = watch_module.SummaryRequest(at(10, 19, 12))

    outcome = watch_module.send_summary(run, settings, state_path, DESK_WEBHOOK, "desk", request)

    assert format_send_outcome(outcome) == "Teams (desk): summary posted (0 open, 0 new)."
    assert heading(poster.cards[0]) == "Queue clear at 12:00: nothing needs attention."


def test_a_desk_summary_without_approval_is_refused_and_touches_no_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    unapproved = Settings(
        tdx={"base_url": "https://tdx.example.edu/TDNext/", "summary": {"enabled": True}}
    )
    path = tmp_path / "alerts-state-desk.json"
    request = watch_module.SummaryRequest(at(10, 19, 7, 30))

    outcome = watch_module.send_summary(
        make_run([waiting(101)]), unapproved, path, DESK_WEBHOOK, "desk", request
    )

    assert outcome.status == "not_approved" and poster.cards == [] and not path.exists()


@pytest.mark.usefixtures("both_webhooks")
def test_summaries_for_another_chat_are_not_sent_by_this_watch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = summary_settings()  # destination desk
    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 7, 40))
    cycles = Cycles(clock)  # takes no summary argument: a summary request would fail

    code, _ = run_watch(settings, clock, cycles)

    assert code == 0 and cycles.times == [at(10, 19, 7, 30)]
    assert "this watch posts to the personal chat, so it sends none." in capsys.readouterr().out


# --- ready to close, duplicates and events this week -------------------------------------

SUMMARY_RUN_TIME = at(10, 19, 7, 31)  # a Monday


def ready(ticket_id: int, age: int = 96) -> Finding:
    return Finding(ticket_id, "ready_to_close", "low", "desk_replied_last", age)


def summary_request() -> watch_module.SummaryRequest:
    return watch_module.SummaryRequest(at(10, 19, 7, 30))


@pytest.mark.parametrize("alert", [False, True])
def test_ready_to_close_alerts_only_when_switched_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alert: bool
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = Settings(
        tdx={
            "base_url": "https://tdx.example.edu/TDNext/",
            "alerts": {"alert_ready_to_close": alert},
        }
    )
    state_path = tmp_path / "alerts-state.json"

    outcome = send_alerts(make_run([ready(101)]), settings, state_path, WEBHOOK)

    if alert:
        assert outcome.status == "sent" and len(poster.cards) == 1
        assert "Ready to close (1)" in json.dumps(poster.cards[0])
        assert AlertState(state_path).keys() == [(101, "ready_to_close")]
    else:
        assert outcome.status == "nothing_new" and poster.cards == []
        assert AlertState(state_path).keys() == []


def test_the_summary_lists_ready_to_close_without_recording_or_marking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    state_path = tmp_path / "alerts-state-desk.json"
    run = make_run([waiting(101), ready(102)], now=SUMMARY_RUN_TIME)

    outcome = watch_module.send_summary(
        run, settings, state_path, DESK_WEBHOOK, "desk", summary_request()
    )

    assert outcome.status == "sent" and outcome.open == 2 and outcome.new == 1
    lines = [block["text"] for block in poster.cards[0]["body"]]
    assert lines[0] == "TDX queue at 07:30: 2 open (1 new)"
    assert lines[-2] == "Ready to close (1)"
    assert "(new)" not in lines[-1] and "Desk replied 4 days ago" in lines[-1]
    assert AlertState(state_path).keys() == [(101, "client_waiting")]


def test_the_summary_draft_adds_events_and_duplicates_from_the_run(
    tmp_path: Path, settings: Settings
) -> None:
    def titled(ticket_id: int, title: str, requestor: str, days_ago: float) -> TicketRow:
        return replace(
            make_row(ticket_id),
            title=title,
            requestor=requestor,
            created=SUMMARY_RUN_TIME - timedelta(days=days_ago),
        )

    rows = (
        titled(101, "Printer in lab will not connect", REQUESTOR, 4),
        titled(102, "Lab printer will not connect", "sampleton, jordan", 2),
        titled(103, "EVENT [10/21 2:00 PM - 4:00 PM Room 210] Alumni panel", "Reese X", 9),
        titled(104, "CCI Event Request Form", "Charlie Y", 3),
    )
    form = EventForm(
        dates=(at(10, 23, 15),), recurring=False, wants_av=True, wants_room=False,
        marketing_only=False, unknown_options=0, has_time=True, end_time=None, space="other",
    )  # fmt: skip
    run = make_run([waiting(101), waiting(102)], now=SUMMARY_RUN_TIME)
    states = {
        103: EventStatus("waiting_on_requester", at(10, 18, 8)),
        104: EventStatus("in_progress"),
    }
    run = replace(
        run,
        rows=rows,
        feeds=replace(run.feeds, forms={104: form}),
        result=replace(run.result, event_states=states),
    )

    draft = watch_module.draft_summary(
        run, settings, AlertState(tmp_path / "s.json"), "desk", summary_request()
    )

    lines = [block["text"] for block in draft.card["body"]]
    assert lines[2] == "Events this week (2)"
    assert lines[3].endswith(
        ") · Wed 10/21 14:00–16:00 · — · A/V on form: — · waiting on requester since Sun 08:00"
    )
    assert lines[4].endswith(
        ") · Fri 10/23 15:00 · Other · A/V on form: yes · in progress (requester answered)"
    )
    (duplicate,) = [line for line in lines if "duplicate" in line]
    assert duplicate.startswith("[#102](") and "(possible duplicate of #101)" in duplicate
    text = json.dumps(draft.card)
    for secret in (*(row.title for row in rows), REQUESTOR, "Reese X", "Charlie Y", "sampleton"):
        assert secret not in text


# --- the desk gets summaries only: full, then deltas ---------------------------------------

EVENT_TITLE = "EVENT [10/21 2:00 PM - 4:00 PM Room 210] Alumni panel"


def day_run(findings: list[Finding], now: datetime, ids: tuple[int, ...] = (101, 102, 103, 104)):
    run = make_run(findings, ids=ids, now=now)
    rows = tuple(
        replace(row, title=EVENT_TITLE) if row.ticket_id == 103 else row for row in run.rows
    )
    return replace(run, rows=rows)


def desk_cards(poster: Poster) -> list[dict]:
    return [
        card for card, url in zip(poster.cards, poster.urls, strict=True) if url == DESK_WEBHOOK
    ]


def personal_cards(poster: Poster) -> list[dict]:
    return [card for card, url in zip(poster.cards, poster.urls, strict=True) if url == WEBHOOK]


def lines_of(card: dict) -> list[str]:
    return [block["text"] for block in card["body"]]


def test_alert_cards_go_where_the_summary_settings_say() -> None:
    def where(to: str = "desk", **summary: object) -> str | None:
        return watch_module.alert_destination(summary_settings(**summary), to)

    assert where() == "personal"
    assert where(personal_alerts=False) is None
    assert where(desk_alerts=True) == "desk"
    assert where(enabled=False) == "desk"  # summaries off: as before
    assert where(destination="personal") == "desk"  # summaries go to the personal chat
    assert where("personal") == "personal"


@pytest.mark.usefixtures("both_webhooks")
def test_with_summaries_off_a_desk_cycle_alerts_the_desk_as_before(
    approved: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)

    outcome = watch_module.send_cycle(make_run([waiting(101)]), approved, "desk")

    assert outcome == SendOutcome("sent", 1, 0, 0, "", "desk")
    assert poster.urls == [DESK_WEBHOOK]
    assert AlertState(watch_module.alert_state_path(approved, "desk")).keys() == [
        (101, "client_waiting")
    ]


@pytest.mark.usefixtures("both_webhooks")
def test_a_desk_runner_with_summaries_sends_its_alert_cards_to_personal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    clock = Clock(at(10, 19, 9, 30), stop_at=at(10, 19, 10, 35))  # 07:30 is past its grace
    cycles = SummaryCycles(clock, settings)

    run_desk_watch(settings, clock, cycles)

    assert cycles.requests == [None, None, None]
    assert poster.urls == [WEBHOOK]  # one personal card, then nothing new; none to the desk
    assert TITLE in json.dumps(poster.cards[0])  # the personal chat shows titles
    assert AlertState(watch_module.alert_state_path(settings)).keys() == [
        (101, "client_waiting"),
        (102, "client_waiting"),
    ]
    assert not watch_module.alert_state_path(settings, "desk").exists()
    out = capsys.readouterr().out
    assert "Alert cards go to the personal chat; the desk chat gets summaries only." in out
    assert "09:30 3 unclaimed, 0 feeds, 2 findings; Teams: sent 1 card (2 new, 0 reminders)." in out


def test_without_a_personal_webhook_a_summaries_only_desk_runner_warns_and_sends_no_cards(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")  # desk only
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    clock = Clock(at(10, 19, 9, 30), stop_at=at(10, 19, 10, 5))
    cycles = SummaryCycles(clock, settings)

    code, _ = run_desk_watch(settings, clock, cycles)

    assert code == 0 and len(cycles.times) == 2 and poster.cards == []
    out, err = capsys.readouterr()
    assert err.count("no personal webhook is saved, so no alert cards are sent") == 1
    assert "Teams: no alert card (no personal webhook)." in out


@pytest.mark.usefixtures("both_webhooks")
def test_with_personal_alerts_off_a_desk_runner_sends_no_alert_cards(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings(personal_alerts=False)
    clock = Clock(at(10, 19, 9, 30), stop_at=at(10, 19, 9, 35))

    run_desk_watch(settings, clock, SummaryCycles(clock, settings))

    assert poster.cards == []
    out = capsys.readouterr().out
    assert "No alert cards; the desk chat gets summaries only." in out
    assert "Teams (desk): no alert card (summaries only)." in out


@pytest.mark.usefixtures("both_webhooks")
def test_a_day_is_one_full_summary_then_deltas_skipped_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings(mention="everyone")

    def runs(now: datetime) -> AuditRun:
        if now < at(10, 19, 10):
            return day_run([waiting(101), waiting(102), ready(104)], now)
        # By 10:00 102 was answered (gone from a judged ticket) and 103 needs a reply.
        return day_run([waiting(101), waiting(103), ready(104)], now)

    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 17, 5))
    cycles = SummaryCycles(clock, settings, runs=runs)

    code, notices = run_desk_watch(settings, clock, cycles)

    assert code == 0 and notices == []  # a skipped delta is not a miss
    summaries = [(r.slot, r.kind) for r in cycles.requests if r is not None]
    assert summaries == [
        (at(10, 19, 7, 30), "full"),
        (at(10, 19, 12), "delta"),
        (at(10, 19, 17), "delta"),
    ]
    full, noon = desk_cards(poster)  # nothing posted at 17:00
    full_lines = lines_of(full)
    assert full_lines[0] == "TDX queue at 07:30: 3 open (2 new)"
    assert full_lines[2] == "Events this week (1)"
    assert "Ready to close (1)" in full_lines
    assert full["msteams"]["entities"]  # the mention, on the full post only
    noon_lines = lines_of(noon)
    assert noon_lines[0] == "Since 07:30: 1 new \u00b7 1 cleared \u00b7 3 open"
    assert noon_lines[2] == "Client waiting (1)"
    assert noon_lines[3].startswith("[#103](") and "(new)" in noon_lines[3]
    assert len(noon_lines) == 4  # no events, no ready to close, no row for the cleared one
    assert "entities" not in noon["msteams"]
    assert TITLE not in json.dumps(desk_cards(poster)) and EVENT_TITLE not in json.dumps(full)
    # The per-cycle alert cards went to the personal chat: 08:00 (101, 102), 10:00 (103).
    assert [heading(card) for card in personal_cards(poster)] == [
        "TDX queue: 2 new, 0 reminders",
        "TDX queue: 1 new, 0 reminders",
    ]
    out = capsys.readouterr().out
    assert "Teams (desk): delta posted (1 new, 1 cleared, 3 open)." in out
    assert "17:00 4 unclaimed, 0 feeds, 3 findings; Teams (desk): 17:00 summary skipped " in out
    state = watch_module.SummaryState(watch_module.summary_state_path(settings, "desk"))
    assert state.last_slot == at(10, 19, 17) and state.last_posted == at(10, 19, 12)
    assert state.open == {(101, "client_waiting"), (103, "client_waiting")}


@pytest.mark.usefixtures("both_webhooks")
def test_a_missed_first_slot_makes_the_next_slot_full(monkeypatch: pytest.MonkeyPatch) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    # Yesterday's last summary was posted; today 07:30 is missed (signed out past grace).
    watch_module.SummaryState(watch_module.summary_state_path(settings, "desk")).record(
        at(10, 18, 17), posted=True, open_keys={(101, "client_waiting")}
    )
    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 12, 5))
    cycles = SummaryCycles(clock, settings, *signed_out(11))  # 07:30 ... 09:10

    _, notices = run_desk_watch(settings, clock, cycles)

    assert notices[-1] == "07:30 summary not posted: TDX sign-in expired"
    assert {r.kind for r in cycles.requests if r is not None and r.slot == at(10, 19, 7, 30)} == {
        "full"
    }  # the signed-out tries at 07:30 were full too
    (request,) = [r for r in cycles.requests if r is not None and r.slot == at(10, 19, 12)]
    assert request.kind == "full"
    (card,) = desk_cards(poster)
    assert heading(card) == "TDX queue at 12:00: 2 open (1 new since yesterday)"


@pytest.mark.usefixtures("both_webhooks")
def test_a_restart_mid_day_keeps_the_open_set_and_posts_nothing_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    morning = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 7, 40))
    run_desk_watch(settings, morning, SummaryCycles(morning, settings))

    # Restarted before noon; 103 is new by then.
    def runs(now: datetime) -> AuditRun:
        return day_run([waiting(101), waiting(102), waiting(103)], now)

    noon = Clock(at(10, 19, 11, 50), stop_at=at(10, 19, 12, 5))
    noon_cycles = SummaryCycles(noon, settings, runs=runs)
    run_desk_watch(settings, noon, noon_cycles)
    again = Clock(at(10, 19, 12, 10), stop_at=at(10, 19, 12, 15))
    again_cycles = SummaryCycles(again, settings, runs=runs)
    run_desk_watch(settings, again, again_cycles)

    assert [r.kind if r else None for r in noon_cycles.requests] == [None, "delta"]
    assert again_cycles.requests == [None]
    assert [heading(card) for card in desk_cards(poster)] == [
        "TDX queue at 07:30: 2 open (2 new)",
        "Since 07:30: 1 new \u00b7 0 cleared \u00b7 3 open",
    ]


def test_summary_outcome_lines_for_deltas_and_skips() -> None:
    noon = at(10, 19, 12)
    delta = SendOutcome("sent", 1, destination="desk", summary=True, open=3, delta=True, cleared=1)
    skipped = SendOutcome("no_change", destination="desk", summary=True, slot=noon)
    assert format_send_outcome(delta) == "Teams (desk): delta posted (1 new, 1 cleared, 3 open)."
    assert format_send_outcome(skipped) == "Teams (desk): 12:00 summary skipped (no change)."


# --- urgent event findings go to the desk at once ------------------------------------------


def soon(ticket_id: int, hours: int = 6) -> Finding:
    """Waiting on the requester, the event ``hours`` away: urgent within near_event_days."""
    return Finding(ticket_id, "event_details_pending", "high", "details_pending_near", 20, hours)


def urgent_cards(poster: Poster) -> list[dict]:
    return [card for card in desk_cards(poster) if heading(card).startswith("Event soon")]


@pytest.mark.usefixtures("both_webhooks")
def test_an_urgent_event_between_slots_posts_one_desk_card_and_the_summaries_follow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()

    def runs(now: datetime) -> AuditRun:
        if now < at(10, 19, 9):
            return day_run([waiting(101)], now)
        return day_run([waiting(101), soon(103)], now)  # the event is 6 hours away

    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 20, 7, 35))
    cycles = SummaryCycles(clock, settings, runs=runs)

    code, notices = run_desk_watch(settings, clock, cycles)

    assert code == 0 and notices == []
    full, urgent, next_full = desk_cards(poster)  # 12:00 and 17:00 had nothing new
    assert heading(full) == "TDX queue at 07:30: 1 open (1 new)"
    lines = lines_of(urgent)
    assert lines[0] == "Event soon: 1 needs action"
    assert lines[2] == "Event details pending (1)"
    assert lines[3].startswith("[#103](") and "follow up now" in lines[3]
    assert "(new)" not in lines[3] and len(lines) == 4
    assert urgent["fallbackText"] == "ops-suite: Event soon: 1 needs action"
    assert "entities" not in urgent["msteams"]  # never a mention
    assert TITLE not in json.dumps(urgent) and EVENT_TITLE not in json.dumps(urgent)
    # The next day's full summary still lists it as open, not as new.
    assert heading(next_full) == "TDX queue at 07:30: 2 open (0 new since yesterday)"
    (row,) = [
        line for line in lines_of(next_full) if line.startswith("[#103](") and "follow" in line
    ]
    assert "(new)" not in row and "(event soon)" not in row
    # The personal chat got its alert cards as before, the event's included.
    assert [heading(card) for card in personal_cards(poster)[:2]] == [
        "TDX queue: 1 new, 0 reminders",
        "TDX queue: 1 new, 0 reminders",
    ]
    assert "#103" in json.dumps(personal_cards(poster)[1])
    out = capsys.readouterr().out
    assert (
        "09:00 4 unclaimed, 0 feeds, 2 findings; Teams: sent 1 card (1 new, 0 reminders). "
        "Teams (desk): urgent event card (1). Next run 09:30." in out
    )
    assert out.count("urgent event card") == 1
    assert "12:00 summary skipped (no change)" in out
    desk_state = AlertState(watch_module.alert_state_path(settings, "desk"))
    assert (103, "event_details_pending") in desk_state.keys()


@pytest.mark.usefixtures("both_webhooks")
def test_an_urgent_event_posts_once_until_it_clears_and_again_when_it_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()

    def runs(now: datetime) -> AuditRun:
        # The requester's answer clears it at 10:00; a desk question at 10:30 brings it back.
        back = at(10, 19, 9) <= now < at(10, 19, 10) or now >= at(10, 19, 10, 30)
        return day_run([waiting(101), *([soon(103)] if back else [])], now)

    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 11, 35))
    run_desk_watch(settings, clock, SummaryCycles(clock, settings, runs=runs))

    assert len(urgent_cards(poster)) == 2  # 09:00 and 10:30; not 09:30, 11:00 or 11:30
    state = watch_module.SummaryState(watch_module.summary_state_path(settings, "desk"))
    assert (103, "event_details_pending") in state.known
    assert (103, "event_details_pending") in state.open  # so the 12:00 delta skips it


@pytest.mark.usefixtures("both_webhooks")
def test_an_urgent_event_in_a_summary_run_goes_into_the_delta_with_no_card_of_its_own(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()

    def runs(now: datetime) -> AuditRun:
        late = now >= at(10, 19, 12)
        return day_run([waiting(101), *([soon(103)] if late else [])], now)

    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 13, 5))
    run_desk_watch(settings, clock, SummaryCycles(clock, settings, runs=runs))

    assert urgent_cards(poster) == []
    _, noon = desk_cards(poster)
    lines = lines_of(noon)
    assert lines[0] == "Since 07:30: 1 new · 0 cleared · 2 open"
    assert lines[3].startswith("[#103](") and "(new) (event soon) · " in lines[3]
    out = capsys.readouterr().out
    assert (
        "Teams (desk): delta posted (1 new, 0 cleared, 2 open). "
        "Teams (desk): urgent events in the summary (1)." in out
    )


@pytest.mark.usefixtures("both_webhooks")
def test_an_urgent_event_at_the_first_slot_is_marked_in_the_full_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()
    clock = Clock(at(10, 19, 7, 30), stop_at=at(10, 19, 8, 35))
    cycles = SummaryCycles(
        clock, settings, runs=lambda now: day_run([waiting(101), soon(103)], now)
    )

    run_desk_watch(settings, clock, cycles)

    (full,) = desk_cards(poster)
    (row,) = [line for line in lines_of(full) if line.startswith("[#103](") and "follow" in line]
    assert "(new) (event soon)" in row
    (other,) = [line for line in lines_of(full) if line.startswith("[#101](")]
    assert "(event soon)" not in other


def far_and_other_findings() -> list[Finding]:
    return [
        waiting(101),
        Finding(102, "event_outreach_due", "normal", "outreach_due", 200, 200),
        Finding(103, "client_waiting", "normal", "event_requester_replied", 60, 100),
        Finding(104, "location_tbd_soon", "normal", "tbd_soon", 72),
        Finding(105, "event_over", "low", "event_over", 30),
        Finding(106, "event_details_pending", "normal", "details_pending", 80, 120),
    ]


def test_only_event_findings_near_their_event_are_urgent() -> None:
    near = [
        soon(1, 48),
        Finding(2, "event_outreach_due", "high", "outreach_due", 30, 30),
        Finding(3, "client_waiting", "high", "event_requester_replied", 50, 10),
        Finding(4, "location_tbd_soon", "normal", "location_missing", 20),
    ]
    assert all(urgent_event(finding, 2) for finding in near)
    assert not any(urgent_event(finding, 2) for finding in far_and_other_findings())
    assert not urgent_event(soon(1, 49), 2) and urgent_event(soon(1, 49), 3)
    assert not urgent_event(Finding(7, "client_waiting", "high", "requestor_last", 50), 2)


@pytest.mark.usefixtures("both_webhooks")
def test_far_events_and_other_findings_never_post_to_the_desk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    run = day_run(far_and_other_findings(), at(10, 19, 9), ids=(101, 102, 103, 104, 105, 106))

    outcome = watch_module.send_cycle(run, summary_settings(), "desk")

    assert outcome.urgent is None and desk_cards(poster) == []
    assert len(personal_cards(poster)) == 1


@pytest.mark.parametrize(
    "case", ["off", "desk not approved", "summaries to personal", "summaries off"]
)
@pytest.mark.usefixtures("both_webhooks")
def test_no_urgent_cards_unless_desk_summaries_are_on_approved_and_the_setting_is_true(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    poster = Poster()
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = {
        "off": lambda: summary_settings(urgent_events_to_desk=False),
        "desk not approved": lambda: Settings(
            tdx={"base_url": "https://tdx.example.edu/TDNext/", "summary": {"enabled": True}}
        ),
        "summaries to personal": lambda: summary_settings(destination="personal"),
        "summaries off": lambda: summary_settings(enabled=False),
    }[case]()

    outcome = watch_module.send_cycle(day_run([soon(103)], at(10, 19, 9)), settings, "desk")

    assert outcome.urgent is None and urgent_cards(poster) == []
    assert not watch_module.urgent_events_on(settings, "desk")
    assert not watch_module.summary_state_path(settings, "desk").exists()


def test_urgent_events_go_only_with_a_desk_watch() -> None:
    assert watch_module.urgent_events_on(summary_settings(), "desk")
    assert not watch_module.urgent_events_on(summary_settings(), "personal")


@pytest.mark.usefixtures("both_webhooks")
def test_the_personal_alert_card_is_the_same_with_urgent_events_on_or_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = day_run([waiting(101), soon(103)], at(10, 19, 9))
    personal: list[dict] = []
    for setting in (True, False):
        poster = Poster()
        monkeypatch.setattr(watch_module, "post_card", poster)
        settings = summary_settings(urgent_events_to_desk=setting)
        for path in (
            watch_module.alert_state_path(settings),
            watch_module.summary_state_path(settings, "desk"),
        ):
            path.unlink(missing_ok=True)

        watch_module.send_cycle(run, settings, "desk")

        personal.extend(personal_cards(poster))
        assert len(urgent_cards(poster)) == (1 if setting else 0)
    on, off = personal
    assert on == off


@pytest.mark.usefixtures("both_webhooks")
def test_a_failed_urgent_post_records_nothing_and_is_tried_again_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster(202, TeamsPostError(400, "http_400"))  # the personal card, then the desk's
    monkeypatch.setattr(watch_module, "post_card", poster)
    settings = summary_settings()

    first = watch_module.send_cycle(day_run([soon(103)], at(10, 19, 9)), settings, "desk")
    again = watch_module.send_cycle(day_run([soon(103)], at(10, 19, 9, 30)), settings, "desk")

    assert first.urgent == watch_module.UrgentOutcome("failed", 1, "http_400")
    assert watch_module.format_urgent(first.urgent) == (
        "Teams (desk): urgent event card not posted (http_400); will retry next run."
    )
    assert again.urgent == watch_module.UrgentOutcome("sent", 1)
    assert len(urgent_cards(poster)) == 2  # the failed try and the retry


def test_urgent_card_headings_and_lines() -> None:
    assert urgent_heading(1) == "Event soon: 1 needs action"
    assert urgent_heading(3) == "Event soon: 3 need action"
    assert watch_module.format_urgent(watch_module.UrgentOutcome("sent", 2)) == (
        "Teams (desk): urgent event card (2)."
    )
