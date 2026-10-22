"""The TDX Web API as the primary source, with the browser as a per-run fallback.

With ``tdx.source = "api"``, each ``tdx watch`` cycle (and ``tdx audit``) reads the queue
through the API (``ApiFirst``): the two reports, then the feeds and event forms the rules
plan for, judged with the desk memory, then the optional follow-up step, exactly as the
browser read is. Every finding, card and summary of that run comes from it.

When the API cannot be used for a run, the browser reads that run instead, as it always has,
and the run carries the API's reason code (``AuditRun.fallback``):

- no token, or one that needs the API's own sign-in (``needs_sign_in`` and the other token
  codes);
- an API error (``unauthorised``, ``forbidden``, ``rate_limited``, ``timeout``, ``http_<n>``,
  ``bad_json``, ...);
- a structure problem (``structure``: a missing report column; ``config``: a report id not
  set);
- no answer within ``tdx.api.max_cycle_seconds`` (``time_box``: no request is sent after it).

A fallback is never a failed run: only the browser's own errors reach the caller, and are
handled as they always are. The run posts once, from whichever source read it. The next run
tries the API again.

Notices go to the personal chat, at most one a day: ``needs_sign_in`` sends the token notice
(``ApiSignInNotices``) and nothing more; other reasons send the fallback notice. A signed-out
TDX session or a browser that cannot start sends none here, since the browser's read meets
the same problem and the loop already says so.

Output and logs carry reason codes and counts only.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ops_suite.common.config import Settings
from ops_suite.common.hints import RUN
from ops_suite.tdx.api_client import ApiError
from ops_suite.tdx.api_token import TokenError
from ops_suite.tdx.browser import BrowserStartError
from ops_suite.tdx.dashboard import NotSignedInError
from ops_suite.tdx.shadow import ClientFactory, default_client, failure_reason
from ops_suite.tdx.watch import (
    ApiSignInNotices,
    AuditRun,
    finish_audit,
    read_and_judge,
    read_and_judge_api,
)

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
BrowserRead = Callable[[Settings, Progress | None], AuditRun]

TOKEN_HINT = f'run "{RUN} tdx api token --headed".'
CHECK_HINT = f'run "{RUN} tdx api check".'
RETRY_HINT = "each run tries the API again."


def fallback_notice(error: Exception, reason: str) -> str:
    """The personal-chat notice for a run the browser read instead of the API; no ticket
    data."""
    if isinstance(error, TokenError) or (isinstance(error, ApiError) and reason == "unauthorised"):
        hint = TOKEN_HINT
    elif reason in ("structure", "config"):
        hint = CHECK_HINT
    else:
        hint = RETRY_HINT
    return (
        f"Reading the queue through the browser: the TDX Web API is unavailable ({reason}); {hint}"
    )


class FallbackNotices:
    """At most one fallback notice a day, plus the token notice's own once a day for
    ``needs_sign_in``, which replaces the fallback notice rather than doubling it."""

    def __init__(self, notify: Callable[[str], None], tz: ZoneInfo) -> None:
        self._notify = notify
        self._tz = tz
        self._sign_in = ApiSignInNotices(notify, tz)
        self.day: date | None = None  # the day of the last fallback notice

    def fell_back(self, error: Exception, reason: str, now: datetime) -> None:
        if self._sign_in.handled(error, now):
            return
        if isinstance(error, NotSignedInError | BrowserStartError):
            return  # the browser's read meets it too, and the loop says so
        today = now.astimezone(self._tz).date()
        if self.day == today:
            return
        self.day = today
        log.info("api fallback notice sent", extra={"reason": reason})
        self._notify(fallback_notice(error, reason))


def _browser_read(headless: bool) -> BrowserRead:
    def read(settings: Settings, progress: Progress | None) -> AuditRun:
        return read_and_judge(settings, headless=headless, progress=progress)

    return read


class ApiFirst:
    """One run's read with the API first: ``reader(progress)`` returns the judged run, with
    the follow-up step applied, from the API or (``run.fallback`` set) from the browser.

    Raises only what the browser's read raises. ``notify`` is None for a one-off command
    (no notices); ``client_factory``, ``browser``, ``clock`` and ``wall`` let tests run
    without a browser, network or real time.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        headless: bool,
        use_llm: bool,
        notify: Callable[[str], None] | None = None,
        client_factory: ClientFactory = default_client,
        browser: BrowserRead | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        tdx = settings.tdx.model_copy(update={"base_url": settings.tdx.base_url.strip()})
        self._settings = settings.model_copy(update={"tdx": tdx})
        self._use_llm = use_llm
        self._client_factory = client_factory
        self._browser = browser or _browser_read(headless)
        self._clock = clock
        self._wall = wall
        tz = ZoneInfo(settings.schedule.timezone)
        self._notices = FallbackNotices(notify, tz) if notify is not None else None

    def __call__(self, progress: Progress | None = None) -> AuditRun:
        settings = self._settings
        started = self._clock()
        try:
            client = self._client_factory(settings, started + settings.tdx.api.max_cycle_seconds)
            run = read_and_judge_api(settings, progress=progress, client=client)
        except Exception as error:  # any API trouble: the browser reads this run
            reason = failure_reason(error)
            log.info("tdx watch source", extra={"source": "browser", "fallback": reason})
            if self._notices is not None:
                self._notices.fell_back(error, reason, self._wall())
            started = self._clock()
            run = replace(self._browser(settings, progress), fallback=reason)
        else:
            log.info(
                "tdx watch source",
                extra={"source": "api", "fallback": None, "api_requests": run.api_requests},
            )
        return finish_audit(settings, run, self._clock() - started, use_llm=self._use_llm)
