"""The TDX Web API as the primary source: the API run makes the cards, the browser reads any
run the API cannot (with its reason code), the notices, the browser read beside the API for
comparison, and the two periods of ``tdx api compare``. Synthetic data on both sides (the demo
dataset as API answers and as browser pages); no browser, network or real time."""

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import keyring.errors
import pytest
from api_fakes import (
    API,
    APP,
    BASE,
    CLAIMED_REPORT,
    TZ,
    UNCLAIMED_REPORT,
    Answer,
    FakeApi,
    FakeClock,
    demo_browser_side,
    demo_routes,
    make_token,
    report,
    stored_tokens,
)

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common import paths
from ops_suite.common.config import Settings
from ops_suite.common.teams import set_webhook_url
from ops_suite.tdx import api_primary, shadow
from ops_suite.tdx import watch as watch_module
from ops_suite.tdx.alerts import urgent_event, urgent_heading
from ops_suite.tdx.api_client import ApiError, TdxApiClient
from ops_suite.tdx.api_primary import ApiFirst, FallbackNotices, fallback_notice
from ops_suite.tdx.api_token import TokenError
from ops_suite.tdx.browser import BrowserStartError
from ops_suite.tdx.dashboard import KINDS, DashboardRead, NotSignedInError, WidgetRead
from ops_suite.tdx.demo.dataset import demo_tickets
from ops_suite.tdx.rules import evaluate, plan_feeds
from ops_suite.tdx.session_log import session_log_path
from ops_suite.tdx.shadow import (
    BROWSER_SIGNED_OUT,
    BrowserShadow,
    ShadowLog,
    SourceRead,
    Timing,
    compare,
    fallback_record,
    from_run,
    judge_read,
    read_only_memory,
    shadow_log_path,
    shadow_record,
    skipped_record,
)
from ops_suite.tdx.tickets import FeedRead
from ops_suite.tdx.watch import (
    API_PRIMARY_ON,
    API_SIGN_IN_NOTICE,
    BROWSER_SHADOW_ON,
    AuditRun,
    SendOutcome,
    SummaryRequest,
    form_options,
    rule_settings,
    send_cycle,
    sign_in_notice,
    watch,
)

WEBHOOK = "https://prod-01.westus.logic.azure.com/workflows/abc/invoke?sig=S"
# The browser side's ticket links; the API's are on the TDX host, and only those reach a card.
PAGE_HOST = "127.0.0.1"


def api_settings(**tdx: object) -> Settings:
    values: dict[str, object] = {
        "base_url": BASE,
        "source": "api",
        "max_feeds_per_run": 200,
        "api": {
            "enabled": True,
            "app_id": APP,
            "unclaimed_report_id": UNCLAIMED_REPORT,
            "claimed_report_id": CLAIMED_REPORT,
        },
    }
    values.update(tdx)
    return Settings(tdx=values)


def browser_side_run(settings: Settings, now: datetime) -> AuditRun:
    """The demo queue as the browser path gives it, judged as a cycle judges it."""
    tdx = settings.tdx
    _, rows, feeds, forms = demo_browser_side(now, TZ, form_options(tdx))
    rules = rule_settings(tdx)
    plan = plan_feeds(rows, now, rules, TZ)
    read = {i: feeds[i] for i in plan.feed_ticket_ids}
    forms_read = {i: form for i, form in forms.items() if i in plan.form_ticket_ids}
    widgets = {
        kind: WidgetRead(kind, tuple(r for r in rows if r.widget == kind), 1, (), False)
        for kind in KINDS
    }
    feed_read = FeedRead(
        feeds=read, failed=(), deferred=(), blocked=(), allowed_non_get=(), forms=forms_read
    )
    result = evaluate(rows, read, now, rules, TZ, forms=forms_read)
    return AuditRun(DashboardRead(widgets, blocked=()), feed_read, result, tuple(rows), now)


class FakeBrowser:
    """The browser read of a run: the demo's pages, or ``error``."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, settings: Settings, progress: object) -> AuditRun:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return browser_side_run(settings, datetime.now(TZ))


class Factory:
    """API clients on the next fake API in ``apis`` (the last one repeats), or raising
    ``errors`` first."""

    def __init__(
        self, apis: list[FakeApi], clock: FakeClock, errors: list[Exception] | None = None
    ) -> None:
        self.apis = apis
        self.clock = clock
        self.errors = list(errors or [])
        self.deadlines: list[float] = []

    def __call__(self, settings: Settings, deadline: float) -> TdxApiClient:
        self.deadlines.append(deadline)
        if self.errors:
            raise self.errors.pop(0)
        api = self.apis.pop(0) if len(self.apis) > 1 else self.apis[0]
        return TdxApiClient(
            settings.tdx,
            stored_tokens(),
            opener=api,  # type: ignore[arg-type]
            clock=self.clock,
            wall=lambda: datetime.now(UTC),
            sleep=self.clock.sleep,
            deadline=deadline,
        )


def demo_api(now: datetime) -> FakeApi:
    return FakeApi(demo_routes(demo_tickets(now), now, TZ))


def reader_for(
    settings: Settings,
    apis: list[FakeApi],
    *,
    browser: FakeBrowser | None = None,
    errors: list[Exception] | None = None,
    notices: list[str] | None = None,
    clock: FakeClock | None = None,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[ApiFirst, FakeBrowser, Factory]:
    clock = clock or FakeClock()
    browser = browser or FakeBrowser()
    factory = Factory(apis, clock, errors)
    reader = ApiFirst(
        settings,
        headless=True,
        use_llm=False,
        notify=notices.append if notices is not None else None,
        client_factory=factory,
        browser=browser,
        clock=clock,
        wall=wall,
    )
    return reader, browser, factory


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every card posted to Teams; a personal webhook is saved."""
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    cards: list[dict] = []

    def post(url: str, card: dict, timeout: float) -> int:
        cards.append(card)
        return 202

    monkeypatch.setattr(watch_module, "post_card", post)
    return cards


# --- the API run --------------------------------------------------------------------------


def test_an_api_run_makes_the_cards_and_summaries_and_the_browser_is_not_read(
    posted: list[dict],
) -> None:
    settings = api_settings()
    api = demo_api(datetime.now(TZ))
    reader, browser, factory = reader_for(settings, [api])

    run = reader()

    assert (run.source, run.fallback, browser.calls) == ("api", None, 0)
    assert factory.deadlines == [1000.0 + 300]  # tdx.api.max_cycle_seconds
    assert run.api_requests == len(api.calls) > 0
    assert {method for method, _, _ in api.calls} == {"GET"}
    assert run.read_seconds is not None and run.result.findings

    outcome = send_cycle(run, settings, "personal")
    summary = send_cycle(run, settings, "personal", SummaryRequest(slot=datetime.now(TZ)))

    assert (outcome.status, summary.status, len(posted)) == ("sent", "sent", 2)
    cards = json.dumps(posted)
    assert "tdx.example.edu" in cards and PAGE_HOST not in cards  # the API's ticket links


@pytest.mark.parametrize(
    ("reason", "route"),
    [
        ("unauthorised", Answer(401)),
        ("forbidden", Answer(403)),
        ("http_500", Answer(500)),
        ("bad_json", Answer(200, raw=b"not json")),
        ("structure", report(("ID", "Title"), [])),  # no Created column
    ],
)
def test_an_api_error_or_a_structure_problem_lets_the_browser_read_the_run(
    reason: str,
    route: object,
    posted: list[dict],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = api_settings()
    now = datetime.now(TZ)
    bad = demo_api(now)
    bad.routes[f"{API}/reports/{UNCLAIMED_REPORT}"] = route
    reader, browser, _ = reader_for(settings, [bad, demo_api(now)])
    caplog.set_level(logging.INFO)

    first = reader()
    posts = send_cycle(first, settings, "personal")

    assert (first.source, first.fallback, browser.calls) == ("browser", reason, 1)
    assert posts.status == "sent" and len(posted) == 1  # once, from the browser
    assert "tdx.example.edu" not in json.dumps(posted)  # no API links on the browser's card
    (record,) = [r for r in caplog.records if r.getMessage() == "tdx watch source"]
    assert (record.source, record.fallback) == ("browser", reason)  # type: ignore[attr-defined]

    second = reader()  # the next run tries the API again

    assert (second.source, second.fallback, browser.calls) == ("api", None, 1)


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        ("needs_sign_in", TokenError("needs_sign_in")),
        ("no_token", TokenError("no_token")),
        ("timeout", ApiError("timeout")),
        ("signed_out", NotSignedInError()),
        ("keychain", keyring.errors.KeyringError()),
        ("RuntimeError", RuntimeError("anything at all")),
    ],
)
def test_a_token_or_other_failure_lets_the_browser_read_the_run(
    reason: str, error: Exception
) -> None:
    settings = api_settings()
    reader, browser, _ = reader_for(settings, [demo_api(datetime.now(TZ))], errors=[error])

    run = reader()

    assert (run.source, run.fallback, browser.calls) == ("browser", reason, 1)
    assert reader().source == "api"


def test_a_report_id_not_set_lets_the_browser_read_the_run() -> None:
    settings = api_settings(api={"enabled": True, "app_id": APP, "claimed_report_id": 12})
    reader, browser, _ = reader_for(settings, [demo_api(datetime.now(TZ))])

    run = reader()

    assert (run.source, run.fallback, browser.calls) == ("browser", "config", 1)


def test_an_api_read_past_max_cycle_seconds_lets_the_browser_read_the_run() -> None:
    settings = api_settings(api={**api_settings().tdx.api.model_dump(), "max_cycle_seconds": 60})
    clock = FakeClock()
    api = demo_api(datetime.now(TZ))
    slow_report = api.routes[f"{API}/reports/{UNCLAIMED_REPORT}"]

    def slow(request: object) -> object:
        clock.now += 61  # the first report takes longer than the whole cycle allows
        return slow_report

    api.routes[f"{API}/reports/{UNCLAIMED_REPORT}"] = slow
    reader, browser, factory = reader_for(settings, [api], clock=clock)

    run = reader()

    assert (run.source, run.fallback, browser.calls) == ("browser", "time_box", 1)
    assert factory.deadlines == [1000.0 + 60]
    assert len(api.calls) == 1  # nothing was sent after the time box


def test_the_browser_failing_too_reaches_the_loop_as_before() -> None:
    settings = api_settings()
    browser = FakeBrowser(NotSignedInError())
    reader, _, _ = reader_for(settings, [], browser=browser, errors=[TokenError("needs_sign_in")])

    with pytest.raises(NotSignedInError):
        reader()


# --- notices ------------------------------------------------------------------------------


class Wall:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def test_repeated_fallbacks_send_one_notice_a_day() -> None:
    settings = api_settings()
    notices: list[str] = []
    wall = Wall(datetime(2026, 10, 22, 9, 0, tzinfo=TZ))
    errors: list[Exception] = [ApiError("timeout"), ApiError("http_503"), ApiError("timeout")]
    reader, _, _ = reader_for(settings, [], errors=errors, notices=notices, wall=wall)

    reader()
    wall.now += timedelta(hours=2)
    reader()
    wall.now += timedelta(days=1)
    reader()

    assert (
        notices
        == [
            "Reading the queue through the browser: the TDX Web API is unavailable (timeout); "
            "each run tries the API again.",
        ]
        * 2
    )


def test_needs_sign_in_sends_only_the_token_notice_once_a_day() -> None:
    settings = api_settings()
    notices: list[str] = []
    errors = [TokenError("needs_sign_in"), TokenError("needs_sign_in"), ApiError("timeout")]
    reader, _, _ = reader_for(settings, [], errors=errors, notices=notices)

    for _ in errors:
        reader()

    assert notices[0] == API_SIGN_IN_NOTICE
    assert notices.count(API_SIGN_IN_NOTICE) == 1
    assert len(notices) == 2  # the timeout's own fallback notice, not a second token one


@pytest.mark.parametrize(
    "error", [NotSignedInError(), BrowserStartError("busy", reason="profile_in_use")]
)
def test_problems_the_browser_meets_too_send_no_fallback_notice(error: Exception) -> None:
    notices: list[str] = []
    FallbackNotices(notices.append, TZ).fell_back(error, "x", datetime.now(TZ))

    assert notices == []


def test_the_notice_names_the_step_that_helps() -> None:
    token = fallback_notice(TokenError("no_token"), "no_token")
    check = fallback_notice(ApiError("not_found"), "structure")

    assert token.endswith('(no_token); run "uv run ops-suite tdx api token --headed".')
    assert check.endswith('(structure); run "uv run ops-suite tdx api check".')


# --- the browser beside the API -------------------------------------------------------------


def api_run(settings: Settings) -> AuditRun:
    reader, _, _ = reader_for(settings, [demo_api(datetime.now(TZ))])
    return reader()


def beside(settings: Settings, calls: list[AuditRun] | None = None, error: Exception | None = None):
    """A browser read for the comparison: the demo's pages, the API run's plan."""

    def read(settings: Settings, run: AuditRun) -> tuple[SourceRead, int]:
        if calls is not None:
            calls.append(run)
        if error is not None:
            raise error
        _, rows, feeds, forms = demo_browser_side(run.now, TZ, form_options(settings.tdx))
        plan = plan_feeds(run.rows, run.now, rule_settings(settings.tdx), TZ)
        read = {i: feeds[i] for i in plan.feed_ticket_ids}
        forms_read = {i: form for i, form in forms.items() if i in plan.form_ticket_ids}
        memory = read_only_memory(settings)
        findings = judge_read(settings, run, rows, read, forms_read, memory)
        return SourceRead(rows, read, findings), 4

    return read


def _records(settings: Settings) -> list[dict]:
    return ShadowLog(shadow_log_path(settings)).read()


def test_the_browser_beside_the_api_compares_and_never_posts_or_writes_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = api_settings()
    run = api_run(settings)
    before = {p.name: p.read_bytes() for p in paths.tdx_dir(settings).iterdir()}

    def no_post(*args: object, **kwargs: object) -> int:
        raise AssertionError("the browser shadow never posts")

    monkeypatch.setattr(watch_module, "post_card", no_post)
    monkeypatch.setattr("ops_suite.common.teams.post_card", no_post)
    calls: list[AuditRun] = []

    line = BrowserShadow(settings, headless=True, reader=beside(settings, calls))(run)

    assert line == "Browser shadow: findings identical."
    assert calls == [run]
    after = {p.name: p.read_bytes() for p in paths.tdx_dir(settings).iterdir()}
    assert after.pop("api-shadow.jsonl") and after == before
    (record,) = _records(settings)
    assert (record["primary"], record["result"]) == ("api", "compared")
    assert record["findings"]["identical"] is True
    assert record["api_requests"] == run.api_requests and record["browser_non_get"] == 4
    assert record["seconds"]["api"] == round(run.read_seconds or 0, 1)


def test_a_fallback_run_is_recorded_and_not_compared() -> None:
    settings = api_settings()
    run = replace(
        browser_side_run(settings, datetime.now(TZ)), read_seconds=80.0, fallback="needs_sign_in"
    )
    calls: list[AuditRun] = []

    line = BrowserShadow(settings, headless=True, reader=beside(settings, calls))(run)

    assert line is None and calls == []
    (record,) = _records(settings)
    assert (record["primary"], record["result"], record["reason"]) == (
        "api",
        "fallback",
        "needs_sign_in",
    )
    assert record["seconds"] == {"browser": 80.0, "api": None}


def test_with_the_shadow_off_each_run_is_still_recorded_by_source() -> None:
    settings = api_settings(shadow={"enabled": False})
    run = api_run(settings)
    calls: list[AuditRun] = []

    line = BrowserShadow(settings, headless=True, reader=beside(settings, calls))(run)

    assert line is None and calls == []
    (record,) = _records(settings)
    assert (record["result"], record["reason"]) == ("skipped", "shadow_off")


def test_a_failing_browser_read_is_recorded_and_never_raises() -> None:
    settings = api_settings()
    run = api_run(settings)
    error = BrowserStartError("busy", reason="profile_in_use")

    line = BrowserShadow(settings, headless=True, reader=beside(settings, error=error))(run)

    assert line == "Browser shadow: not compared (profile_in_use)."
    (record,) = _records(settings)
    assert (record["primary"], record["result"], record["reason"]) == (
        "api",
        "failed",
        "profile_in_use",
    )


def test_the_browser_read_follows_the_api_runs_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = api_settings()
    run = api_run(settings)
    page = browser_side_run(settings, run.now)
    asked: dict[str, object] = {}

    class NoBrowser:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "NoBrowser":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def read_feeds(browser, tdx, rows_by_id, ticket_ids, now, progress, **kwargs) -> FeedRead:
        asked.update(ids=list(ticket_ids), now=now, rows=rows_by_id, forms=kwargs)
        return page.feeds

    monkeypatch.setattr(watch_module, "TdxBrowser", NoBrowser)
    monkeypatch.setattr(watch_module, "read_dashboard", lambda *args, **kwargs: page.dashboard)
    monkeypatch.setattr(watch_module, "read_feeds", read_feeds)

    read, non_get = shadow.read_browser_beside(settings, run, headless=True)

    plan = plan_feeds(run.rows, run.now, rule_settings(settings.tdx), TZ)
    assert asked["ids"] == list(plan.feed_ticket_ids) and asked["now"] == run.now
    rows_by_id = asked["rows"]
    assert isinstance(rows_by_id, dict)
    assert all(PAGE_HOST in (rows_by_id[r.ticket_id].detail_url or "") for r in page.rows)
    assert list(read.rows) == list(page.rows) and non_get == 0


# --- the watch loop in api mode --------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def run_watch(
    settings: Settings,
    script: list[AuditRun | BaseException],
    shadow_step: Callable[[AuditRun], str | None] | None,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, list[str], list[str]]:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    clock = Clock(datetime(2026, 10, 16, 10, 0, tzinfo=TZ))
    steps = list(script)

    def run_cycle(summary: object = None) -> tuple[AuditRun, SendOutcome]:
        if not steps:
            raise KeyboardInterrupt
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step, SendOutcome("nothing_new")

    notices: list[str] = []
    code = watch(
        settings,
        clock=clock,
        sleep=clock.sleep,
        run_cycle=run_cycle,
        notify=notices.append,
        shadow=shadow_step,
    )
    return code, capsys.readouterr().out.splitlines(), notices


def test_each_cycle_line_names_its_source(capsys: pytest.CaptureFixture[str]) -> None:
    settings = api_settings()
    from_api = api_run(settings)
    fallback = replace(from_api, source="browser", fallback="needs_sign_in")

    code, out, notices = run_watch(
        settings, [from_api, fallback], lambda run: "Browser shadow: findings identical.", capsys
    )

    assert code == 0 and notices == []
    assert out[1:3] == [API_PRIMARY_ON, BROWSER_SHADOW_ON]
    cycles = [line for line in out if "unclaimed" in line]
    assert cycles[0][6:].startswith("source: api; ")
    assert cycles[0].endswith(
        "Teams: nothing new. Browser shadow: findings identical. Next run 10:30."
    )
    assert cycles[1][6:].startswith("source: browser (fallback: needs_sign_in); ")


def test_api_fine_and_browser_signed_out_is_no_failure(capsys: pytest.CaptureFixture[str]) -> None:
    settings = api_settings(watch={"failures_before_stop": 1})
    run = api_run(settings)
    step = BrowserShadow(settings, headless=True, reader=beside(settings, error=NotSignedInError()))

    code, out, notices = run_watch(settings, [run, run], step, capsys)

    assert code == 0 and notices == []
    cycles = [line for line in out if "unclaimed" in line]
    assert len(cycles) == 2 and all(BROWSER_SIGNED_OUT in line for line in cycles)
    assert [(r["result"], r["reason"]) for r in _records(settings)] == [
        ("skipped", "browser_signed_out")
    ] * 2
    assert not session_log_path(settings).exists()


def test_a_signed_out_fallback_is_handled_as_before_and_the_api_resumes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = api_settings()
    run = api_run(settings)

    code, out, notices = run_watch(settings, [NotSignedInError(), run], None, capsys)

    assert code == 0
    assert notices == [sign_in_notice(settings.tdx.watch.signed_out_retry_minutes)]
    assert any(line.endswith("The TDX Web API answered; resuming.") for line in out)


def test_the_default_source_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(tdx={"base_url": BASE})
    run = browser_side_run(settings, datetime.now(TZ))

    code, out, _ = run_watch(settings, [run], None, capsys)

    assert code == 0 and settings.tdx.source == "browser"
    assert not any("source" in line or "API" in line for line in out)
    assert any(line.endswith("Teams: nothing new. Next run 10:30.") for line in out)


# --- tdx api compare over both periods --------------------------------------------------------


def test_compare_reports_the_shadow_and_the_api_primary_periods(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = api_settings()
    run = api_run(settings)
    same = from_run(settings, run)
    diff = compare(same, same)
    log = ShadowLog(shadow_log_path(settings))
    now = datetime.now(UTC)

    def at(hours: int) -> datetime:
        return now - timedelta(hours=hours)

    log.append(shadow_record(diff, at(9), Timing(90, 40, 12, 10)), now)  # the shadow period
    log.append(shadow_record(diff, at(8), Timing(90, 40, 12, 10)), now)
    for hours in (5, 4):
        timing = Timing(80, 30, 14, 10)
        log.append(shadow_record(diff, at(hours), timing, primary="api"), now)
    log.append(skipped_record("browser_signed_out", at(3), Timing(None, 31, 14, 0)), now)
    log.append(fallback_record("needs_sign_in", at(2), Timing(85, None, 0, 10)), now)
    log.append(fallback_record("needs_sign_in", at(1), Timing(85, None, 0, 10)), now)

    assert main(["tdx", "api", "compare", "--days", "1"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "API shadow, last 1 day: 2 cycles; 2 compared, 0 API failures.",
        "Rows matched: 100.0% (54 of 54). Feeds matched: 100.0% (34 of 34). "
        "Cycles with identical findings: 100.0% (2 of 2).",
        "Mismatches: none.",
        "Median per compared cycle: browser 90.0s, API 40.0s; API requests 12; "
        "browser allowed non-GET requests 10.",
        "Verdict: API matched the browser in 2 of 2 cycles; API reads were 50.0s faster.",
        "API primary, last 1 day: 5 cycles; 3 read through the API, 2 browser fallbacks "
        "(needs_sign_in 2).",
        "Browser shadow: 2 compared, 1 skipped (browser_signed_out 1).",
        "Rows matched: 100.0% (54 of 54). Feeds matched: 100.0% (34 of 34). "
        "Cycles with identical findings: 100.0% (2 of 2).",
        "Mismatches: none.",
        "Median per compared cycle: browser 80.0s, API 30.0s; API requests 14; "
        "browser allowed non-GET requests 10.",
        "Verdict: the browser matched the API in 2 of 2 cycles; API reads were 50.0s faster.",
    ]


# --- tdx audit with tdx.source = "api" --------------------------------------------------------


@pytest.fixture
def api_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", BASE)
    monkeypatch.setenv("OPS_SUITE_TDX__SOURCE", "api")
    monkeypatch.setenv("OPS_SUITE_TDX__MAX_FEEDS_PER_RUN", "200")
    monkeypatch.setenv("OPS_SUITE_TDX__API__ENABLED", "true")
    monkeypatch.setenv("OPS_SUITE_TDX__API__APP_ID", str(APP))
    monkeypatch.setenv("OPS_SUITE_TDX__API__UNCLAIMED_REPORT_ID", str(UNCLAIMED_REPORT))
    monkeypatch.setenv("OPS_SUITE_TDX__API__CLAIMED_REPORT_ID", str(CLAIMED_REPORT))
    return monkeypatch


def _use(env: pytest.MonkeyPatch, api: FakeApi, browser: FakeBrowser) -> None:
    """Every API client on ``api`` with a stored token; the browser read is ``browser``."""
    token = make_token(datetime.now(UTC) + timedelta(hours=20))

    def client(tdx, tokens, **kwargs) -> TdxApiClient:
        return TdxApiClient(tdx, tokens, opener=api, sleep=lambda seconds: None, **kwargs)

    env.setattr(shadow, "TdxApiClient", client)
    env.setattr(shadow, "browser_fetcher", lambda settings: lambda: token)
    env.setattr(
        api_primary,
        "read_and_judge",
        lambda settings, *, headless, progress: browser(settings, progress),
    )


def test_tdx_audit_follows_tdx_source_api_and_may_send(
    api_env: pytest.MonkeyPatch, posted: list[dict], capsys: pytest.CaptureFixture[str]
) -> None:
    browser = FakeBrowser(AssertionError("the browser is not read"))
    _use(api_env, demo_api(datetime.now(TZ)), browser)

    code = main(["tdx", "audit", "--send"])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0] == cli.API_FIRST_LINE
    assert out[-1].startswith("Teams: sent 1 card") and len(posted) == 1
    assert browser.calls == 0


def test_tdx_audit_says_when_the_browser_read_instead(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = demo_api(datetime.now(TZ))
    api.routes[f"{API}/reports/{UNCLAIMED_REPORT}"] = Answer(503)
    browser = FakeBrowser()
    _use(api_env, api, browser)

    assert main(["tdx", "audit"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [
        cli.API_FIRST_LINE,
        "Read through the browser instead: the TDX Web API is unavailable (http_503).",
    ]
    assert browser.calls == 1


def test_tdx_audit_source_api_with_tdx_source_api_has_no_fallback_and_may_send(
    api_env: pytest.MonkeyPatch, posted: list[dict], capsys: pytest.CaptureFixture[str]
) -> None:
    api = demo_api(datetime.now(TZ))
    browser = FakeBrowser(AssertionError("no fallback with --source api"))
    _use(api_env, api, browser)
    for module in (cli, watch_module):
        api_env.setattr(
            module,
            "TdxApiClient",
            lambda tdx, tokens, **kwargs: TdxApiClient(
                tdx, stored_tokens(), opener=api, sleep=lambda seconds: None
            ),
        )

    assert main(["tdx", "audit", "--source", "api", "--send"]) == 0

    assert capsys.readouterr().out.splitlines()[0] == cli.API_ONLY_LINE
    assert len(posted) == 1


def test_tdx_audit_source_api_without_tdx_source_api_stays_the_trial(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__SOURCE", "browser")

    assert main(["tdx", "audit", "--source", "api", "--send"]) == 2

    assert "--send works with the browser source only" in capsys.readouterr().err


def test_records_hold_no_names_or_titles() -> None:
    settings = api_settings()
    now = datetime.now(TZ)
    tickets = demo_tickets(now)
    run = api_run(settings)
    BrowserShadow(settings, headless=True, reader=beside(settings))(run)

    text = shadow_log_path(settings).read_text(encoding="utf-8")
    for ticket in tickets:
        assert ticket.title not in text and ticket.requestor not in text


# --- urgent event cards: the API run only -----------------------------------------------------

DESK_URL = "https://prod-02.westus.logic.azure.com/workflows/desk/invoke?sig=D"


def test_urgent_event_cards_come_from_the_api_run_only_and_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = api_settings(summary={"enabled": True}).model_copy(
        update={"teams": Settings(teams={"desk": {"approved": True}}).teams}
    )
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    set_webhook_url(DESK_URL, [".logic.azure.com"], "desk")
    posts: list[tuple[str, dict]] = []

    def post(url: str, card: dict, timeout: float) -> int:
        posts.append((url, card))
        return 202

    monkeypatch.setattr(watch_module, "post_card", post)
    reader, browser, _ = reader_for(settings, [demo_api(datetime.now(TZ))])
    step = BrowserShadow(settings, headless=True, reader=beside(settings))
    runs: list[AuditRun] = []

    def run_cycle(summary: SummaryRequest | None = None) -> tuple[AuditRun, SendOutcome]:
        if len(runs) == 3:
            raise KeyboardInterrupt
        run = reader()
        runs.append(run)
        return run, send_cycle(run, settings, "desk", summary)

    clock = Clock(datetime(2026, 10, 16, 10, 0, tzinfo=TZ))  # between summary slots
    watch(
        settings,
        clock=clock,
        sleep=clock.sleep,
        run_cycle=run_cycle,
        notify=lambda text: None,
        destination="desk",
        shadow=step,
    )

    near = settings.tdx.events.near_event_days
    expected = {(f.ticket_id, f.kind) for f in runs[0].result.findings if urgent_event(f, near)}
    urgent = [
        card
        for url, card in posts
        if url == DESK_URL and card["body"][0]["text"].startswith("Event soon")
    ]
    assert expected and {run.source for run in runs} == {"api"} and browser.calls == 0
    assert len(urgent) == 1  # three cycles, each also read by the browser: one card
    assert urgent[0]["body"][0]["text"] == urgent_heading(len(expected))
    assert [record["primary"] for record in _records(settings)] == ["api"] * 3
