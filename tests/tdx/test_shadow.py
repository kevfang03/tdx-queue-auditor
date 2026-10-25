"""Shadow mode: the API read beside the browser's in a watch cycle, the counts-only diff, the
``api-shadow.jsonl`` record and the ``tdx api compare`` report. Synthetic data on both sides
(the demo dataset as browser pages and as API answers); no browser, network or real time."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from api_fakes import (
    API,
    APP,
    BASE,
    CLAIMED_REPORT,
    UNCLAIMED_REPORT,
    FakeApi,
    FakeClock,
    demo_browser_side,
    demo_routes,
    stored_tokens,
)

from ops_suite.cli import main
from ops_suite.common import paths
from ops_suite.common.config import Settings
from ops_suite.common.teams import set_webhook_url
from ops_suite.tdx import watch as watch_module
from ops_suite.tdx.alerts import urgent_event, urgent_heading
from ops_suite.tdx.api_client import TdxApiClient
from ops_suite.tdx.api_token import TokenError
from ops_suite.tdx.dashboard import KINDS, DashboardRead, DashboardStructureError, WidgetRead
from ops_suite.tdx.desk_memory import DeskMemory
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import FINDING_KINDS, Finding, evaluate, plan_feeds
from ops_suite.tdx.shadow import (
    CATEGORIES,
    KEEP_DAYS,
    SHOW_IDS_WARNING,
    ShadowLog,
    ShadowRunner,
    SourceRead,
    Timing,
    compare,
    compare_report,
    failure_record,
    shadow_line,
    shadow_log_path,
    shadow_record,
)
from ops_suite.tdx.tickets import FeedRead
from ops_suite.tdx.watch import (
    API_SIGN_IN_NOTICE,
    AuditRun,
    SendOutcome,
    desk_memory_path,
    form_options,
    rule_settings,
    watch,
)

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)  # the demo's queue at noon
REQUESTOR = "Jordan Sampleton"
DESK = "Casey Deskworker"


def shadow_settings(**tdx: object) -> Settings:
    values: dict[str, object] = {
        "base_url": BASE,
        "source": "shadow",
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


# --- hand-made rows and feeds for the pure diff --------------------------------------------


def row(ticket_id: int, **values: object) -> TicketRow:
    fields: dict[str, object] = {
        "widget": "unclaimed",
        "ticket_id": ticket_id,
        "title": "Printer offline",
        "requestor": REQUESTOR,
        "resp_group": "IT Service Desk",
        "created": NOW - timedelta(days=3),
        "modified": None,
        "prim_resp": None,
        "status": None,
        "detail_url": None,
    }
    fields.update(values)
    return TicketRow(**fields)  # type: ignore[arg-type]


def feed(ticket_id: int, *entries: FeedEntry) -> TicketFeed:
    return TicketFeed(ticket_id, entries)


def reply(author: str, hours_ago: float, kind: str = "reply") -> FeedEntry:
    return FeedEntry(author, NOW - timedelta(hours=hours_ago), kind)  # type: ignore[arg-type]


def waiting(ticket_id: int) -> Finding:
    return Finding(ticket_id, "client_waiting", "high", "requestor_last", 60)


def same_both_sides() -> tuple[SourceRead, SourceRead]:
    rows = [row(1), row(2), row(3, widget="claimed", modified=NOW, prim_resp=DESK, status="Open")]
    feeds = {
        1: feed(1, reply(REQUESTOR, 60), reply(DESK, 70)),
        2: feed(2, reply(DESK, 80, "private"), reply(REQUESTOR, 90)),
    }
    findings = [waiting(1)]
    return SourceRead(rows, feeds, findings), SourceRead(list(rows), dict(feeds), findings)


TIMING = Timing(browser_seconds=90.04, api_seconds=40.0, api_requests=12, browser_non_get=10)


# --- the diff ----------------------------------------------------------------------------


def test_identical_sources_match_everywhere() -> None:
    browser, api = same_both_sides()

    diff = compare(browser, api)

    assert diff.findings.identical and diff.findings.differences == 0
    assert diff.rows["unclaimed"].matched == diff.rows["unclaimed"].total == 2
    assert diff.rows["claimed"].matched == diff.rows["claimed"].total == 1
    assert diff.feeds.matched == diff.feeds.compared == diff.feeds.total == 2
    assert (
        diff.feeds.entries_browser
        == diff.feeds.entries_api
        == {
            "reply": 3,
            "private": 1,
            "system": 0,
        }
    )
    assert shadow_line(diff) == "API shadow: findings identical."
    report = compare_report([shadow_record(diff, NOW, TIMING)], days=14)
    assert report[1] == (
        "Rows matched: 100.0% (3 of 3). Feeds matched: 100.0% (2 of 2). "
        "Cycles with identical findings: 100.0% (1 of 1)."
    )
    assert report[2] == "Mismatches: none."


def test_an_id_only_in_the_api_is_counted_as_such() -> None:
    browser, api = same_both_sides()
    api = SourceRead([*api.rows, row(9)], api.feeds, api.findings)

    diff = compare(browser, api)

    unclaimed = diff.rows["unclaimed"]
    assert (unclaimed.browser, unclaimed.api, unclaimed.matched) == (2, 3, 2)
    assert unclaimed.only_api == (9,) and unclaimed.only_browser == ()
    assert unclaimed.total == 3


def test_created_two_minutes_off_differs_and_thirty_seconds_does_not() -> None:
    browser, _ = same_both_sides()
    created = NOW - timedelta(days=3)
    shifted = [
        row(1, created=created + timedelta(minutes=2)),
        row(2, created=created + timedelta(seconds=30)),
        browser.rows[2],
    ]

    diff = compare(browser, SourceRead(shifted, browser.feeds, browser.findings))

    assert diff.rows["unclaimed"].differs == {"created": (1,)}
    assert diff.rows["unclaimed"].matched == 1


def test_names_compare_as_people_and_titles_as_normalised_text() -> None:
    browser, _ = same_both_sides()
    api_rows = [
        row(1, requestor="Sampleton, Jordan", title="  printer OFFLINE "),
        row(2, requestor="Avery Otherperson"),
        browser.rows[2],
    ]

    diff = compare(browser, SourceRead(api_rows, browser.feeds, browser.findings))

    assert diff.rows["unclaimed"].differs == {"requestor": (2,)}


def test_a_different_latest_author_and_private_flag_are_found() -> None:
    browser, _ = same_both_sides()
    api_feeds = {
        1: feed(1, reply("Avery Otherperson", 60), reply(DESK, 70)),
        2: feed(2, reply(DESK, 80, "reply"), reply(REQUESTOR, 90)),
    }

    diff = compare(browser, SourceRead(browser.rows, api_feeds, browser.findings))

    assert diff.feeds.differs == {
        "latest_author": (1,),
        "reply_count": (2,),
        "private_count": (2,),
        "latest_private": (2,),
    }
    assert diff.feeds.matched == 0
    assert diff.findings.identical  # the findings are compared on their own


def test_a_latest_time_off_and_a_feed_read_by_one_source_only() -> None:
    browser, _ = same_both_sides()
    api_feeds = {1: feed(1, reply(REQUESTOR, 60.05), reply(DESK, 70))}  # 3 minutes off

    diff = compare(browser, SourceRead(browser.rows, api_feeds, browser.findings))

    assert diff.feeds.differs == {"latest_time": (1,)}
    assert diff.feeds.only_browser == (2,) and diff.feeds.only_api == ()
    assert (diff.feeds.compared, diff.feeds.total) == (1, 2)


def test_a_different_finding_set_is_counted_and_shown_on_the_cycle_line() -> None:
    browser, _ = same_both_sides()
    other = [Finding(2, "needs_review", "normal", "other_last", 50)]

    diff = compare(browser, SourceRead(browser.rows, browser.feeds, other))

    assert not diff.findings.identical
    assert diff.findings.only_browser == ((1, "client_waiting", "requestor_last"),)
    assert diff.findings.only_api == ((2, "needs_review", "other_last"),)
    assert shadow_line(diff) == "API shadow: 2 differences (see tdx api compare)."


# --- the record --------------------------------------------------------------------------

ALLOWED_TEXT = {
    *CATEGORIES,
    *FINDING_KINDS,
    *KINDS,
    "compared",
    "failed",
    "requestor_last",
    "other_last",
    "no_reply_yet",
    "time_box",
}


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [*value, *(s for item in value.values() for s in _strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def test_the_record_holds_counts_ids_and_codes_only() -> None:
    browser, _ = same_both_sides()
    api_rows = [row(1, created=NOW), row(9), browser.rows[2]]
    api_feeds = {1: feed(1, reply("Avery Otherperson", 60))}
    other = [Finding(2, "needs_review", "normal", "other_last", 50)]

    record = shadow_record(compare(browser, SourceRead(api_rows, api_feeds, other)), NOW, TIMING)

    text = json.dumps(record)
    for private in (REQUESTOR, DESK, "Avery", "Printer", "printer", "IT Service Desk", "Open"):
        assert private not in text
    keys = {"schema", "at", "primary", "result", "seconds", "api_requests", "browser_non_get"}
    keys |= {"rows"}
    keys |= {"feeds", "findings", "browser", "api", "matched", "only_browser", "only_api"}
    keys |= {"differs", "compared", "entries", "identical", "reply", "private", "system"}
    for found in _strings(record):
        if found == record["at"]:
            continue
        assert found in ALLOWED_TEXT | keys, found
    assert record["at"] == "2026-10-17T16:00:00+00:00"
    assert record["primary"] == "browser"
    assert record["seconds"] == {"browser": 90.0, "api": 40.0}
    assert record["rows"]["unclaimed"]["only_api"] == [9]
    assert record["findings"]["only_api"] == [[2, "needs_review", "other_last"]]


def test_the_log_keeps_30_days_and_drops_unreadable_lines(tmp_path: Path) -> None:
    path = tmp_path / "api-shadow.jsonl"
    log = ShadowLog(path)
    old = failure_record("timeout", NOW - timedelta(days=KEEP_DAYS, minutes=1), TIMING)
    recent = failure_record("timeout", NOW - timedelta(days=KEEP_DAYS - 1), TIMING)
    path.write_text(f"{json.dumps(old)}\nnot json\n{json.dumps(recent)}\n", encoding="utf-8")

    dropped = log.append(failure_record("time_box", NOW, TIMING), NOW)

    assert dropped == 1  # the old record; the unreadable line was never a record
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["reason"] for line in lines] == ["timeout", "time_box"]
    assert [r["reason"] for r in log.since(NOW - timedelta(days=1))] == ["time_box"]


# --- the shadow step in a cycle ------------------------------------------------------------


def browser_run(settings: Settings, now: datetime = NOW) -> tuple[AuditRun, list]:
    """The demo queue as the browser path gives it, judged as the watch cycle judges it."""
    tdx = settings.tdx
    tickets, rows, feeds, forms = demo_browser_side(now, TZ, form_options(tdx))
    rules = rule_settings(tdx)
    plan = plan_feeds(rows, now, rules, TZ)
    read = {i: feeds[i] for i in plan.feed_ticket_ids}
    forms_read = {i: form for i, form in forms.items() if i in plan.form_ticket_ids}
    widgets = {
        kind: WidgetRead(kind, tuple(r for r in rows if r.widget == kind), 1, (), False)
        for kind in KINDS
    }
    dashboard = DashboardRead(widgets, blocked=(), allowed_non_get=("POST /masked/1",))
    feed_read = FeedRead(
        feeds=read,
        failed=(),
        deferred=(),
        blocked=(),
        allowed_non_get=("POST /masked/2", "POST /masked/3"),
        forms=forms_read,
    )
    result = evaluate(rows, read, now, rules, TZ, forms=forms_read)
    run = AuditRun(dashboard, feed_read, result, tuple(rows), now, read_seconds=95.0)
    return run, tickets


class Factory:
    """Builds the runner's API client on the fake opener and the fake clock."""

    def __init__(self, api: FakeApi, clock: FakeClock) -> None:
        self.api = api
        self.clock = clock
        self.deadlines: list[float] = []

    def __call__(self, settings: Settings, deadline: float) -> TdxApiClient:
        self.deadlines.append(deadline)
        return TdxApiClient(
            settings.tdx,
            stored_tokens(),
            opener=self.api,  # type: ignore[arg-type]
            clock=self.clock,
            wall=lambda: NOW.astimezone(UTC),
            sleep=self.clock.sleep,
            deadline=deadline,
        )


def runner_for(
    settings: Settings, api: FakeApi, clock: FakeClock | None = None
) -> tuple[ShadowRunner, list[str], Factory]:
    clock = clock or FakeClock()
    notices: list[str] = []
    factory = Factory(api, clock)
    return (
        ShadowRunner(settings, notices.append, client_factory=factory, clock=clock),
        (notices),
        factory,
    )


def _records(settings: Settings) -> list[dict]:
    return ShadowLog(shadow_log_path(settings)).read()


def test_the_same_queue_through_both_sources_is_identical_and_recorded() -> None:
    settings = shadow_settings()
    run, tickets = browser_run(settings)
    api = FakeApi(demo_routes(tickets, NOW, TZ))
    runner, notices, factory = runner_for(settings, api)

    line = runner(run)

    assert line == "API shadow: findings identical."
    assert notices == []
    assert factory.deadlines == [1000.0 + 300]
    (record,) = _records(settings)
    assert record["result"] == "compared"
    assert record["findings"]["identical"] is True
    assert record["findings"]["browser"] == len(run.result.findings) > 0
    for kind in KINDS:
        rows = record["rows"][kind]
        assert rows["matched"] == rows["browser"] == rows["api"] > 0
        assert rows["differs"] == {}
    assert record["feeds"]["matched"] == record["feeds"]["compared"] == len(run.feeds.feeds)
    assert record["browser_non_get"] == 3
    assert record["seconds"]["browser"] == 95.0
    assert record["api_requests"] == len(api.calls) > 0
    assert {method for method, _, _ in api.calls} == {"GET"}
    # Only the feeds the browser's plan picked were read through the API.
    feed_paths = {p for p in api.paths() if p.endswith("/feed")}
    assert feed_paths == {f"{API}/{APP}/tickets/{i}/feed" for i in run.feeds.feeds}
    # No names, titles or text from either source.
    text = shadow_log_path(settings).read_text(encoding="utf-8")
    for ticket in tickets:
        assert ticket.title not in text and ticket.requestor not in text
        for item in ticket.entries:
            assert item.author not in text


def test_the_api_path_never_posts_or_writes_alert_summary_verdict_or_memory_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = shadow_settings()
    run, tickets = browser_run(settings)
    memory = DeskMemory(desk_memory_path(settings))
    memory.remember(["Example Deskmember"], NOW)
    memory.save(NOW)
    before = desk_memory_path(settings).read_bytes()

    def no_post(*args: object, **kwargs: object) -> int:
        raise AssertionError("the shadow step never posts")

    monkeypatch.setattr(watch_module, "post_card", no_post)
    monkeypatch.setattr("ops_suite.common.teams.post_card", no_post)
    runner, _, _ = runner_for(settings, FakeApi(demo_routes(tickets, NOW, TZ)))

    assert runner(run) == "API shadow: findings identical."

    assert desk_memory_path(settings).read_bytes() == before
    written = sorted(p.name for p in paths.tdx_dir(settings).iterdir())
    assert written == ["api-shadow.jsonl", "desk-members.json"]


def test_a_read_only_memory_is_never_saved_and_leaves_a_bad_file_where_it_is(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desk-members.json"
    path.write_text("not json", encoding="utf-8")

    memory = DeskMemory(path, read_only=True)

    assert len(memory) == 0 and path.read_text(encoding="utf-8") == "not json"
    with pytest.raises(RuntimeError):
        memory.save(NOW)


def test_an_api_failure_is_recorded_with_its_reason_and_returns_a_line() -> None:
    settings = shadow_settings()
    run, tickets = browser_run(settings)
    routes = demo_routes(tickets, NOW, TZ)
    routes.pop(f"{API}/reports/{UNCLAIMED_REPORT}")  # answers 404
    runner, notices, _ = runner_for(settings, FakeApi(routes))

    line = runner(run)

    assert line == "API shadow: not compared (not_found)."
    (record,) = _records(settings)
    assert (record["result"], record["reason"]) == ("failed", "not_found")
    assert record["api_requests"] == 1 and notices == []


def test_the_time_box_stops_the_api_part_and_is_recorded() -> None:
    settings = shadow_settings(shadow={"max_seconds": 60})
    run, tickets = browser_run(settings)
    clock = FakeClock()
    routes = demo_routes(tickets, NOW, TZ)
    slow_report = routes[f"{API}/reports/{UNCLAIMED_REPORT}"]

    def slow(request: object) -> object:
        clock.now += 61  # the first report takes longer than the whole time box
        return slow_report

    routes[f"{API}/reports/{UNCLAIMED_REPORT}"] = slow
    api = FakeApi(routes)
    runner, _, _ = runner_for(settings, api, clock)

    line = runner(run)

    assert line == "API shadow: not compared (time_box)."
    (record,) = _records(settings)
    assert record["reason"] == "time_box" and record["api_requests"] == 1
    assert len(api.calls) == 1  # nothing was sent after the time box
    assert record["seconds"]["api"] >= 60


def test_a_token_that_needs_a_sign_in_sends_one_notice_a_day() -> None:
    settings = shadow_settings()
    run, _ = browser_run(settings)
    notices: list[str] = []

    def no_token(settings: Settings, deadline: float) -> TdxApiClient:
        raise TokenError("needs_sign_in")

    runner = ShadowRunner(settings, notices.append, client_factory=no_token)

    lines = [runner(run), runner(run)]

    assert lines == ["API shadow: not compared (needs_sign_in)."] * 2
    assert notices == [API_SIGN_IN_NOTICE]
    assert [r["reason"] for r in _records(settings)] == ["needs_sign_in"] * 2


def test_an_unexpected_error_is_recorded_by_class_name_only() -> None:
    settings = shadow_settings()
    run, _ = browser_run(settings)

    def broken(settings: Settings, deadline: float) -> TdxApiClient:
        raise ValueError(f"something about {REQUESTOR}")

    line = ShadowRunner(settings, lambda text: None, client_factory=broken)(run)

    assert line == "API shadow: not compared (ValueError)."
    assert REQUESTOR not in shadow_log_path(settings).read_text(encoding="utf-8")


# --- the report ----------------------------------------------------------------------------


def seeded(settings: Settings) -> None:
    """Five records: three identical cycles, one with differences, one failure; and one
    record from 20 days ago."""
    browser, api = same_both_sides()
    log = ShadowLog(shadow_log_path(settings))
    now = datetime.now(UTC)
    old_diff = compare(browser, api)
    log.append(shadow_record(old_diff, now - timedelta(days=20), TIMING), now)
    for hours in (5, 4, 3):
        timing = Timing(80.0 + hours, 30.0 + hours, 12, 10)
        log.append(shadow_record(compare(browser, api), now - timedelta(hours=hours), timing), now)
    api_rows = [row(1, created=NOW), *api.rows[1:]]
    api_feeds = {**api.feeds, 1: feed(1, reply("Avery Otherperson", 60), reply(DESK, 70))}
    other = SourceRead(api_rows, api_feeds, [Finding(1, "needs_review", "normal", "x", 1)])
    log.append(
        shadow_record(compare(browser, other), now - timedelta(hours=2), Timing(90, 40, 14, 10)),
        now,
    )
    log.append(failure_record("time_box", now - timedelta(hours=1), Timing(85, 300, 40, 10)), now)


def test_tdx_api_compare_prints_percentages_and_the_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings()
    seeded(settings)

    assert main(["tdx", "api", "compare", "--days", "7"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out == [
        "API shadow, last 7 days: 5 cycles; 4 compared, 1 API failure (time_box 1).",
        "Rows matched: 91.7% (11 of 12). Feeds matched: 87.5% (7 of 8). "
        "Cycles with identical findings: 75.0% (3 of 4).",
        "Mismatches: Created differs: 1 (1 ticket), latest author differs: 1 (1 ticket), "
        "findings only from the browser: 1 (1 ticket), findings only from the API: 1 (1 ticket).",
        "Median per compared cycle: browser 84.5s, API 34.5s; API requests 12; "
        "browser allowed non-GET requests 10.",
        "Verdict: API matched the browser in 3 of 5 cycles; API reads were 50.0s faster.",
    ]
    assert REQUESTOR not in "\n".join(out)


def test_tdx_api_compare_days_sets_how_far_back(capsys: pytest.CaptureFixture[str]) -> None:
    seeded(Settings())

    assert main(["tdx", "api", "compare"]) == 0  # 14 days by default
    assert main(["tdx", "api", "compare", "--days", "30"]) == 0

    first, second = (line for line in capsys.readouterr().out.splitlines() if "last" in line)
    assert first == "API shadow, last 14 days: 5 cycles; 4 compared, 1 API failure (time_box 1)."
    assert second == "API shadow, last 30 days: 6 cycles; 5 compared, 1 API failure (time_box 1)."


def test_tdx_api_compare_show_ids_lists_ids_with_the_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded(Settings())

    assert main(["tdx", "api", "compare", "--days", "1", "--show-ids"]) == 0

    out = capsys.readouterr().out.splitlines()
    start = out.index(SHOW_IDS_WARNING)
    assert out[start + 1 :] == [
        "  created  1  1 cycle",
        "  latest_author  1  1 cycle",
        "  finding_only_browser  1  client_waiting  requestor_last  1 cycle",
        "  finding_only_api  1  needs_review  x  1 cycle",
    ]


def test_tdx_api_compare_without_records_says_how_to_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tdx", "api", "compare", "--days", "3"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        'No API shadow records in the last 3 days. Set tdx.source = "shadow" and run tdx watch '
        '(docs/USAGE.md, "Shadow mode").'
    ]


@pytest.mark.parametrize("days", ["0", "31", "two"])
def test_tdx_api_compare_refuses_days_outside_the_kept_window(
    days: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["tdx", "api", "compare", "--days", days])

    assert "must be a whole number from 1 to 30" in capsys.readouterr().err


def test_a_slower_api_and_unreadable_records_are_reported() -> None:
    browser, api = same_both_sides()
    record = shadow_record(compare(browser, api), NOW, Timing(30.0, 45.5, 12, 0))

    lines = compare_report([record, {"result": "compared"}], days=1)

    assert (
        lines[-2]
        == "Verdict: API matched the browser in 1 of 1 cycles; API reads were 15.5s slower."
    )
    assert lines[-1] == "Unreadable records skipped: 1."


# --- the watch loop --------------------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def run_watch(
    settings: Settings,
    script: list[BaseException | None],
    shadow: Callable[[AuditRun], str] | None,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, list[str], list[str]]:
    set_webhook_url(
        "https://prod-01.westus.logic.azure.com/workflows/abc/invoke?sig=S", [".logic.azure.com"]
    )
    clock = Clock(datetime(2026, 10, 16, 10, 0, tzinfo=TZ))
    run, _ = browser_run(settings)
    steps = list(script)

    def run_cycle(summary: object = None) -> tuple[AuditRun, SendOutcome]:
        if not steps:
            raise KeyboardInterrupt
        step = steps.pop(0)
        if step is not None:
            raise step
        return run, SendOutcome("nothing_new")

    notices: list[str] = []
    code = watch(
        settings,
        clock=clock,
        sleep=clock.sleep,
        run_cycle=run_cycle,
        notify=notices.append,
        shadow=shadow,
    )
    return code, capsys.readouterr().out.splitlines(), notices


def test_a_failing_shadow_step_never_fails_the_cycle_or_counts_toward_a_stop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = shadow_settings()
    calls: list[AuditRun] = []

    def broken(run: AuditRun) -> str:
        calls.append(run)
        raise RuntimeError("boom")

    failure = DashboardStructureError("synthetic")
    # Two failures, a good cycle whose shadow step fails, two more failures: with
    # failures_before_stop = 3 the loop keeps going, so the shadow failure counted nothing.
    code, out, notices = run_watch(
        settings, [failure, failure, None, failure, failure], broken, capsys
    )

    assert code == 0 and notices == []
    assert len(calls) == 1
    cycle = [line for line in out if "unclaimed" in line]
    assert len(cycle) == 1
    assert cycle[0].endswith(
        "Teams: nothing new. API shadow: not compared (error). Next run 11:30."
    )
    assert out[1].startswith("API shadow mode: each run also reads the same tickets")


def test_the_cycle_line_shows_the_shadow_result(capsys: pytest.CaptureFixture[str]) -> None:
    settings = shadow_settings()

    code, out, _ = run_watch(
        settings, [None], lambda run: "API shadow: findings identical.", capsys
    )

    assert code == 0
    assert any(
        line.endswith("Teams: nothing new. API shadow: findings identical. Next run 10:30.")
        for line in out
    )


def test_without_shadow_mode_the_output_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(tdx={"base_url": BASE})

    code, out, _ = run_watch(settings, [None], None, capsys)

    assert code == 0
    assert not any("shadow" in line.casefold() for line in out)
    assert any(line.endswith("Teams: nothing new. Next run 10:30.") for line in out)
    assert not shadow_log_path(settings).exists()


# --- settings and the start checks -----------------------------------------------------------


def test_the_browser_is_the_default_source_and_shadow_settings_are_checked() -> None:
    assert Settings().tdx.source == "browser"
    assert Settings().tdx.shadow.max_seconds == 300
    assert Settings().tdx.shadow.enabled is True
    assert Settings().tdx.api.max_cycle_seconds == 300
    assert Settings(tdx={"source": "api"}).tdx.source == "api"
    with pytest.raises(ValueError):
        Settings(tdx={"source": "both"})
    with pytest.raises(ValueError):
        Settings(tdx={"api": {"max_cycle_seconds": 10}})
    with pytest.raises(ValueError):
        Settings(tdx={"shadow": {"max_seconds": 10}})
    with pytest.raises(ValueError):
        Settings(tdx={"shadow": {"seconds": 300}})


@pytest.mark.parametrize(
    "api",
    [
        {"enabled": False, "unclaimed_report_id": 1, "claimed_report_id": 2},
        {"enabled": True, "unclaimed_report_id": 1},
    ],
)
@pytest.mark.parametrize("command", [["tdx", "watch"], ["tdx", "agent", "install"]])
@pytest.mark.parametrize("source", ["shadow", "api"])
def test_shadow_and_api_sources_need_the_api_before_anything_starts(
    api: dict, command: list[str], source: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", BASE)
    monkeypatch.setenv("OPS_SUITE_TDX__SOURCE", source)
    monkeypatch.setenv("OPS_SUITE_TDX__API", json.dumps(api))

    def no_watch(*args: object, **kwargs: object) -> int:
        raise AssertionError("nothing may start")

    monkeypatch.setattr("ops_suite.cli.watch", no_watch)

    class NoAgent:
        status = install = no_watch

    monkeypatch.setattr("ops_suite.cli.for_platform", lambda **kwargs: NoAgent())

    assert main(command) == 2

    assert f'tdx.source = "{source}" reads the queue through the TDX Web API' in (
        capsys.readouterr().err
    )


def test_a_record_names_the_primary_source() -> None:
    browser, api = same_both_sides()

    as_api = shadow_record(compare(browser, api), NOW, TIMING, primary="api")
    failed = failure_record("structure", NOW, TIMING, primary="api")

    assert as_api["primary"] == failed["primary"] == "api"
    assert compare_report([as_api, failed], days=1)[:2] == [
        "API primary, last 1 day: 2 cycles; 2 read through the API, 0 browser fallbacks.",
        "Browser shadow: 1 compared, 1 browser failure (structure 1).",
    ]


def test_a_named_period_replaces_the_days_in_both_headings_only() -> None:
    browser, api = same_both_sides()
    records = [
        shadow_record(compare(browser, api), NOW, TIMING),
        shadow_record(compare(browser, api), NOW, TIMING, primary="api"),
    ]

    default = compare_report(records, days=1)
    today = compare_report(records, days=1, period="today")

    headings = [line for line in today if line.startswith(("API shadow,", "API primary,"))]
    assert headings == [
        "API shadow, today: 1 cycle; 1 compared, 0 API failures.",
        "API primary, today: 1 cycle; 1 read through the API, 0 browser fallbacks.",
    ]
    assert [line.replace("today", "last 1 day") for line in today] == default
    assert compare_report(records, days=1, period=None) == default


# --- urgent event cards: the browser run only ------------------------------------------------

DESK_URL = "https://prod-02.westus.logic.azure.com/workflows/desk/invoke?sig=D"


def test_urgent_event_cards_come_from_the_browser_run_only_and_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = shadow_settings(summary={"enabled": True}).model_copy(
        update={"teams": Settings(teams={"desk": {"approved": True}}).teams}
    )
    set_webhook_url(
        "https://prod-01.westus.logic.azure.com/workflows/abc/invoke?sig=S", [".logic.azure.com"]
    )
    set_webhook_url(DESK_URL, [".logic.azure.com"], "desk")
    posts: list[tuple[str, dict]] = []

    def post(url: str, card: dict, timeout: float) -> int:
        posts.append((url, card))
        return 202

    monkeypatch.setattr(watch_module, "post_card", post)
    run, tickets = browser_run(settings)
    runner, _, _ = runner_for(settings, FakeApi(demo_routes(tickets, NOW, TZ)))
    requests: list[object] = []

    def run_cycle(summary: object = None) -> tuple[AuditRun, SendOutcome]:
        if len(requests) == 3:
            raise KeyboardInterrupt
        requests.append(summary)
        return run, watch_module.send_cycle(run, settings, "desk", summary)

    clock = Clock(datetime(2026, 10, 16, 10, 0, tzinfo=TZ))  # between summary slots
    watch(
        settings,
        clock=clock,
        sleep=clock.sleep,
        run_cycle=run_cycle,
        notify=lambda text: None,
        destination="desk",
        shadow=runner,
    )

    near = settings.tdx.events.near_event_days
    expected = {(f.ticket_id, f.kind) for f in run.result.findings if urgent_event(f, near)}
    urgent = [
        card
        for url, card in posts
        if url == DESK_URL and card["body"][0]["text"].startswith("Event soon")
    ]
    assert expected and requests == [None, None, None]
    assert len(urgent) == 1  # three cycles, each also read through the API: one card
    assert urgent[0]["body"][0]["text"] == urgent_heading(len(expected))
    assert len(_records(settings)) == 3
