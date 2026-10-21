"""TdxBrowser against the synthetic pages on a local http.server. No real site is contacted.

The start-failure tests replace Playwright with a fake and need no browser.
"""

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ops_suite.tdx import browser as browser_module
from ops_suite.tdx.browser import (
    PROFILE_IN_USE_MESSAGE,
    BrowserStartError,
    FrameHtml,
    TdxBrowser,
    profile_in_use,
    start_failure,
)
from ops_suite.tdx.session import on_tdx

SYNTHETIC = Path(__file__).parent.parent / "fixtures" / "tdx_synthetic"

_needs_display = pytest.mark.skipif(
    sys.platform.startswith("linux")
    and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="TdxBrowser is always headed and no display is available (DISPLAY is unset)",
)


def real_browser(test):
    """Starts a real Chromium: marked ``browser`` and skipped on Linux without a display."""
    return pytest.mark.browser(_needs_display(test))


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def fixture_server() -> Iterator[str]:
    """Serve the synthetic pages on a free port of 127.0.0.1; yield the base URL."""
    handler = partial(_QuietHandler, directory=str(SYNTHETIC))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def _wait_for(browser: TdxBrowser, done, timeout: float = 15.0) -> list[FrameHtml]:
    deadline = time.monotonic() + timeout
    while True:
        frames = browser.collect_frames()
        if done(frames, browser.requests_seen()) or time.monotonic() > deadline:
            return frames
        time.sleep(0.2)


def _open_start_page(browser: TdxBrowser, url: str, timeout: float = 10.0) -> None:
    """Open the start page and wait until the page is there; retry the start page once.

    On Windows a connection to the local server was once aborted mid-load (WinError 10053,
    most likely a connection Chromium dropped while the page loaded), the navigation failed
    and the page stayed on about:blank. The server already serves each connection in its own
    thread, so the abort cannot block it; only the one navigation is lost.
    TdxBrowser treats a failed start page as not fatal by design (the person retries by
    hand), so the test retries the way a person would.
    """
    for _ in range(2):
        browser.open_start_page()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if browser.current_urls() == [url]:
                return
            time.sleep(0.1)


@real_browser
def test_collects_the_main_frame_and_the_iframe(fixture_server: str, tmp_path: Path) -> None:
    base_url = f"{fixture_server}dashboard.html"
    profile = tmp_path / "profile"

    def loaded(frames: list[FrameHtml], requests) -> bool:
        widget_ready = len(frames) >= 2 and "REQUESTOR" in frames[1].html
        posted = any(request.method == "POST" for request in requests)
        return widget_ready and posted

    with TdxBrowser(profile, base_url) as browser:
        _open_start_page(browser, base_url)
        frames = _wait_for(browser, loaded)
        urls = browser.current_urls()
        requests = browser.requests_seen()
        skipped = browser.skipped_frames

    assert urls == [base_url]
    assert [(frame.page_index, frame.frame_index) for frame in frames] == [(0, 0), (0, 1)]
    assert frames[0].url == base_url
    assert frames[1].url.startswith(f"{fixture_server}widget.html?")
    assert "Active Tickets Claimed by Others" in frames[0].html
    assert "REQUESTOR" in frames[1].html
    assert skipped == 0
    assert profile.is_dir() and any(profile.iterdir())

    assert requests, "no requests were recorded"
    assert all(on_tdx(request.url, base_url) for request in requests)
    assert not any("localhost" in request.url for request in requests)
    seen = {
        (request.method, request.resource_type, request.url.split("?")[0]) for request in requests
    }
    assert ("GET", "document", base_url) in seen
    assert ("GET", "document", f"{fixture_server}widget.html") in seen
    assert ("POST", "fetch", f"{fixture_server}api/tickets.json") in seen


@real_browser
def test_a_start_page_that_does_not_load_is_not_fatal(tmp_path: Path) -> None:
    # Nothing listens on this port of 127.0.0.1 once the socket is closed.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    port = server.server_address[1]
    server.server_close()
    base_url = f"http://127.0.0.1:{port}/"

    with TdxBrowser(tmp_path / "profile", base_url) as browser:
        browser.open_start_page()
        urls = browser.current_urls()

    # The page stays open (on Chromium's error page) for the person to retry by hand.
    assert len(urls) == 1


@real_browser
def test_sees_navigation_that_happened_while_waiting_for_enter(
    fixture_server: str, tmp_path: Path
) -> None:
    base_url = f"{fixture_server}redirect.html"

    with TdxBrowser(tmp_path / "profile", base_url) as browser:
        browser.open_start_page()
        # The CLI blocks in input() here; Playwright processes no browser events meanwhile.
        time.sleep(2)
        urls = browser.current_urls()

    assert len(urls) == 1
    assert urls[0].startswith(f"{fixture_server}widget.html?")


_HOLD_PROFILE = """
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    playwright.chromium.launch_persistent_context(sys.argv[1], headless=False)
    print("ready", flush=True)
    sys.stdin.read()
"""


@real_browser
def test_a_profile_held_by_a_headed_window_is_refused(fixture_server: str, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    # A "tdx login" window in another process; Playwright allows one sync API per thread.
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_PROFILE, str(profile)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        assert profile_in_use(profile)
        for options in ({}, {"headless": True, "allowed_requests": ()}):
            with pytest.raises(BrowserStartError) as error:
                with TdxBrowser(profile, fixture_server, **options):
                    pass
            assert str(error.value) == PROFILE_IN_USE_MESSAGE
            assert error.value.reason == "profile_in_use"
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)
    assert not profile_in_use(profile)


# --- start failures, with a fake Playwright (no browser) ----------------------------------

HOME_PATH = "/Users/jordan.sampleton/Library/Caches/ms-playwright/chromium-1140/chrome-mac"
LAUNCH = "BrowserType.launch_persistent_context: "
START_FAILURES = [
    (
        PlaywrightError(
            f"{LAUNCH}Failed to create a ProcessSingleton for your profile directory. This "
            "usually means that the profile is already in use by another instance of Chromium."
        ),
        "profile_in_use",
    ),
    (
        PlaywrightError(
            f"{LAUNCH}Executable doesn't exist at {HOME_PATH}/Chromium.app\n"
            "Looks like Playwright was just installed or updated.\n"
            "Please run the following command to download new browsers:\n"
            "    playwright install"
        ),
        "not_installed",
    ),
    (PlaywrightTimeoutError(f"{LAUNCH}Timeout 60000ms exceeded."), "launch_timeout"),
    (
        PlaywrightError(
            f"{LAUNCH}Target page, context or browser has been closed\n"
            "Looks like you launched a headed browser without having a XServer running.\n"
            "[pid=4242][err] Missing X server or $DISPLAY"
        ),
        "no_display",
    ),
    (PlaywrightError(f"{LAUNCH}Browser closed.\n==== logs ===="), "other"),
]


class FakeChromium:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.launches: list[dict] = []

    def launch_persistent_context(self, profile_dir: Path, **options: object) -> None:
        self.launches.append(options)
        if self.error is not None:
            raise self.error
        raise PlaywrightError("the fake starts no browser")


class FakePlaywright:
    def __init__(self, error: Exception | None = None) -> None:
        self.chromium = FakeChromium(error)
        self.stopped = False

    def __call__(self) -> "FakePlaywright":  # stands in for sync_playwright()
        return self

    def start(self) -> "FakePlaywright":
        return self

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.parametrize(("error", "reason"), START_FAILURES)
def test_each_start_failure_gets_its_reason_code(error: PlaywrightError, reason: str) -> None:
    assert start_failure(error) == reason


@pytest.mark.parametrize(("error", "reason"), START_FAILURES)
def test_a_failed_launch_raises_with_the_reason_and_stops_playwright(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: PlaywrightError, reason: str
) -> None:
    fake = FakePlaywright(error)
    monkeypatch.setattr(browser_module, "sync_playwright", fake)

    with pytest.raises(BrowserStartError) as raised:
        with TdxBrowser(tmp_path / "profile", "https://tdx.example.edu/TDNext/"):
            pass

    assert raised.value.reason == reason
    assert fake.stopped
    if reason == "profile_in_use":
        assert str(raised.value) == PROFILE_IN_USE_MESSAGE


def test_a_browser_start_error_defaults_to_other() -> None:
    assert BrowserStartError("something new").reason == "other"


def test_the_launch_timeout_reaches_launch_persistent_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakePlaywright()
    monkeypatch.setattr(browser_module, "sync_playwright", fake)
    base_url = "https://tdx.example.edu/TDNext/"

    for options in ({}, {"launch_timeout_seconds": 90}):
        with pytest.raises(BrowserStartError):
            with TdxBrowser(tmp_path / "profile", base_url, **options):
                pass

    assert [launch["timeout"] for launch in fake.chromium.launches] == [60_000, 90_000]
