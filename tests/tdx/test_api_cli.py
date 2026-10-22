"""The TDX Web API commands: tdx api token / check / reports, --source api on tdx rows and
tdx audit, and the token after tdx login. Synthetic answers through a fake opener; no browser
and no network."""

import functools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_fakes import (
    API,
    APP,
    BASE,
    TZ,
    Answer,
    FakeApi,
    demo_routes,
    entry,
    make_token,
    report,
)

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common.config import load_settings
from ops_suite.tdx import watch
from ops_suite.tdx.api_client import TdxApiClient
from ops_suite.tdx.api_token import ApiToken, TokenError, load_token, save_token
from ops_suite.tdx.demo.dataset import demo_tickets
from ops_suite.tdx.session_log import record_sign_in, session_log_path

UNCLAIMED = ("ID", "Created", "Title", "Requestor", "Resp Group")
CLAIMED = ("ID", "Modified", "Title", "Requestor", "Resp Group", "Status", "Prim Resp")
REQUESTER = "Alex Example"
DESK = "Pat Helpdesk"
ROLE = "Technician"


class Fetcher:
    """Stands in for the headless browser that asks for a token."""

    def __init__(self, token: ApiToken | Exception) -> None:
        self.token = token
        self.calls = 0

    def __call__(self, settings: object) -> "Fetcher":
        return self

    def fetch(self) -> ApiToken:
        self.calls += 1
        if isinstance(self.token, Exception):
            raise self.token
        return self.token


@pytest.fixture
def api_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", BASE)
    monkeypatch.setenv("OPS_SUITE_TDX__API__ENABLED", "true")
    monkeypatch.setenv("OPS_SUITE_TDX__API__APP_ID", str(APP))
    monkeypatch.setenv("OPS_SUITE_TDX__API__UNCLAIMED_REPORT_ID", "11")
    monkeypatch.setenv("OPS_SUITE_TDX__API__CLAIMED_REPORT_ID", "12")
    return monkeypatch


def _use(
    env: pytest.MonkeyPatch, api: FakeApi, token: ApiToken | Exception | None = None
) -> Fetcher:
    """Route every API client to ``api`` and every token request to a fake fetcher."""
    expires = datetime.now(UTC) + timedelta(hours=20)
    fetcher = Fetcher(token or make_token(expires))
    client = functools.partial(TdxApiClient, opener=api, sleep=lambda seconds: None)
    for module in (cli, watch):
        env.setattr(module, "TdxApiClient", client)
        env.setattr(module, "browser_fetcher", lambda settings: fetcher.fetch)
    return fetcher


def _routes() -> dict[str, object]:
    created = datetime.now(UTC) - timedelta(days=4)
    row = {
        "ID": 5501001,
        "Created": created.isoformat(),
        "Title": "Printer offline",
        "Requestor": REQUESTER,
        "Resp Group": "IT Service Desk",
    }
    claimed = {
        "ID": 4409100,
        "Modified": created.isoformat(),
        "Title": "Laptop repair",
        "Requestor": "Sam Synthetic",
        "Resp Group": "IT Service Desk",
        "Status": "In Process",
        "Prim Resp": "Robin Fulltime",
    }
    return {
        f"{API}/auth/getuser": {"FullName": "Kim Owner", "SecurityRoleName": ROLE},
        f"{API}/reports/11": report(UNCLAIMED, [row]),
        f"{API}/reports/12": report(CLAIMED, [claimed]),
        f"{API}/{APP}/tickets/5501001/feed": [
            entry(2, REQUESTER, datetime.now(UTC) - timedelta(days=3), "Any news?"),
            entry(1, DESK, datetime.now(UTC) - timedelta(days=4), "Looking into it.", replies=1),
        ],
        f"{API}/feed/1": {
            "ID": 1,
            "Replies": [
                {
                    "ID": 3,
                    "CreatedFullName": DESK,
                    "CreatedDate": created.isoformat(),
                    "Body": "Note",
                    "IsRichHtml": True,
                }
            ],
        },
        f"{API}/reports": [
            {
                "ID": 12,
                "Name": "Claimed by others",
                "ReportSourceName": "Tickets",
                "CreatedFullName": "Kim Owner",
            },
            {"ID": 11, "Name": "Unclaimed in my groups", "ReportSourceName": "Tickets"},
        ],
    }


NAMES = (REQUESTER, DESK, "Kim Owner", "Sam Synthetic", "Robin Fulltime", "Printer offline")


def _no_names(*texts: str) -> None:
    for text in texts:
        for name in NAMES:
            assert name not in text


# --- the switch ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ["api", "token"],
        ["api", "check"],
        ["api", "reports"],
        ["rows", "--source", "api"],
        ["audit", "--source", "api"],
    ],
)
def test_the_api_commands_need_tdx_api_enabled(
    command: list[str], api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__API__ENABLED", "false")
    api = FakeApi(_routes())
    fetcher = _use(api_env, api)

    assert main(["tdx", *command]) == 2

    assert "tdx.api.enabled is false" in capsys.readouterr().err
    assert api.calls == [] and fetcher.calls == 0


def test_an_unknown_api_setting_is_refused(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__API__UNCLAIMED_REPORT", "11")

    assert main(["tdx", "api", "check"]) == 2
    assert "unknown setting tdx.api.unclaimed_report" in capsys.readouterr().err


# --- tdx api token ------------------------------------------------------------------------


def test_api_token_saves_it_and_prints_only_the_expiry(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = make_token(datetime(2026, 10, 22, 13, 14, tzinfo=UTC))
    _use(api_env, FakeApi(), token)

    code = main(["tdx", "api", "token"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "TDX API token: saved, expires Thu 09:14.\n"
    assert load_token(datetime(2026, 10, 21, tzinfo=UTC)) == token
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    for text in (captured.out, captured.err, log_text):
        assert token.value not in text and token.value.split(".")[1] not in text


NEEDS_SIGN_IN = 'TDX API token: needs sign-in; run "uv run ops-suite tdx api token --headed".'


@pytest.mark.parametrize(
    "command",
    [
        ["api", "token"],
        ["api", "check"],
        ["rows", "--source", "api"],
        ["audit", "--source", "api"],
    ],
)
def test_a_token_that_needs_a_sign_in_is_never_an_expired_session(
    command: list[str], api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The headless refresh reached the identity provider: the API's own sign-in, while the
    TDX session may be fine. Exit 3 and a line naming --headed; the session log is untouched."""
    log_path = session_log_path(load_settings())
    record_sign_in(load_settings(), datetime.now(UTC) - timedelta(hours=2))
    before = log_path.read_bytes()
    _use(api_env, FakeApi(_routes()), TokenError("needs_sign_in"))

    assert main(["tdx", *command]) == 3

    err = capsys.readouterr().err
    assert err.splitlines()[-1] == NEEDS_SIGN_IN
    assert "expired" not in err
    assert log_path.read_bytes() == before


def test_api_token_lists_requests_the_guard_refused(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _use(api_env, FakeApi(), TokenError("blocked_request", blocked=("POST /SSO/Acs/#",)))

    assert main(["tdx", "api", "token"]) == 8
    err = capsys.readouterr().err
    assert "TDX API token: blocked_request" in err
    assert "Blocked: POST /SSO/Acs/# (1)" in err
    assert "tdx.allowed_requests" in err


# --- tdx api check / reports ------------------------------------------------------------


def test_api_check_prints_the_role_counts_and_requests_only(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    api = FakeApi(_routes())
    _use(api_env, api)

    code = main(["tdx", "api", "check"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    lines = captured.out.splitlines()
    assert lines[0] == f"API: signed in as {ROLE}."
    assert lines[1].startswith("TDX API token: saved, expires ")
    assert lines[2:] == [
        "Unclaimed report: 1 row(s), 5 column(s), every needed column found.",
        "Claimed report: 1 row(s), 7 column(s), every needed column found.",
        f"App id: {APP} (tdx.api.app_id).",
        "One feed: 3 entries (3 reply, 0 private, 0 system); 1 of them replies, 0 unreadable.",
        "API requests: 5 (GET /auth/getuser 1, GET /reports/{n} 2, GET /{n}/tickets/{n}/feed 1, "
        "GET /feed/{n} 1).",
    ]
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    _no_names(captured.out, captured.err, log_text)
    assert {method for method, _, _ in api.calls} == {"GET"}


def test_api_check_derives_the_app_id_and_says_so(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__API__APP_ID", "0")
    routes = _routes()
    routes[f"{API}/tickets/5501001"] = {"ID": 5501001, "AppID": APP}
    _use(api_env, FakeApi(routes))

    assert main(["tdx", "api", "check"]) == 0
    assert (
        f"App id: {APP} (from the first ticket's record; set tdx.api.app_id to save that request)."
        in capsys.readouterr().out
    )


def test_api_check_with_a_report_id_unset_says_so_and_exits_2(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__API__CLAIMED_REPORT_ID", "0")
    _use(api_env, FakeApi(_routes()))

    code = main(["tdx", "api", "check"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Claimed report: not set (tdx.api.claimed_report_id)." in captured.out
    assert "tdx api reports" in captured.err


def test_api_check_names_a_missing_column_and_exits_5(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _routes()
    routes[f"{API}/reports/12"] = report(CLAIMED[:-1], [])
    _use(api_env, FakeApi(routes))

    assert main(["tdx", "api", "check"]) == 5
    assert "claimed report: required column(s) missing: Prim Resp" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "hint"),
    [(403, "check the report ids"), (401, "tdx api token"), (429, "try again in a minute")],
)
def test_api_failures_exit_8_with_a_hint(
    status: int, hint: str, api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _routes()
    routes[f"{API}/auth/getuser"] = Answer(status, {"Message": "Kim Owner"})
    _use(api_env, FakeApi(routes))

    assert main(["tdx", "api", "check"]) == 8
    err = capsys.readouterr().err
    assert "TDX API request GET /auth/getuser failed" in err and hint in err
    assert "Kim Owner" not in err


def test_api_reports_lists_ids_and_names_only(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__API__CLAIMED_REPORT_ID", "0")  # not needed to list them
    api = FakeApi(_routes())
    _use(api_env, api)

    code = main(["tdx", "api", "reports"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[:3] == [
        "Reports you can see: 2.",
        "        12  Claimed by others  (Tickets)",
        "        11  Unclaimed in my groups  (Tickets)",
    ]
    assert "Kim Owner" not in out
    assert api.paths() == [f"{API}/reports"]


# --- --source api -------------------------------------------------------------------------


def test_rows_from_the_api_print_the_dashboard_summary(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(_routes())
    _use(api_env, api)

    code = main(["tdx", "rows", "--source", "api"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out.splitlines() == [
        cli.API_SOURCE_LINE,
        "Unclaimed: 1 ticket(s) on 1 page(s).",
        "Claimed by others: 1 ticket(s) on 1 page(s).",
        "Unreadable dates: 0. Duplicates: 0. Blocked requests: 0. Allowed non-GET: 0.",
    ]
    assert api.paths() == [f"{API}/reports/11", f"{API}/reports/12"]


def test_rows_from_the_api_flag_a_report_at_its_row_limit(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _routes()
    routes[f"{API}/reports/11"]["MaxResults"] = 1  # type: ignore[index]
    _use(api_env, FakeApi(routes))

    assert main(["tdx", "rows", "--source", "api"]) == 0
    assert (
        "Stopped at the report's row limit (MaxResults) for: unclaimed" in capsys.readouterr().out
    )


def test_audit_from_the_api_refuses_send_before_any_request(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(_routes())
    fetcher = _use(api_env, api)

    assert main(["tdx", "audit", "--source", "api", "--send"]) == 2
    assert "--send works with the browser source only" in capsys.readouterr().err
    assert api.calls == [] and fetcher.calls == 0


def test_audit_from_the_api_gives_the_demo_findings_without_the_llm(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    api_env.setenv("OPS_SUITE_LLM__ENABLED", "true")
    api_env.setenv("OPS_SUITE_LLM__ALLOW_INSTITUTIONAL_DATA", "true")
    api_env.setenv("OPS_SUITE_TDX__MAX_FEEDS_PER_RUN", "200")
    now = datetime.now(TZ)
    tickets = demo_tickets(now)
    api = FakeApi(demo_routes(tickets, now, TZ))
    _use(api_env, api)
    api_env.setattr(watch, "LlmClient", lambda settings: pytest.fail("no LLM on the API trial"))

    code = main(["tdx", "audit", "--source", "api"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    lines = captured.out.splitlines()
    assert lines[0] == cli.API_SOURCE_LINE
    assert lines[1:3] == [
        "Unclaimed: 21 ticket(s) on 1 page(s).",
        "Claimed by others: 6 ticket(s) on 1 page(s).",
    ]
    assert (
        "Findings: client waiting 6, follow up with client 0, event outreach due 1, "
        "event details pending 1, event over 1, location TBD soon 2, needs review 1, "
        "ready to close 1."
    ) in lines
    assert {method for method, _, _ in api.calls} == {"GET"}
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines()]
    assert any(r["message"] == "api requests" for r in records)
    for ticket in tickets:
        for text in (captured.out, log_text):
            assert ticket.title not in text and ticket.requestor not in text


def test_audit_from_the_api_reports_an_api_failure_with_exit_8(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _routes()
    routes[f"{API}/reports/11"] = Answer(403)
    _use(api_env, FakeApi(routes))

    assert main(["tdx", "audit", "--source", "api"]) == 8
    assert "TDX API request GET /reports/{n} failed (forbidden)" in capsys.readouterr().err


# --- tdx login ----------------------------------------------------------------------------


class LoginBrowser:
    def __init__(self, profile_dir: Path, base_url: str, **options: object) -> None:
        pass

    def __enter__(self) -> "LoginBrowser":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def open_start_page(self) -> None:
        pass

    def current_urls(self) -> list[str]:
        return [BASE]


SIGN_IN_LINE = (
    "The TDX API uses its own sign-in. If a sign-in page appears, sign in again (MFA included)."
)
RETRY = 'Run "uv run ops-suite tdx api token --headed" to try again.'
IDP_REFUSED = (
    "TDX API token: not saved (idp_refused). The sign-in service showed an error page; "
    "try again once."
)


@pytest.mark.parametrize(
    ("result", "line"),
    [
        (None, "TDX API token: saved, expires "),
        (TokenError("not_a_jwt"), f"TDX API token: not saved (not_a_jwt). {RETRY}"),
        (TokenError("sign_in_timeout"), f"TDX API token: not saved (sign_in_timeout). {RETRY}"),
        (TokenError("idp_refused"), IDP_REFUSED),
    ],
)
def test_login_asks_for_the_token_when_the_api_is_enabled(
    result: Exception | None,
    line: str,
    api_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = make_token(datetime.now(UTC) + timedelta(hours=24))
    asked: list[bool] = []

    def fetch(browser: object, tdx: object, *, interactive: bool = False) -> ApiToken:
        asked.append(interactive)
        if result is not None:
            raise result
        return token

    api_env.setattr("builtins.input", lambda *args: "")
    api_env.setattr(cli, "for_platform", lambda **kwargs: None)
    api_env.setattr(cli, "TdxBrowser", LoginBrowser)
    api_env.setattr(cli, "fetch_token", fetch)

    assert main(["tdx", "login"]) == 0  # the TDX sign-in worked either way

    out = capsys.readouterr().out.splitlines()
    assert asked == [True]  # in the same window, with the person
    assert out[-2] == SIGN_IN_LINE
    assert out[-1].startswith(line)
    assert token.value not in "\n".join(out)
    assert (load_token(datetime.now(UTC)) == token) is (result is None)
    sessions = json.loads(session_log_path(load_settings()).read_text(encoding="utf-8"))
    assert len(sessions["sessions"]) == 1  # the TDX session is saved and logged


class HeadedBrowser(LoginBrowser):
    opened: list[dict[str, object]] = []

    def __init__(self, profile_dir: Path, base_url: str, **options: object) -> None:
        HeadedBrowser.opened.append(options)


@pytest.mark.parametrize(
    ("result", "code", "line"),
    [
        (None, 0, "TDX API token: saved, expires "),
        (TokenError("sign_in_timeout"), 8, f"TDX API token: not saved (sign_in_timeout). {RETRY}"),
        (TokenError("window_closed"), 8, f"TDX API token: not saved (window_closed). {RETRY}"),
        (TokenError("idp_refused"), 8, IDP_REFUSED),
    ],
)
def test_api_token_headed_signs_in_in_a_window_and_skips_the_headless_try(
    result: Exception | None,
    code: int,
    line: str,
    api_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = make_token(datetime.now(UTC) + timedelta(hours=24))
    asked: list[bool] = []

    def fetch(browser: object, tdx: object, *, interactive: bool = False) -> ApiToken:
        asked.append(interactive)
        if result is not None:
            raise result
        return token

    fetcher = _use(api_env, FakeApi())
    HeadedBrowser.opened = []
    api_env.setattr(cli, "TdxBrowser", HeadedBrowser)
    api_env.setattr(cli, "fetch_token", fetch)

    assert main(["tdx", "api", "token", "--headed"]) == code

    captured = capsys.readouterr()
    assert fetcher.calls == 0 and asked == [True]
    assert HeadedBrowser.opened == [{}]  # headed and unguarded, as tdx login
    assert captured.out.splitlines()[0] == SIGN_IN_LINE
    shown = captured.out if result is None else captured.err
    assert shown.splitlines()[-1].startswith(line)
    assert token.value not in captured.out + captured.err
    assert (load_token(datetime.now(UTC)) == token) is (result is None)
    assert not session_log_path(load_settings()).exists()


def test_login_without_the_api_asks_for_no_token(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("OPS_SUITE_TDX__API__ENABLED", "false")
    api_env.setattr("builtins.input", lambda *args: "")
    api_env.setattr(cli, "for_platform", lambda **kwargs: None)
    api_env.setattr(cli, "TdxBrowser", LoginBrowser)
    api_env.setattr(cli, "fetch_token", lambda *a: pytest.fail("no token without the API"))

    assert main(["tdx", "login"]) == 0
    assert "TDX API token" not in capsys.readouterr().out


def test_a_stored_token_is_used_without_a_browser(
    api_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    save_token(make_token(datetime.now(UTC) + timedelta(hours=10)))
    fetcher = _use(api_env, FakeApi(_routes()))

    assert main(["tdx", "rows", "--source", "api"]) == 0
    assert fetcher.calls == 0
