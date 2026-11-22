"""Confirming a staff requester by account: the setting, the API reader's yes or no, the rules
and ``tdx api check``. Invented people and addresses, synthetic answers, no network."""

import functools
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_fakes import (
    API,
    APP,
    BASE,
    NOW,
    TZ,
    Answer,
    FakeApi,
    api_client,
    api_tdx,
    entry,
    iso,
    make_token,
    report,
)
from pydantic import ValidationError

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common.config import Settings, TdxRulesSettings, TdxSettings, load_settings
from ops_suite.tdx import api_source, watch
from ops_suite.tdx.api_client import ApiError, TdxApiClient
from ops_suite.tdx.dashboard import DashboardRead, WidgetRead
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import RuleSettings, evaluate, format_audit_summary, staff_name_tickets
from ops_suite.tdx.tickets import FeedRead

STAFF = "Max Manager"
TWIN_ADDRESS = "sentinel.twin@example.invalid"
STAFF_ADDRESS = "sentinel.staff@example.invalid"
ACCOUNTS = {STAFF: STAFF_ADDRESS}
SENTINELS = ("sentinel", TWIN_ADDRESS, STAFF_ADDRESS)
UNCLAIMED = ("ID", "Created", "Title", "Requestor", "Resp Group")
CLAIMED = ("ID", "Modified", "Title", "Requestor", "Resp Group", "Status", "Prim Resp")
OLD = NOW - timedelta(days=4)


def _row(ticket_id: int, requestor: str = STAFF, widget: str = "unclaimed") -> TicketRow:
    claimed = widget == "claimed"
    return TicketRow(
        widget=widget,  # type: ignore[arg-type]
        ticket_id=ticket_id,
        title="Printer offline",
        requestor=requestor,
        resp_group="Example Group",
        created=OLD,
        modified=OLD if claimed else None,
        prim_resp="Robin Fulltime" if claimed else None,
        status="Open" if claimed else None,
        detail_url=None,
    )


def _feeds(*ticket_ids: int) -> dict[int, TicketFeed]:
    return {ticket_id: TicketFeed(ticket_id=ticket_id, entries=()) for ticket_id in ticket_ids}


def _ticket(ticket_id: int, **fields: object) -> tuple[str, dict[str, object]]:
    return f"{API}/{APP}/tickets/{ticket_id}", {"ID": ticket_id, "AppID": APP, **fields}


def _unconfirmed(api: FakeApi, rows: list[TicketRow], feeds: tuple[int, ...]) -> frozenset[int]:
    return api_source.unconfirmed_staff_requesters(api_client(api), APP, rows, feeds, ACCOUNTS)


# --- the setting --------------------------------------------------------------------------


def test_the_setting_is_read_from_the_environment_and_repr_shows_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPS_SUITE_TDX__RULES__STAFF_ACCOUNTS", f'{{"{STAFF}": " {STAFF_ADDRESS} "}}'
    )

    tdx = load_settings().tdx

    assert tdx.rules.staff_accounts == {STAFF: STAFF_ADDRESS}
    assert "staff_accounts=1" in repr(tdx.rules)
    rules = watch.rule_settings(tdx)
    assert rules.staff_account_names == (STAFF,)
    for shown in (repr(tdx), str(tdx), repr(tdx.rules), repr(rules)):
        assert "sentinel" not in shown
        assert STAFF not in shown


@pytest.mark.parametrize(
    ("accounts", "message"),
    [
        ({STAFF: "sentinel-not-an-address"}, "entry 1 is not an email address"),
        ({STAFF: 5}, "entry 1 is not an email address"),
        ({"Robin Fulltime": STAFF_ADDRESS, "   ": TWIN_ADDRESS}, "entry 2 has a blank name"),
        ({STAFF: STAFF_ADDRESS, f" {STAFF} ": TWIN_ADDRESS}, "a name is listed twice"),
        ("sentinel", "must map display names"),
    ],
)
def test_a_bad_entry_is_refused_without_repeating_it(accounts: object, message: str) -> None:
    for build in (
        lambda: TdxRulesSettings(staff_accounts=accounts),
        lambda: TdxSettings(rules={"staff_accounts": accounts}),
        lambda: Settings(tdx={"rules": {"staff_accounts": accounts}}),
    ):
        with pytest.raises(ValidationError) as raised:
            build()
        text = str(raised.value)
        assert message in text
        assert "sentinel" not in text and STAFF not in text


def test_the_cli_message_for_a_bad_entry_repeats_neither_name_nor_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPS_SUITE_TDX__RULES__STAFF_ACCOUNTS", f'{{"{STAFF}": "sentinel"}}')

    assert main(["tdx", "api", "check"]) == 2

    err = capsys.readouterr().err
    assert "tdx.rules.staff_accounts" in err
    assert "sentinel" not in err and STAFF not in err


def test_another_settings_error_does_not_repeat_the_addresses() -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(
            tdx={
                "rules": {"staff_accounts": ACCOUNTS},
                "summary": {"enabled": True, "times": {"mon": ["03:00"]}},
            }
        )
    assert "outside desk hours" in str(raised.value)
    assert "sentinel" not in str(raised.value)


# --- the API reader's yes or no -----------------------------------------------------------


def test_a_matching_email_confirms_whatever_its_case_and_spaces() -> None:
    path, ticket = _ticket(1, RequestorEmail=f"  {STAFF_ADDRESS.upper()} ")

    assert _unconfirmed(FakeApi({path: ticket}), [_row(1)], (1,)) == frozenset()


@pytest.mark.parametrize(
    "fields",
    [
        {"RequestorEmail": TWIN_ADDRESS},
        {},
        {"RequestorEmail": ""},
        {"RequestorEmail": "   "},
        {"RequestorEmail": None},
        {"RequestorEmail": 5},
        {"RequestorEmail": [STAFF_ADDRESS]},
    ],
)
def test_a_different_missing_empty_or_odd_email_is_unconfirmed(fields: dict[str, object]) -> None:
    path, ticket = _ticket(1, **fields)

    assert _unconfirmed(FakeApi({path: ticket}), [_row(1)], (1,)) == {1}


def test_an_answer_that_is_not_a_record_is_unconfirmed() -> None:
    assert _unconfirmed(FakeApi({f"{API}/{APP}/tickets/1": [1]}), [_row(1)], (1,)) == {1}


@pytest.mark.parametrize("status", [403, 404, 500])
def test_one_unreadable_ticket_is_unconfirmed_and_the_run_continues(status: int) -> None:
    good_path, good = _ticket(2, RequestorEmail=STAFF_ADDRESS)
    api = FakeApi({f"{API}/{APP}/tickets/1": Answer(status), good_path: good})

    assert _unconfirmed(api, [_row(1), _row(2)], (1, 2)) == {1}


def test_every_unreadable_ticket_is_unconfirmed_without_stopping_the_run() -> None:
    rows = [_row(ticket_id) for ticket_id in (1, 2, 3, 4)]

    assert _unconfirmed(FakeApi(), rows, (1, 2, 3, 4)) == {1, 2, 3, 4}


def test_a_refused_token_still_stops_the_run() -> None:
    api = FakeApi({f"{API}/{APP}/tickets/1": Answer(403)})
    api.routes[f"{API}/{APP}/tickets/2"] = Answer(401)

    with pytest.raises(ApiError) as raised:
        _unconfirmed(api, [_row(1), _row(2)], (1, 2))

    assert raised.value.reason == "unauthorised"
    assert "sentinel" not in str(raised.value)


def test_only_unclaimed_rows_with_a_read_feed_and_a_listed_name_are_asked() -> None:
    path, ticket = _ticket(1, RequestorEmail=STAFF_ADDRESS)
    api = FakeApi({path: ticket})
    rows = [
        _row(1),
        _row(2),  # no feed read
        _row(3, requestor="Alex Example"),  # not listed
        _row(4, widget="claimed"),
    ]

    assert _unconfirmed(api, rows, (1, 3, 4)) == frozenset()
    assert api.paths() == [path]


def test_no_accounts_means_no_request() -> None:
    api = FakeApi()

    found = api_source.unconfirmed_staff_requesters(api_client(api), APP, [_row(1)], (1,), {})

    assert found == frozenset()
    assert api.calls == []


def _queue_routes(requestor: str, **fields: object) -> dict[str, object]:
    row = {
        "ID": 1,
        "Created": iso(OLD),
        "Title": "Printer offline",
        "Requestor": requestor,
        "Resp Group": "Example Group",
    }
    path, ticket = _ticket(1, **fields)
    return {
        f"{API}/reports/11": report(UNCLAIMED, [row]),
        f"{API}/reports/12": report(CLAIMED, []),
        f"{API}/{APP}/tickets/1/feed": [],
        path: ticket,
    }


def _queue_tdx(accounts: dict[str, str]) -> TdxSettings:
    tdx = api_tdx()
    return tdx.model_copy(update={"rules": TdxRulesSettings(staff_accounts=accounts)})


def test_an_empty_setting_or_an_unlisted_requester_adds_no_request() -> None:
    for requestor, accounts in ((STAFF, {}), ("Alex Example", ACCOUNTS)):
        api = FakeApi(_queue_routes(requestor, RequestorEmail=STAFF_ADDRESS))
        tdx = _queue_tdx(accounts)
        client = api_client(api, tdx)

        queue = api_source.read_queue(
            tdx, client, NOW, watch.rule_settings(tdx), watch.form_options(tdx)
        )

        assert queue.not_staff_requesters == frozenset()
        assert f"{API}/{APP}/tickets/1" not in api.paths()
        assert sum(queue.requests.values()) == 3  # two reports and one feed


def test_the_queue_carries_the_answer_and_counts_the_request() -> None:
    api = FakeApi(_queue_routes(STAFF, RequestorEmail=TWIN_ADDRESS))
    tdx = _queue_tdx(ACCOUNTS)

    queue = api_source.read_queue(
        tdx, api_client(api, tdx), NOW, watch.rule_settings(tdx), watch.form_options(tdx)
    )

    assert queue.not_staff_requesters == {1}
    assert sum(queue.requests.values()) == 4
    for text in (repr(queue.not_staff_requesters), repr(queue.feeds), repr(queue.rows)):
        assert "sentinel" not in text


def test_no_address_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    path, ticket = _ticket(1, RequestorEmail=TWIN_ADDRESS)
    api = FakeApi({path: ticket, f"{API}/{APP}/tickets/2": Answer(404)})

    assert _unconfirmed(api, [_row(1), _row(2)], (1, 2)) == {1, 2}

    assert caplog.records
    for record in caplog.records:
        assert "sentinel" not in record.getMessage()
        assert "sentinel" not in repr(record.__dict__)


# --- the rules ----------------------------------------------------------------------------


def _evaluate(
    rules: RuleSettings,
    not_staff: frozenset[int] = frozenset(),
    known_desk: object = None,
    feed: tuple[FeedEntry, ...] = (),
) -> object:
    return evaluate(
        [_row(1)],
        {1: TicketFeed(ticket_id=1, entries=feed)},
        NOW,
        rules,
        TZ,
        known_desk=known_desk,  # type: ignore[arg-type]
        not_staff_requesters=not_staff,
    )


def test_a_confirmed_listed_name_is_internal() -> None:
    result = _evaluate(RuleSettings(staff_account_names=(STAFF,)))

    assert result.findings == ()
    assert result.counts["internal_requestor"] == 1
    assert result.counts["staff_name_unconfirmed"] == 0
    assert result.counts["desk_members_known"] == 1


@pytest.mark.parametrize(
    ("rules", "known_desk"),
    [
        (RuleSettings(staff_account_names=(STAFF,)), None),
        (RuleSettings(staff_account_names=(STAFF,), extra_staff_names=(STAFF,)), None),
        (RuleSettings(staff_account_names=(STAFF,)), lambda name: True),
    ],
)
def test_an_unconfirmed_requester_raises_no_reply_yet(
    rules: RuleSettings, known_desk: object
) -> None:
    result = _evaluate(rules, frozenset({1}), known_desk)

    assert [(f.kind, f.reason) for f in result.findings] == [("client_waiting", "no_reply_yet")]
    assert result.counts["internal_requestor"] == 0
    assert result.counts["staff_name_unconfirmed"] == 1


def test_an_unconfirmed_requester_who_wrote_last_is_waiting() -> None:
    feed = (FeedEntry(author=STAFF, created=NOW - timedelta(days=3), kind="reply"),)

    result = _evaluate(RuleSettings(staff_account_names=(STAFF,)), frozenset({1}), feed=feed)

    assert [(f.kind, f.reason) for f in result.findings] == [("client_waiting", "requestor_last")]


def test_a_claimed_ticket_is_not_counted_as_unconfirmed() -> None:
    result = evaluate(
        [_row(1, widget="claimed")],
        {},
        NOW,
        RuleSettings(staff_account_names=(STAFF,)),
        TZ,
        not_staff_requesters={1},
    )

    assert result.counts["staff_name_unconfirmed"] == 0


def test_the_summary_line_appears_only_when_the_count_is_not_zero() -> None:
    line = "Requester shares a staff name, not confirmed: 1."
    rules = RuleSettings(staff_account_names=(STAFF,))

    assert line in " ".join(format_audit_summary(_evaluate(rules, frozenset({1})), show_ids=False))
    assert "shares a staff name" not in " ".join(
        format_audit_summary(_evaluate(rules), show_ids=False)
    )


def test_staff_name_tickets_are_unclaimed_rows_with_a_listed_name() -> None:
    rows = [_row(1), _row(2, "Manager, Max"), _row(3, "Alex Example"), _row(4, widget="claimed")]

    assert staff_name_tickets(rows, (STAFF,)) == {1, 2}
    assert staff_name_tickets(rows, ()) == frozenset()


# --- a browser read -----------------------------------------------------------------------


def _browser_judged(accounts: dict[str, str], tmp_path: Path) -> object:
    settings = Settings(
        paths={"data_dir": tmp_path, "log_dir": tmp_path},
        tdx={"rules": {"staff_accounts": accounts, "desk_memory": False}},
    )
    widget = WidgetRead(
        kind="unclaimed", rows=(_row(1),), pages=1, unreadable_dates=(), hit_page_limit=False
    )
    feeds = FeedRead(
        feeds=_feeds(1),
        failed=(),
        deferred=(),
        blocked=(),
        allowed_non_get=(),
        unreadable_shapes=(),
    )
    dashboard = DashboardRead(widgets={"unclaimed": widget}, blocked=())
    return watch._judged(settings, dashboard, feeds, [_row(1)], NOW.astimezone(TZ)).result


def test_a_browser_read_leaves_a_listed_name_unconfirmed(tmp_path: Path) -> None:
    result = _browser_judged(ACCOUNTS, tmp_path)

    assert [f.reason for f in result.findings] == ["no_reply_yet"]
    assert result.counts["internal_requestor"] == 0
    assert result.counts["staff_name_unconfirmed"] == 1


def test_a_browser_read_with_no_setting_is_unchanged(tmp_path: Path) -> None:
    result = _browser_judged({}, tmp_path)

    assert result.counts["staff_name_unconfirmed"] == 0
    assert [f.reason for f in result.findings] == ["no_reply_yet"]


# --- tdx api check ------------------------------------------------------------------------


def _check_routes(rows: int = 1, **fields: object) -> dict[str, object]:
    created = datetime.now(UTC) - timedelta(days=4)
    row = {
        "ID": 1,
        "Created": created.isoformat(),
        "Title": "Printer offline",
        "Requestor": "Alex Example",
        "Resp Group": "Example Group",
    }
    claimed = {
        "ID": 2,
        "Modified": created.isoformat(),
        "Title": "Laptop repair",
        "Requestor": "Sam Synthetic",
        "Resp Group": "Example Group",
        "Status": "In Process",
        "Prim Resp": "Robin Fulltime",
    }
    path, ticket = _ticket(1, **fields)
    return {
        f"{API}/auth/getuser": {"FullName": "Kim Owner", "SecurityRoleName": "Technician"},
        f"{API}/reports/11": report(UNCLAIMED, [row] if rows else []),
        f"{API}/reports/12": report(CLAIMED, [claimed]),
        f"{API}/{APP}/tickets/1/feed": [entry(1, "Alex Example", created, "Any news?")],
        f"{API}/{APP}/tickets/2/feed": [],
        path: ticket,
    }


def _run_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: FakeApi,
    accounts: str | None,
) -> str:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", BASE)
    monkeypatch.setenv("OPS_SUITE_TDX__API__ENABLED", "true")
    monkeypatch.setenv("OPS_SUITE_TDX__API__APP_ID", str(APP))
    monkeypatch.setenv("OPS_SUITE_TDX__API__UNCLAIMED_REPORT_ID", "11")
    monkeypatch.setenv("OPS_SUITE_TDX__API__CLAIMED_REPORT_ID", "12")
    if accounts is not None:
        monkeypatch.setenv("OPS_SUITE_TDX__RULES__STAFF_ACCOUNTS", accounts)
    token = make_token(datetime.now(UTC) + timedelta(hours=20))
    client = functools.partial(TdxApiClient, opener=api, sleep=lambda seconds: None)
    for module in (cli, watch):
        monkeypatch.setattr(module, "TdxApiClient", client)
        monkeypatch.setattr(module, "browser_fetcher", lambda settings: lambda: token)

    assert main(["tdx", "api", "check"]) == 0

    captured = capsys.readouterr()
    assert "sentinel" not in captured.out + captured.err
    return captured.out


ACCOUNTS_ENV = f'{{"{STAFF}": "{STAFF_ADDRESS}"}}'


@pytest.mark.parametrize(
    ("fields", "shown"),
    [
        ({"RequestorEmail": TWIN_ADDRESS}, "yes"),
        ({"RequestorEmail": "  "}, "no"),
        ({}, "no"),
        ({"RequestorEmail": 5}, "no"),
    ],
)
def test_api_check_says_whether_the_email_is_readable(
    fields: dict[str, object],
    shown: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    out = _run_check(monkeypatch, capsys, FakeApi(_check_routes(**fields)), ACCOUNTS_ENV)

    assert f"Staff accounts: 1 listed; requester email readable: {shown}." in out
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    assert "sentinel" not in log_text


def test_api_check_with_an_unreadable_ticket_says_no(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _check_routes()
    routes[f"{API}/{APP}/tickets/1"] = Answer(404)

    out = _run_check(monkeypatch, capsys, FakeApi(routes), ACCOUNTS_ENV)

    assert "requester email readable: no." in out


def test_api_check_with_no_unclaimed_ticket_says_not_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(_check_routes(rows=0))

    out = _run_check(monkeypatch, capsys, api, ACCOUNTS_ENV)

    assert "Staff accounts: 1 listed; requester email readable: not checked." in out
    assert f"{API}/{APP}/tickets/1" not in api.paths()


def test_api_check_with_no_setting_prints_no_line_and_reads_no_ticket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(_check_routes(RequestorEmail=TWIN_ADDRESS))

    out = _run_check(monkeypatch, capsys, api, None)

    assert "Staff accounts" not in out
    assert f"{API}/{APP}/tickets/1" not in api.paths()
