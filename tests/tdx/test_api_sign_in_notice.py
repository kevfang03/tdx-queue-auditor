"""A background token refresh that needs the API's own sign-in: one plain notice a day to the
personal chat, never a cycle failure, never the session log. Fake clock; no browser."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ops_suite.common.config import load_settings
from ops_suite.tdx.api_client import ApiError
from ops_suite.tdx.api_token import TokenError
from ops_suite.tdx.dashboard import NotSignedInError
from ops_suite.tdx.session_log import session_log_path
from ops_suite.tdx.watch import API_SIGN_IN_NOTICE, CYCLE_ERRORS, ApiSignInNotices

NY = ZoneInfo("America/New_York")
MONDAY = datetime(2026, 10, 19, 9, 0, tzinfo=NY)


def test_the_notice_names_the_headed_command_and_holds_no_ticket_data() -> None:
    assert API_SIGN_IN_NOTICE == (
        'TDX API token needs a sign-in: run "uv run ops-suite tdx api token --headed".'
    )


def test_needs_sign_in_is_noticed_once_a_day_across_cycles() -> None:
    sent: list[str] = []
    notices = ApiSignInNotices(sent.append, NY)
    error = TokenError("needs_sign_in")
    # Every 10 minutes from Monday 09:00 to Tuesday 17:00, desk hours or not.
    cycles = [MONDAY + timedelta(minutes=10 * n) for n in range(6 * 32)]

    handled = [notices.handled(error, now) for now in cycles]

    assert all(handled)
    assert sent == [API_SIGN_IN_NOTICE, API_SIGN_IN_NOTICE]  # Monday, then Tuesday
    assert notices.day == (MONDAY + timedelta(days=1)).date()


def test_the_day_is_the_desks_day_not_utcs() -> None:
    sent: list[str] = []
    notices = ApiSignInNotices(sent.append, NY)
    monday_utc = datetime(2026, 10, 19, 23, 30, tzinfo=UTC)  # Monday 19:30 at the desk
    tuesday_utc = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)  # still Monday, 21:00, at the desk

    notices.handled(TokenError("needs_sign_in"), monday_utc)
    notices.handled(TokenError("needs_sign_in"), tuesday_utc)

    assert len(sent) == 1


def test_other_errors_are_left_to_the_caller() -> None:
    sent: list[str] = []
    notices = ApiSignInNotices(sent.append, NY)

    for error in (
        TokenError("idp_refused"),
        TokenError("no_token"),
        ApiError("unauthorised", "GET /auth/getuser"),
        NotSignedInError(),
    ):
        assert not notices.handled(error, MONDAY)

    assert sent == [] and notices.day is None


def test_needs_sign_in_is_no_cycle_failure_and_writes_no_session_log() -> None:
    error = TokenError("needs_sign_in")
    notices = ApiSignInNotices(lambda text: None, NY)

    notices.handled(error, MONDAY)

    assert not any(isinstance(error, kind) for kind in CYCLE_ERRORS)
    assert not isinstance(error, NotSignedInError)  # the watch loop's signed-out branch
    assert not session_log_path(load_settings()).exists()
