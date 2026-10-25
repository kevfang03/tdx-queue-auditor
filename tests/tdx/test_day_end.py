"""The day-end notice of ``tdx watch``: the token line, the notice text, its state file and
when the loop sends it. Synthetic records and tokens; no browser, network or real time."""

import json
import logging
from datetime import date, datetime, timedelta

import pytest
from api_fakes import APP, BASE, CLAIMED_REPORT, TZ, UNCLAIMED_REPORT, make_token

from ops_suite.common.config import ScheduleSettings, Settings
from ops_suite.common.schedule import DeskHours
from ops_suite.common.teams import set_webhook_url
from ops_suite.tdx import watch as watch_module
from ops_suite.tdx.api_token import ApiToken
from ops_suite.tdx.dashboard import DashboardRead, WidgetRead
from ops_suite.tdx.rules import AuditResult
from ops_suite.tdx.shadow import SCHEMA, ShadowLog, shadow_log_path
from ops_suite.tdx.tickets import FeedRead
from ops_suite.tdx.watch import (
    DAY_END_MISMATCH_ADVICE,
    DAY_END_NO_RECORDS,
    AuditRun,
    DayEndState,
    SendOutcome,
    day_end_notice,
    day_end_state_path,
    token_line,
    watch,
    webhook_notifier,
)

WEBHOOK = "https://prod-01.westus.logic.azure.com/workflows/abc/invoke?sig=SIGSECRET42"
DESK_WEBHOOK = "https://prod-02.westus.logic.azure.com/workflows/desk/invoke?sig=SIGSECRET43"
DESK = DeskHours.from_settings(ScheduleSettings())
COMMAND = '"uv run ops-suite tdx api token --headed"'
# Ticket ids in the records; none may reach a notice.
IDS = (918273, 918274, 918275, 918276, 918277)


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


def api_settings(**tdx: object) -> Settings:
    values: dict[str, object] = {
        "base_url": BASE,
        "source": "api",
        "api": {
            "enabled": True,
            "app_id": APP,
            "unclaimed_report_id": UNCLAIMED_REPORT,
            "claimed_report_id": CLAIMED_REPORT,
        },
    }
    values.update(tdx)
    return Settings(tdx=values)


def compared(when: datetime, *, primary: str = "api", mismatch: bool = False) -> dict:
    """One compared cycle's record, with ticket ids in every list when ``mismatch``."""
    ids = list(IDS) if mismatch else []
    return {
        "schema": SCHEMA,
        "at": when.isoformat(timespec="seconds"),
        "primary": primary,
        "result": "compared",
        "seconds": {"browser": 20.0, "api": 3.0},
        "api_requests": 12,
        "browser_non_get": 0,
        "rows": {
            "unclaimed": {
                "browser": 3,
                "api": 3 + len(ids[:1]),
                "matched": 3,
                "only_browser": [],
                "only_api": ids[:1],
                "differs": {"title": ids[1:2]},
            }
        },
        "feeds": {
            "compared": 2,
            "matched": 2 - len(ids[2:3]),
            "only_browser": ids[3:4],
            "only_api": [],
            "entries": {"browser": {}, "api": {}},
            "differs": {"reply_count": ids[2:3]},
        },
        "findings": {
            "browser": 1,
            "api": 1 + len(ids[4:]),
            "identical": not mismatch,
            "only_browser": [],
            "only_api": [[i, "client_waiting", "requestor_last"] for i in ids[4:]],
        },
    }


# --- the token line ----------------------------------------------------------------------


def test_a_token_expiring_monday_5_pm_at_sundays_close_is_refreshed_after_10_pm() -> None:
    line, code = token_line(make_token(at(10, 26, 17, 0)), at(10, 25, 17, 0), DESK)

    assert code == "refresh_later"
    assert line == (
        "API token expires Mon 5:00 PM, before the desk closes Mon 10:00 PM. Refresh it after "
        f"Sun 10:00 PM so it lasts the whole day: {COMMAND}."
    )


def test_at_saturdays_close_a_token_short_of_sundays_close_is_refreshed_now() -> None:
    # Next close Sunday 17:00: a token refreshed from Saturday 17:00 on lasts, so now.
    line, code = token_line(make_token(at(10, 25, 15, 0)), at(10, 24, 18, 0), DESK)

    assert code == "refresh_now"
    assert line == (
        "API token expires Sun 3:00 PM, before the desk closes Sun 5:00 PM. "
        f"Refresh it now: {COMMAND}."
    )


def test_a_fresh_token_on_a_weekday_covers_tomorrow() -> None:
    line, code = token_line(make_token(at(10, 16, 22, 14)), at(10, 15, 22, 0), DESK)

    assert code == "ok"
    assert line == "API token valid until Fri 10:14 PM; it covers tomorrow."


def test_from_friday_tomorrow_is_saturdays_window() -> None:
    line, code = token_line(make_token(at(10, 17, 21, 0)), at(10, 16, 22, 0), DESK)

    assert code == "ok"
    assert line == "API token valid until Sat 9:00 PM; it covers tomorrow."


def test_a_next_window_after_tomorrow_is_named_by_its_day() -> None:
    weekdays = DeskHours.from_settings(
        ScheduleSettings(desk_hours={d: "07:30-22:00" for d in ("mon", "tue", "fri")})
    )
    line, code = token_line(make_token(at(10, 19, 23, 0)), at(10, 16, 22, 0), weekdays)

    assert code == "ok"
    assert line == "API token valid until Mon 11:00 PM; it covers Mon."


@pytest.mark.parametrize("token", [None, make_token(at(10, 25, 16, 0))])
def test_a_missing_or_expired_token_is_refreshed_before_the_desk_opens(
    token: ApiToken | None,
) -> None:
    line, code = token_line(token, at(10, 25, 17, 0), DESK)

    assert code == "missing"
    assert line == (
        f"API token missing or expired. Refresh it before the desk opens Mon 7:30 AM: {COMMAND}."
    )


def test_the_refresh_time_counts_the_dst_change_on_2026_11_01() -> None:
    # Sunday 2026-11-01 is EST (the clocks went back at 02:00); Monday's close is 22:00 EST.
    line, code = token_line(make_token(at(11, 2, 17, 0)), at(11, 1, 17, 0), DESK)

    assert code == "refresh_later"
    assert "Refresh it after Sun 10:00 PM so it lasts the whole day" in line


# --- the notice --------------------------------------------------------------------------

THURSDAY = (at(10, 15, 7, 30), at(10, 15, 22, 0))
CLOSE = THURSDAY[1]
GOOD_TOKEN = make_token(at(10, 16, 22, 14))


def test_the_notice_leads_with_the_day_and_the_token_then_the_comparison() -> None:
    records = [compared(at(10, 15, 9, 0)), compared(at(10, 15, 9, 30))]

    text, extras = day_end_notice(api_settings(), THURSDAY, CLOSE, DESK, records, GOOD_TOKEN)

    lines = text.split("\n\n")
    assert lines[0] == (
        "Desk day done (Thu). API token valid until Fri 10:14 PM; it covers tomorrow."
    )
    assert lines[1].startswith("API primary, today: 2 cycles; 2 read through the API")
    assert "Mismatches: none." in lines
    assert DAY_END_MISMATCH_ADVICE not in text
    assert extras == {"day": "2026-10-15", "compared": 2, "mismatches": False, "token": "ok"}


def test_a_mismatch_under_the_api_adds_what_to_do() -> None:
    records = [compared(at(10, 15, 9, 0), mismatch=True)]

    text, extras = day_end_notice(api_settings(), THURSDAY, CLOSE, DESK, records, GOOD_TOKEN)

    assert text.split("\n\n")[-1] == DAY_END_MISMATCH_ADVICE
    assert extras["mismatches"] is True


def test_a_mismatch_under_shadow_needs_no_advice() -> None:
    records = [compared(at(10, 15, 9, 0), primary="browser", mismatch=True)]
    settings = api_settings(source="shadow")

    text, extras = day_end_notice(settings, THURSDAY, CLOSE, DESK, records, GOOD_TOKEN)

    assert "API shadow, today: 1 cycle" in text
    assert extras["mismatches"] is True
    assert DAY_END_MISMATCH_ADVICE not in text


def test_the_notice_never_shows_a_ticket_id() -> None:
    records = [
        compared(at(10, 15, 9, 0), mismatch=True),
        compared(at(10, 15, 9, 30), primary="browser", mismatch=True),
    ]

    text, extras = day_end_notice(api_settings(), THURSDAY, CLOSE, DESK, records, GOOD_TOKEN)

    assert "Mismatches: " in text
    for ticket_id in IDS:
        assert str(ticket_id) not in text
        assert str(ticket_id) not in json.dumps(extras)


def test_without_records_or_the_api_the_notice_says_so() -> None:
    settings = api_settings(api={"enabled": False})

    text, extras = day_end_notice(settings, THURSDAY, CLOSE, DESK, [], None)

    assert text == f"Desk day done (Thu).\n\n{DAY_END_NO_RECORDS}"
    assert extras == {"day": "2026-10-15", "compared": 0, "mismatches": False, "token": "off"}


# --- the state file ----------------------------------------------------------------------


def test_the_state_file_holds_the_last_day_only(tmp_path) -> None:
    path = tmp_path / "tdx" / "day-end-state.json"
    assert DayEndState(path).last_sent is None

    DayEndState(path).record(date(2026, 10, 15))

    assert json.loads(path.read_text(encoding="utf-8")) == {"last_sent": "2026-10-15"}
    assert DayEndState(path).last_sent == date(2026, 10, 15)


@pytest.mark.parametrize("text", ["not json", "{}", '{"last_sent": "Thursday"}', "[]"])
def test_an_unreadable_state_file_counts_as_never_sent(tmp_path, text: str) -> None:
    path = tmp_path / "day-end-state.json"
    path.write_text(text, encoding="utf-8")

    assert DayEndState(path).last_sent is None


def test_the_state_file_sits_in_the_tdx_data_folder() -> None:
    settings = api_settings()
    path = day_end_state_path(settings)
    assert path.name == "day-end-state.json"
    assert path.parent == watch_module.summary_state_path(settings, "desk").parent


# --- the loop ----------------------------------------------------------------------------


class Clock:
    """Fake time: ``sleep`` advances ``now``; ``jumps`` move it on (a computer waking up)."""

    def __init__(self, start: datetime, stop_at: datetime) -> None:
        self.now = start
        self.stop_at = stop_at
        self.jumps: dict[datetime, datetime] = {}

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= 30
        self.now += timedelta(seconds=seconds)
        for when, to in list(self.jumps.items()):
            if self.now >= when:
                self.now = max(self.now, to)
                del self.jumps[when]
        if self.now >= self.stop_at:
            raise KeyboardInterrupt


def empty_run(now: datetime) -> AuditRun:
    dashboard = DashboardRead(
        widgets={"unclaimed": WidgetRead("unclaimed", (), 1, (), False)}, blocked=()
    )
    feeds = FeedRead(feeds={}, failed=(), deferred=(), blocked=(), allowed_non_get=())
    return AuditRun(dashboard, feeds, AuditResult((), {"rows_unclaimed": 0}), (), now)


class Loop:
    """One ``watch`` run with fake cycles that post nothing, and a fake token reader."""

    def __init__(self, clock: Clock, token: ApiToken | Exception | None = GOOD_TOKEN) -> None:
        self.clock = clock
        self.token = token
        self.times: list[datetime] = []
        self.token_reads: list[datetime] = []
        self.notices: list[str] = []

    def cycle(self, summary: object = None) -> tuple[AuditRun, SendOutcome]:
        self.times.append(self.clock.now)
        if summary is not None:
            return empty_run(self.clock.now), SendOutcome("sent", summary=True)
        return empty_run(self.clock.now), SendOutcome("nothing_new")

    def read_token(self, now: datetime) -> ApiToken | None:
        self.token_reads.append(now)
        if isinstance(self.token, Exception):
            raise self.token
        return self.token

    def run(self, settings: Settings, **kwargs: object) -> int:
        kwargs.setdefault("notify", self.notices.append)
        return watch(
            settings,
            clock=self.clock,
            sleep=self.clock.sleep,
            run_cycle=self.cycle,
            day_end=self.read_token,
            **kwargs,
        )


@pytest.fixture
def webhook() -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])


def day_end_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "Day-end notice" in line]


@pytest.mark.usefixtures("webhook")
def test_the_last_cycle_wakes_at_the_close_sends_one_notice_then_sleeps_to_the_open(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    settings = api_settings()
    log = ShadowLog(shadow_log_path(settings))
    log.append(compared(at(10, 14, 21, 0)), at(10, 14, 21, 0))  # Wednesday: not today
    log.append(compared(at(10, 15, 21, 10)), at(10, 15, 21, 10))
    clock = Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 7, 31))
    loop = Loop(clock)

    with caplog.at_level(logging.INFO, logger="ops_suite"):
        assert loop.run(settings) == 0

    assert loop.times == [at(10, 15, 21, 40), at(10, 16, 7, 30)]
    assert loop.token_reads == [at(10, 15, 22, 0)]
    out = capsys.readouterr().out.splitlines()
    cycle = next(line for line in out if line.startswith("21:40 "))
    assert cycle.endswith("Next run 22:00.")
    after = out[out.index(cycle) + 1 :]
    assert after[:2] == ["22:00 Day-end notice sent.", "Outside desk hours; next run Fri 07:30."]
    assert len(loop.notices) == 1
    notice = loop.notices[0].split("\n\n")
    assert (
        notice[0] == "Desk day done (Thu). API token valid until Fri 10:14 PM; it covers tomorrow."
    )
    assert notice[1].startswith("API primary, today: 1 cycle;")
    assert DayEndState(day_end_state_path(settings)).last_sent == date(2026, 10, 15)
    [record] = [r for r in caplog.records if r.getMessage() == "tdx watch day-end notice"]
    assert (record.day, record.compared, record.mismatches, record.token) == (
        "2026-10-15",
        1,
        False,
        "ok",
    )


@pytest.mark.usefixtures("webhook")
def test_a_summary_slot_before_the_close_still_comes_first() -> None:
    settings = api_settings(
        summary={"enabled": True, "destination": "personal", "times": {"thu": ["21:50"]}}
    )
    clock = Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 7, 31))
    loop = Loop(clock)

    loop.run(settings)

    assert loop.times == [at(10, 15, 21, 40), at(10, 15, 21, 50), at(10, 16, 7, 30)]
    assert loop.token_reads == [at(10, 15, 22, 0)]
    assert len(loop.notices) == 1


@pytest.mark.usefixtures("webhook")
def test_a_restart_after_the_notice_was_sent_sends_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = api_settings()
    DayEndState(day_end_state_path(settings)).record(date(2026, 10, 15))
    loop = Loop(Clock(at(10, 15, 23, 10), stop_at=at(10, 16, 7, 31)))

    loop.run(settings)

    assert loop.notices == []
    assert day_end_lines(capsys.readouterr().out) == []


@pytest.mark.usefixtures("webhook")
def test_a_restart_after_the_close_sends_the_days_notice_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = api_settings()
    first = Loop(Clock(at(10, 15, 23, 10), stop_at=at(10, 15, 23, 30)))
    first.run(settings)
    second = Loop(Clock(at(10, 15, 23, 40), stop_at=at(10, 16, 7, 31)))
    second.run(settings)

    assert len(first.notices) == 1
    assert first.notices[0].startswith("Desk day done (Thu).")
    assert second.notices == []
    assert day_end_lines(capsys.readouterr().out) == ["23:10 Day-end notice sent."]


@pytest.mark.usefixtures("webhook")
def test_a_computer_asleep_over_the_close_sends_the_notice_when_it_wakes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 7, 31))
    clock.jumps[at(10, 15, 21, 41)] = at(10, 15, 23, 10)  # asleep from 21:41 to 23:10
    loop = Loop(clock)

    loop.run(api_settings())

    assert len(loop.notices) == 1
    assert day_end_lines(capsys.readouterr().out) == ["23:10 Day-end notice sent."]


@pytest.mark.usefixtures("webhook")
def test_a_day_slept_through_until_the_next_open_gets_no_late_notice() -> None:
    clock = Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 8, 31))
    clock.jumps[at(10, 15, 21, 41)] = at(10, 16, 8, 0)  # asleep until Friday's desk hours
    loop = Loop(clock)

    loop.run(api_settings())

    assert loop.times == [at(10, 15, 21, 40), at(10, 16, 8, 0), at(10, 16, 8, 30)]
    assert loop.notices == []


@pytest.mark.usefixtures("webhook")
@pytest.mark.parametrize("case", ["setting off", "browser source", "defaults"])
def test_no_notice_and_no_close_wake_when_off_or_reading_with_the_browser(
    case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Built inside the test, after the fixtures keep the real settings away.
    settings = {
        "setting off": lambda: api_settings(watch={"day_end_notice": False}),
        "browser source": lambda: api_settings(source="browser"),
        "defaults": lambda: Settings(tdx={"base_url": BASE}),
    }[case]()
    loop = Loop(Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 7, 31)))

    loop.run(settings)

    assert loop.notices == []
    assert loop.token_reads == []
    out = capsys.readouterr().out
    assert day_end_lines(out) == []
    assert any(line.endswith("Next run 07:30.") for line in out.splitlines())
    assert not day_end_state_path(settings).exists()


@pytest.mark.usefixtures("webhook")
def test_a_failing_notice_is_logged_and_never_stops_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = api_settings()
    loop = Loop(Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 7, 31)), token=RuntimeError("boom"))

    with caplog.at_level(logging.INFO, logger="ops_suite"):
        code = loop.run(settings)

    assert code == 0
    assert loop.times == [at(10, 15, 21, 40), at(10, 16, 7, 30)]  # the loop went on
    assert loop.notices == []
    [failed] = [r for r in caplog.records if r.getMessage() == "tdx watch day-end notice failed"]
    assert failed.reason == "RuntimeError"
    assert "boom" not in caplog.text
    assert not day_end_state_path(settings).exists()  # tried again on the next closed wake


def test_a_desk_watch_sends_the_notice_to_the_personal_chat_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    set_webhook_url(DESK_WEBHOOK, [".logic.azure.com"], "desk")
    settings = api_settings().model_copy(
        update={"teams": Settings(teams={"desk": {"approved": True}}).teams}
    )
    posts: list[tuple[str, dict]] = []

    def post(url: str, card: dict, *, timeout: float) -> int:
        posts.append((url, card))
        return 202

    monkeypatch.setattr(watch_module, "post_card", post)
    loop = Loop(Clock(at(10, 15, 21, 40), stop_at=at(10, 16, 7, 31)))

    loop.run(settings, notify=webhook_notifier(settings), destination="desk")

    assert [url for url, _ in posts] == [WEBHOOK]
    body = posts[0][1]["body"]
    assert body[0]["text"] == "ops-suite: TDX auditor"
    assert body[1]["text"].startswith("Desk day done (Thu).")
