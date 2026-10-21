"""read_dashboard against the synthetic TDWorkManagement app on a local server.

Headless Chromium with the read-only guard, as ``tdx rows`` runs it. No real site is contacted.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.common.config import TdxSettings, TdxWidgetSettings
from ops_suite.tdx.browser import TdxBrowser
from ops_suite.tdx.dashboard import (
    DashboardRead,
    DashboardStructureError,
    NotSignedInError,
    WidgetNotFoundError,
    check_dashboard,
    find_dashboard_frame,
    read_dashboard,
)
from ops_suite.tdx.demo.app import WorkMgmtServer
from ops_suite.tdx.guard import BUILT_IN_ALLOWED

pytestmark = pytest.mark.browser

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
WIDGET_POST = "POST /TDWorkManagement/WorkManagement/Widgets/#"
BLOCKED_FEED = "POST /TDWorkManagement/api/#/feed"
UNCLAIMED_IDS = {4410231, 4410245, 4410302, 4410317}
CLAIMED_IDS = {4409100 + i * 37 for i in range(23)}


def _settings(server: WorkMgmtServer, variant: str = "", **overrides: object) -> TdxSettings:
    values: dict[str, object] = {
        "base_url": server.base_url,
        "dashboard_url": server.dashboard(variant),
        "page_timeout_seconds": 10,
    }
    values.update(overrides)
    return TdxSettings(**values)


def _read(tmp_path: Path, settings: TdxSettings) -> DashboardRead:
    browser = TdxBrowser(
        tmp_path / "profile",
        settings.base_url,
        headless=True,
        allowed_requests=BUILT_IN_ALLOWED,
    )
    with browser:
        return read_dashboard(browser, settings, NOW, start_url=settings.dashboard_url)


@pytest.mark.parametrize("variant", ["", "closed-loading"])
def test_reads_every_row_of_both_widgets(
    variant: str, workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    read = _read(tmp_path, _settings(workmgmt_server, variant))

    unclaimed = read.widgets["unclaimed"]
    claimed = read.widgets["claimed"]
    assert {row.ticket_id for row in unclaimed.rows} == UNCLAIMED_IDS
    assert (unclaimed.pages, unclaimed.hit_page_limit, unclaimed.duplicates) == (1, False, 0)
    assert unclaimed.unreadable_dates == ("aaaaaaaaa 9:99 aa",)
    # Page 3 repeats the last row of page 2, as TDX does when rows shift while paging.
    assert {row.ticket_id for row in claimed.rows} == CLAIMED_IDS
    assert (claimed.pages, claimed.hit_page_limit, claimed.duplicates) == (3, False, 1)
    assert claimed.unreadable_dates == ()
    # The Closed widget and the unrelated widget with an ID column are ignored.
    assert {r.widget for r in unclaimed.rows} == {"unclaimed"}
    assert {r.widget for r in claimed.rows} == {"claimed"}

    row = next(r for r in claimed.rows if r.ticket_id == 4409100 + 5 * 37)
    assert row.prim_resp is None
    assert row.status == "Open"
    assert row.modified == datetime(2026, 10, 4, 12, 30, tzinfo=TZ)
    assert row.detail_url == (
        f"http://127.0.0.1:{workmgmt_server.port}"
        f"/TDWorkManagement/Apps/43/Tickets/TicketDet?TicketID={4409100 + 5 * 37}"
    )
    first = next(r for r in unclaimed.rows if r.ticket_id == 4410231)
    assert first.created == datetime(2026, 10, 2, 15, 30, tzinfo=TZ)
    assert first.resp_group == "IT Service Desk"

    # The write fired on load was aborted before it reached the server.
    assert read.blocked == (BLOCKED_FEED,)
    assert ("POST", "/TDWorkManagement/api/12345/feed") not in workmgmt_server.received
    # One widget read per loaded widget, plus one per page turn of the claimed widget.
    loaded_widgets = 3 if variant == "closed-loading" else 4
    assert read.allowed_non_get == (WIDGET_POST,) * (loaded_widgets + 2)


def test_the_page_limit_is_honoured(workmgmt_server: WorkMgmtServer, tmp_path: Path) -> None:
    read = _read(tmp_path, _settings(workmgmt_server, max_pages_per_widget=2))

    claimed = read.widgets["claimed"]
    assert (claimed.pages, claimed.hit_page_limit) == (2, True)
    assert len(claimed.rows) == 20
    assert read.widgets["unclaimed"].hit_page_limit is False


def test_an_empty_widget_gives_no_rows(workmgmt_server: WorkMgmtServer, tmp_path: Path) -> None:
    read = _read(tmp_path, _settings(workmgmt_server, "empty"))

    assert read.widgets["unclaimed"].rows == ()
    assert read.widgets["unclaimed"].pages == 1
    assert len(read.widgets["claimed"].rows) == 3
    assert read.widgets["claimed"].pages == 1


def test_the_sign_in_redirect_raises_not_signed_in(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    with pytest.raises(NotSignedInError):
        _read(tmp_path, _settings(workmgmt_server, "signin"))


def test_a_pager_that_never_advances_is_a_structure_error(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    with pytest.raises(DashboardStructureError) as error:
        _read(tmp_path, _settings(workmgmt_server, "stuck", page_timeout_seconds=5))

    assert "paging did not advance for the claimed widget after page 1" in str(error.value)
    assert error.value.blocked == (BLOCKED_FEED,)


def test_a_missing_column_is_a_structure_error(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    with pytest.raises(DashboardStructureError) as error:
        _read(tmp_path, _settings(workmgmt_server, "missing"))

    message = str(error.value)
    assert "claimed widget: required column(s) missing: Prim Resp" in message
    for invented in ["Morgan Fixture", "Pat Helpdesk", "Zoom recording", "4409100"]:
        assert invented not in message


def test_a_widget_that_is_not_there_is_named(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    settings = _settings(
        workmgmt_server,
        page_timeout_seconds=5,
        widgets=TdxWidgetSettings(claimed_title="Tickets Nobody Has"),
    )

    with pytest.raises(DashboardStructureError) as error:
        _read(tmp_path, settings)

    assert 'widget not found: "Tickets Nobody Has" (tdx.widgets.claimed_title)' in str(error.value)


# --- choosing the dashboard (tdx set-dashboard) -------------------------------------------


def _open_shell(browser: TdxBrowser, server: WorkMgmtServer, switch_to: str = "") -> None:
    """Open the app on the homepage and, like a person using the menu, switch dashboards."""
    browser.open_url(server.shell())
    for _ in range(40):
        if len(browser.live_frames()) >= 2:
            break
        browser.wait(100)
    if switch_to:
        browser.live_frames()[0].evaluate(f"switchDashboard({switch_to!r})")


def _find(tmp_path: Path, server: WorkMgmtServer, switch_to: str) -> tuple[str, list[str]]:
    # Unguarded, as in tdx set-dashboard while the person drives the window.
    with TdxBrowser(tmp_path / "find-profile", server.base_url, headless=True) as browser:
        _open_shell(browser, server, switch_to)
        url = find_dashboard_frame(browser, _settings(server))
        return url, browser.current_urls()


def _check(tmp_path: Path, server: WorkMgmtServer, url: str) -> None:
    settings = _settings(server, page_timeout_seconds=5)
    browser = TdxBrowser(
        tmp_path / "check-profile",
        server.base_url,
        headless=True,
        allowed_requests=BUILT_IN_ALLOWED,
    )
    with browser:
        check_dashboard(browser, settings, url)


def test_find_returns_the_inner_frame_address_with_its_query(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    url, top_urls = _find(tmp_path, workmgmt_server, "d=2")

    assert url == workmgmt_server.frame("d=2")
    # The page address never changed: only the inner frame switched dashboards.
    assert top_urls == [workmgmt_server.shell()]


def test_the_homepage_dashboard_names_the_missing_widget(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    with pytest.raises(WidgetNotFoundError) as error:
        _find(tmp_path, workmgmt_server, "")

    assert str(error.value) == (
        'widget not found: "Active Tickets Claimed by Others in My Group(s)" '
        '(tdx.widgets.claimed_title) (found: "Unclaimed Tickets in My Group(s)" '
        "(tdx.widgets.unclaimed_title))"
    )


def test_a_saved_address_that_opens_on_its_own_passes_the_check(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    url, _ = _find(tmp_path, workmgmt_server, "d=2")

    _check(tmp_path, workmgmt_server, url)


def test_a_frame_that_needs_its_parent_fails_the_check(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    # Inside the app it shows both widgets, so set-dashboard finds and saves it...
    url, _ = _find(tmp_path, workmgmt_server, "d=3")
    assert url == workmgmt_server.frame("d=3")

    # ...but opened on its own it shows no widgets.
    with pytest.raises(WidgetNotFoundError, match="widget not found"):
        _check(tmp_path, workmgmt_server, url)


def test_reads_every_row_from_the_saved_address(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    saved, _ = _find(tmp_path, workmgmt_server, "d=2")
    settings = _settings(workmgmt_server)
    browser = TdxBrowser(
        tmp_path / "profile", settings.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    )

    with browser:
        read = read_dashboard(browser, settings, NOW, start_url=saved)

    assert {row.ticket_id for row in read.widgets["unclaimed"].rows} == UNCLAIMED_IDS
    assert {row.ticket_id for row in read.widgets["claimed"].rows} == CLAIMED_IDS
    assert read.widgets["claimed"].pages == 3
    assert read.blocked == ()  # the shell, which fires the blocked request, is not loaded
    assert read.allowed_non_get == (WIDGET_POST,) * 6
