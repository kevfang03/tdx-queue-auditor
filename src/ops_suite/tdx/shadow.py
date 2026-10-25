"""Shadow mode: the two sources read side by side, compared in counts only.

For a side-by-side trial of the two sources. With ``tdx.source = "shadow"``, each successful
``tdx watch`` cycle, after its post, reads the same queue through the API (``ShadowRunner``):

- the two reports, then the feeds and event forms of the tickets the **browser's** plan picked,
  at the browser run's time;
- the rules over each source's data, with the same settings and the desk-member memory read
  only (never taught or saved), so the two sets of findings differ only where the data does;
- ``compare``: a pure diff in counts and ticket ids, for rows, feeds and findings;
- one line per cycle in ``api-shadow.jsonl`` (``ShadowLog``), kept 30 days.

The API part never posts, never writes the alert, summary or verdict state or the desk memory,
and sends no request after ``tdx.shadow.max_seconds`` (``time_box``). A failure is recorded
with its reason code and never fails the cycle; a token that needs the API's own sign-in sends
one notice a day to the personal chat.

With ``tdx.source = "api"`` the roles swap (``BrowserShadow``): the API makes the cards, and
the browser reads the tickets the API run's plan picked, for comparison only. Every cycle
gets a record: compared, a browser read that failed or was skipped (a signed-out browser is
not an error), or a cycle the browser read instead of the API (``fallback``, with the API's
reason code).

``compare_report`` turns the records into the lines ``tdx api compare`` prints: the shadow
period (the browser primary) and the API-primary period, each on its own.

Personal data rule: a record holds times, seconds, counts, ticket ids and codes only: never a
name, a title or any other text. Rows, feeds and findings stay in memory.
"""

import json
import logging
import os
import statistics
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import keyring.errors

from ops_suite.common import paths
from ops_suite.common.config import ConfigProblem, Settings
from ops_suite.tdx import api_source
from ops_suite.tdx.api_client import ApiError, TdxApiClient
from ops_suite.tdx.api_token import TokenError, TokenSource, browser_fetcher
from ops_suite.tdx.browser import BrowserStartError
from ops_suite.tdx.dashboard import KINDS, DashboardStructureError, NotSignedInError
from ops_suite.tdx.desk_memory import DeskMemory
from ops_suite.tdx.event_form import EventForm
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.names import same_person
from ops_suite.tdx.rows import TicketRow, WidgetKind
from ops_suite.tdx.rules import Finding, evaluate, plan_feeds
from ops_suite.tdx.watch import (
    ApiSignInNotices,
    AuditRun,
    desk_memory_path,
    form_options,
    read_browser,
    rule_settings,
)

log = logging.getLogger(__name__)

SHADOW_FILE_NAME = "api-shadow.jsonl"
KEEP_DAYS = 30
SCHEMA = 1
SAME_TIME = timedelta(minutes=1)  # Created, Modified and entry times this close are the same
ENTRY_KINDS = ("reply", "private", "system")

# Row fields compared, per ticket in both sources.
ROW_FIELDS = ("created", "modified", "title", "requestor", "prim_resp", "status", "resp_group")
# Feed checks, per ticket whose feed both sources read. "latest" is the newest entry someone
# wrote (a reply or a private note), the one the rules look at.
FEED_CHECKS = (
    "reply_count",
    "private_count",
    "system_count",
    "latest_missing",
    "latest_private",
    "latest_author",
    "latest_time",
)
# Every mismatch category of the report, in order, with its label.
CATEGORIES: dict[str, str] = {
    "row_only_browser": "rows only in the browser",
    "row_only_api": "rows only in the API",
    "created": "Created differs",
    "modified": "Modified differs",
    "title": "title differs",
    "requestor": "requestor differs",
    "prim_resp": "Prim Resp differs",
    "status": "status differs",
    "resp_group": "Resp Group differs",
    "feed_only_browser": "feeds read only by the browser",
    "feed_only_api": "feeds read only through the API",
    "reply_count": "reply count differs",
    "private_count": "private note count differs",
    "system_count": "system entry count differs",
    "latest_missing": "latest entry in one source only",
    "latest_private": "private flag differs",
    "latest_author": "latest author differs",
    "latest_time": "latest time differs",
    "finding_only_browser": "findings only from the browser",
    "finding_only_api": "findings only from the API",
}
SHOW_IDS_WARNING = (
    "Ticket ids, for your own check in TDX only; do not paste these lines into a chat:"
)

FindingKey = tuple[int, str, str]  # (ticket id, kind, reason code)
# Which source made the cycle's cards. The diff and the record name the sources, never their
# roles, so they read the same whichever source is primary; a record carries ``primary`` and
# its "failed" result is about the compared source.
Primary = Literal["browser", "api"]


# --- the diff (pure) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRead:
    """What one source gave for the comparison. Student data: kept in memory only."""

    rows: Sequence[TicketRow]
    feeds: Mapping[int, TicketFeed]
    findings: Sequence[Finding]

    def __repr__(self) -> str:
        return f"SourceRead(rows={len(self.rows)}, feeds={len(self.feeds)})"


@dataclass(frozen=True, slots=True)
class RowsDiff:
    """One widget (or report): counts, and ticket ids by category."""

    browser: int
    api: int
    matched: int  # in both, with every field the same
    only_browser: tuple[int, ...]
    only_api: tuple[int, ...]
    differs: Mapping[str, tuple[int, ...]]  # field -> ticket ids in both whose field differs

    @property
    def total(self) -> int:
        """Tickets in either source."""
        return self.browser + len(self.only_api)


@dataclass(frozen=True, slots=True)
class FeedsDiff:
    compared: int  # feeds both sources read
    matched: int  # of those, with every check the same
    only_browser: tuple[int, ...]
    only_api: tuple[int, ...]
    entries_browser: Mapping[str, int]  # entry counts by kind over the compared feeds
    entries_api: Mapping[str, int]
    differs: Mapping[str, tuple[int, ...]]  # check -> ticket ids

    @property
    def total(self) -> int:
        return self.compared + len(self.only_browser) + len(self.only_api)


@dataclass(frozen=True, slots=True)
class FindingsDiff:
    browser: int
    api: int
    only_browser: tuple[FindingKey, ...]
    only_api: tuple[FindingKey, ...]

    @property
    def identical(self) -> bool:
        return not self.only_browser and not self.only_api

    @property
    def differences(self) -> int:
        return len(self.only_browser) + len(self.only_api)


@dataclass(frozen=True, slots=True)
class ShadowDiff:
    rows: Mapping[WidgetKind, RowsDiff]
    feeds: FeedsDiff
    findings: FindingsDiff


def normalised_text(text: str | None) -> str:
    """Text as compared: Unicode-normalised, whitespace collapsed, case folded."""
    return " ".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def _same_time(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a.astimezone(UTC) - b.astimezone(UTC)) <= SAME_TIME


def _same_name(a: str | None, b: str | None) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return not a and not b
    return same_person(a, b)


def row_differences(browser: TicketRow, api: TicketRow) -> list[str]:
    """The ``ROW_FIELDS`` that differ between one ticket's two rows: times within a minute,
    names compared as people, other text as normalised text."""
    found = []
    for name in ("created", "modified"):
        if not _same_time(getattr(browser, name), getattr(api, name)):
            found.append(name)
    for name in ("title", "status", "resp_group"):
        if normalised_text(getattr(browser, name)) != normalised_text(getattr(api, name)):
            found.append(name)
    for name in ("requestor", "prim_resp"):
        if not _same_name(getattr(browser, name), getattr(api, name)):
            found.append(name)
    return sorted(found, key=ROW_FIELDS.index)


def _latest(feed: TicketFeed) -> FeedEntry | None:
    return next((entry for entry in feed.entries if entry.kind != "system"), None)


def feed_differences(browser: TicketFeed, api: TicketFeed) -> list[str]:
    """The ``FEED_CHECKS`` that differ between one ticket's two feeds."""
    found = []
    counts = Counter(e.kind for e in browser.entries), Counter(e.kind for e in api.entries)
    found += [f"{kind}_count" for kind in ENTRY_KINDS if counts[0][kind] != counts[1][kind]]
    latest = _latest(browser), _latest(api)
    if latest[0] is None or latest[1] is None:
        if (latest[0] is None) != (latest[1] is None):
            found.append("latest_missing")
        return found
    if latest[0].kind != latest[1].kind:
        found.append("latest_private")
    if not same_person(latest[0].author, latest[1].author):
        found.append("latest_author")
    if not _same_time(latest[0].created, latest[1].created):
        found.append("latest_time")
    return found


def _rows_diff(browser: Sequence[TicketRow], api: Sequence[TicketRow]) -> RowsDiff:
    by_browser = {row.ticket_id: row for row in browser}
    by_api = {row.ticket_id: row for row in api}
    differs: dict[str, list[int]] = {}
    matched = 0
    for ticket_id in sorted(by_browser.keys() & by_api.keys()):
        fields = row_differences(by_browser[ticket_id], by_api[ticket_id])
        matched += not fields
        for name in fields:
            differs.setdefault(name, []).append(ticket_id)
    return RowsDiff(
        browser=len(by_browser),
        api=len(by_api),
        matched=matched,
        only_browser=tuple(sorted(by_browser.keys() - by_api.keys())),
        only_api=tuple(sorted(by_api.keys() - by_browser.keys())),
        differs={name: tuple(ids) for name, ids in differs.items()},
    )


def _entry_counts(feeds: Iterable[TicketFeed]) -> dict[str, int]:
    counts = Counter(entry.kind for feed in feeds for entry in feed.entries)
    return {kind: counts[kind] for kind in ENTRY_KINDS}


def _feeds_diff(browser: Mapping[int, TicketFeed], api: Mapping[int, TicketFeed]) -> FeedsDiff:
    both = sorted(browser.keys() & api.keys())
    differs: dict[str, list[int]] = {}
    matched = 0
    for ticket_id in both:
        checks = feed_differences(browser[ticket_id], api[ticket_id])
        matched += not checks
        for check in checks:
            differs.setdefault(check, []).append(ticket_id)
    return FeedsDiff(
        compared=len(both),
        matched=matched,
        only_browser=tuple(sorted(browser.keys() - api.keys())),
        only_api=tuple(sorted(api.keys() - browser.keys())),
        entries_browser=_entry_counts(browser[i] for i in both),
        entries_api=_entry_counts(api[i] for i in both),
        differs={check: tuple(ids) for check, ids in differs.items()},
    )


def _keys(findings: Sequence[Finding]) -> set[FindingKey]:
    return {(f.ticket_id, f.kind, f.reason) for f in findings}


def compare(browser: SourceRead, api: SourceRead) -> ShadowDiff:
    """The two reads compared: counts, ticket ids and codes only. Pure."""
    found = _keys(browser.findings), _keys(api.findings)
    return ShadowDiff(
        rows={
            kind: _rows_diff(
                [r for r in browser.rows if r.widget == kind],
                [r for r in api.rows if r.widget == kind],
            )
            for kind in KINDS
        },
        feeds=_feeds_diff(browser.feeds, api.feeds),
        findings=FindingsDiff(
            browser=len(found[0]),
            api=len(found[1]),
            only_browser=tuple(sorted(found[0] - found[1])),
            only_api=tuple(sorted(found[1] - found[0])),
        ),
    )


API_LABEL = "API shadow"  # the browser primary, the API compared
BROWSER_LABEL = "Browser shadow"  # the API primary, the browser compared


def shadow_line(diff: ShadowDiff, label: str = API_LABEL) -> str:
    """The cycle line's part: ``API shadow: findings identical.`` or the difference count."""
    if diff.findings.identical:
        return f"{label}: findings identical."
    n = diff.findings.differences
    return f"{label}: {n} difference{'' if n == 1 else 's'} (see tdx api compare)."


def failure_line(reason: str, label: str = API_LABEL) -> str:
    return f"{label}: not compared ({reason})."


# --- the record --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Timing:
    browser_seconds: float | None
    api_seconds: float | None
    api_requests: int
    browser_non_get: int  # the browser's allowlisted non-GET requests in this cycle


def _round(seconds: float | None) -> float | None:
    return round(seconds, 1) if seconds is not None else None


def _base(at: datetime, result: str, timing: Timing, primary: Primary) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "at": at.astimezone(UTC).isoformat(timespec="seconds"),
        "primary": primary,
        "result": result,
        "seconds": {
            "browser": _round(timing.browser_seconds),
            "api": _round(timing.api_seconds),
        },
        "api_requests": timing.api_requests,
        "browser_non_get": timing.browser_non_get,
    }


def shadow_record(
    diff: ShadowDiff, at: datetime, timing: Timing, primary: Primary = "browser"
) -> dict[str, object]:
    """One compared cycle as a JSON-ready record: times, counts, ticket ids and codes."""
    record = _base(at, "compared", timing, primary)
    record["rows"] = {
        kind: {
            "browser": rows.browser,
            "api": rows.api,
            "matched": rows.matched,
            "only_browser": list(rows.only_browser),
            "only_api": list(rows.only_api),
            "differs": {name: list(ids) for name, ids in rows.differs.items()},
        }
        for kind, rows in diff.rows.items()
    }
    feeds = diff.feeds
    record["feeds"] = {
        "compared": feeds.compared,
        "matched": feeds.matched,
        "only_browser": list(feeds.only_browser),
        "only_api": list(feeds.only_api),
        "entries": {"browser": dict(feeds.entries_browser), "api": dict(feeds.entries_api)},
        "differs": {check: list(ids) for check, ids in feeds.differs.items()},
    }
    findings = diff.findings
    record["findings"] = {
        "browser": findings.browser,
        "api": findings.api,
        "identical": findings.identical,
        "only_browser": [list(key) for key in findings.only_browser],
        "only_api": [list(key) for key in findings.only_api],
    }
    return record


def failure_record(
    reason: str, at: datetime, timing: Timing, primary: Primary = "browser"
) -> dict[str, object]:
    """A cycle whose compared read (the API's, while the browser is primary) failed or ran
    out of time: its reason code and timing."""
    record = _base(at, "failed", timing, primary)
    record["reason"] = reason
    return record


def skipped_record(reason: str, at: datetime, timing: Timing) -> dict[str, object]:
    """An API-primary cycle whose browser read was not made: ``browser_signed_out`` (the API
    worked while the TDX web session had expired) or ``shadow_off``. Not an error."""
    record = _base(at, "skipped", timing, "api")
    record["reason"] = reason
    return record


def fallback_record(reason: str, at: datetime, timing: Timing) -> dict[str, object]:
    """An API-primary cycle that the browser read instead: the API's reason code."""
    record = _base(at, "fallback", timing, "api")
    record["reason"] = reason
    return record


def _record_time(record: Mapping[str, object]) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(record["at"]))
    except (KeyError, ValueError):
        return None
    return at if at.tzinfo is not None else None


def _kept(record: Mapping[str, object], cutoff: datetime) -> bool:
    at = _record_time(record)
    return at is not None and at >= cutoff


class ShadowLog:
    """``api-shadow.jsonl``: one record per line, oldest first, kept ``KEEP_DAYS`` days. An
    unreadable line is skipped when read and dropped at the next write."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> list[dict[str, object]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        records = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and _record_time(record) is not None:
                records.append(record)
        return records

    def since(self, cutoff: datetime) -> list[dict[str, object]]:
        """The records from ``cutoff`` on."""
        return [record for record in self.read() if _kept(record, cutoff)]

    def append(self, record: Mapping[str, object], now: datetime) -> int:
        """Add ``record``, drop records older than ``KEEP_DAYS``, write atomically. Returns
        how many were dropped."""
        cutoff = now - timedelta(days=KEEP_DAYS)
        old = self.read()
        kept = [record for record in old if _kept(record, cutoff)]
        lines = [json.dumps(r, separators=(",", ":"), sort_keys=True) for r in (*kept, record)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temp, self.path)
        return len(old) - len(kept)


def shadow_log_path(settings: Settings) -> Path:
    """``api-shadow.jsonl`` in the TDX data folder."""
    return paths.tdx_dir(settings) / SHADOW_FILE_NAME


# --- one shadow read ---------------------------------------------------------------------


def failure_reason(error: Exception) -> str:
    """A fixed code for why the API part failed; the class name when nothing better fits.
    Never the message."""
    if isinstance(error, ApiError | TokenError | BrowserStartError):
        return error.reason
    if isinstance(error, DashboardStructureError):
        return "structure"
    if isinstance(error, NotSignedInError):
        return "signed_out"
    if isinstance(error, ConfigProblem):
        return "config"
    if isinstance(error, keyring.errors.KeyringError):
        return "keychain"
    return type(error).__name__


def read_only_memory(settings: Settings) -> DeskMemory | None:
    """The desk-member memory as the browser's run left it, read only; None when it is off."""
    rules = settings.tdx.rules
    if not rules.desk_memory:
        return None
    return DeskMemory(
        desk_memory_path(settings), forget_days=rules.desk_memory_forget_days, read_only=True
    )


def judge_read(
    settings: Settings,
    run: AuditRun,
    rows: Sequence[TicketRow],
    feeds: Mapping[int, TicketFeed],
    forms: Mapping[int, EventForm],
    memory: DeskMemory | None,
) -> list[Finding]:
    """The rules over one source's data, at the browser run's time, with the memory only
    asked (never taught): the same for both sources."""
    tdx = settings.tdx
    result = evaluate(
        list(rows),
        feeds,
        run.now,
        rule_settings(tdx),
        ZoneInfo(tdx.display_timezone),
        known_desk=memory.knows if memory is not None else None,
        forms=forms,
    )
    return list(result.findings)


def read_api(settings: Settings, client: TdxApiClient, run: AuditRun) -> SourceRead:
    """The API's rows, and the feeds and forms of the tickets the browser's plan picked."""
    tdx = settings.tdx
    rules = rule_settings(tdx)
    tz = ZoneInfo(tdx.display_timezone)
    widgets, app_id, _ = api_source.read_reports(tdx, client)
    rows = [row for kind in KINDS for row in widgets[kind].rows]
    plan = plan_feeds(run.rows, run.now, rules, tz)
    # The API's own rows give the requester; the browser's fill in a ticket the API lacks.
    by_id = {row.ticket_id: row for row in (*run.rows, *rows)}
    feeds = api_source.read_feeds(
        client,
        tdx,
        app_id,
        by_id,
        plan.feed_ticket_ids,
        run.now,
        form_ticket_ids=set(plan.form_ticket_ids),
        form_options=form_options(tdx),
    )
    memory = read_only_memory(settings)
    findings = judge_read(settings, run, rows, feeds.feeds, feeds.forms, memory)
    return SourceRead(rows, feeds.feeds, findings)


ClientFactory = Callable[[Settings, float], TdxApiClient]


def default_client(settings: Settings, deadline: float) -> TdxApiClient:
    """A client whose token is refreshed headless when needed, sending nothing after
    ``deadline`` (``time.monotonic``)."""
    return TdxApiClient(settings.tdx, TokenSource(browser_fetcher(settings)), deadline=deadline)


class ShadowRunner:
    """The API read of one ``tdx watch`` cycle, compared and recorded: ``runner(run)`` returns
    the line for the cycle's output and never raises.

    It posts nothing and writes only ``api-shadow.jsonl``. A token that needs the API's own
    sign-in sends one notice a day through ``notify`` (the personal chat).
    """

    def __init__(
        self,
        settings: Settings,
        notify: Callable[[str], None],
        *,
        client_factory: ClientFactory = default_client,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        tdx = settings.tdx.model_copy(update={"base_url": settings.tdx.base_url.strip()})
        self._settings = settings.model_copy(update={"tdx": tdx})
        self._client_factory = client_factory
        self._clock = clock
        self._log = ShadowLog(shadow_log_path(settings))
        self._notices = ApiSignInNotices(notify, ZoneInfo(settings.schedule.timezone))

    def __call__(self, run: AuditRun) -> str:
        settings = self._settings
        started = self._clock()
        client: TdxApiClient | None = None
        try:
            client = self._client_factory(settings, started + settings.tdx.shadow.max_seconds)
            api = read_api(settings, client, run)
            diff = compare(from_run(settings, run), api)
        except Exception as error:  # the trial must never fail the browser's cycle
            reason = failure_reason(error)
            self._notices.handled(error, run.now)
            record = failure_record(reason, run.now, self._timing(run, client, started))
            line = failure_line(reason)
            log.info("api shadow not compared", extra={"reason": reason})
        else:
            record = shadow_record(diff, run.now, self._timing(run, client, started))
            line = shadow_line(diff)
            log.info(
                "api shadow compared",
                extra={
                    "identical": diff.findings.identical,
                    "differences": diff.findings.differences,
                    "rows_matched": sum(rows.matched for rows in diff.rows.values()),
                    "feeds_matched": diff.feeds.matched,
                    "seconds": record["seconds"],
                    "api_requests": record["api_requests"],
                },
            )
        try:
            dropped = self._log.append(record, run.now)
        except OSError as error:
            log.info("api shadow record not saved", extra={"error": type(error).__name__})
        else:
            if dropped:
                log.info("api shadow records dropped", extra={"dropped": dropped})
        return line

    def _timing(self, run: AuditRun, client: TdxApiClient | None, started: float) -> Timing:
        return Timing(
            browser_seconds=run.read_seconds,
            api_seconds=self._clock() - started,
            api_requests=sum(client.requests.values()) if client is not None else 0,
            browser_non_get=_non_get(run),
        )


def from_run(settings: Settings, run: AuditRun) -> SourceRead:
    """A cycle's own run, judged again the way the compared read is (no LLM step)."""
    memory = read_only_memory(settings)
    findings = judge_read(settings, run, run.rows, run.feeds.feeds, run.feeds.forms, memory)
    return SourceRead(run.rows, run.feeds.feeds, findings)


def read_browser_beside(
    settings: Settings, run: AuditRun, *, headless: bool
) -> tuple[SourceRead, int]:
    """The browser's read beside an API run: the dashboard, then the feeds and forms of the
    tickets the API run's plan picked, at its time; judged with the memory read only. Returns
    the read and the browser's allowlisted non-GET request count."""
    dashboard, feeds, rows = read_browser(
        settings, headless=headless, now=run.now, progress=None, plan_rows=run.rows
    )
    memory = read_only_memory(settings)
    findings = judge_read(settings, run, rows, feeds.feeds, feeds.forms, memory)
    non_get = len(dashboard.allowed_non_get) + len(feeds.allowed_non_get)
    return SourceRead(rows, feeds.feeds, findings), non_get


BrowserReader = Callable[[Settings, AuditRun], tuple[SourceRead, int]]
BROWSER_SIGNED_OUT = "Browser shadow: skipped (TDX sign-in expired; the API still answers)."


class BrowserShadow:
    """``tdx.source = "api"``: one record per cycle in ``api-shadow.jsonl``, and while
    ``tdx.shadow.enabled`` the browser's read beside the API's, compared. ``shadow(run)``
    returns the line for the cycle's output (None when there is nothing to add) and never
    raises.

    A cycle the browser read instead of the API (``run.fallback``) is recorded as a fallback
    and not compared. The browser read never posts and writes nothing but the record; a
    signed-out browser is recorded as skipped (``browser_signed_out``), not as a failure.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        headless: bool,
        reader: BrowserReader | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        tdx = settings.tdx.model_copy(update={"base_url": settings.tdx.base_url.strip()})
        self._settings = settings.model_copy(update={"tdx": tdx})
        self._reader = reader or (
            lambda settings, run: read_browser_beside(settings, run, headless=headless)
        )
        self._clock = clock
        self._log = ShadowLog(shadow_log_path(settings))

    def __call__(self, run: AuditRun) -> str | None:
        settings = self._settings
        line: str | None = None
        if run.fallback is not None:
            timing = Timing(run.read_seconds, None, 0, _non_get(run))
            record = fallback_record(run.fallback, run.now, timing)
        elif not settings.tdx.shadow.enabled:
            record = skipped_record("shadow_off", run.now, self._timing(run, None, 0))
        else:
            record, line = self._compare(run)
        try:
            dropped = self._log.append(record, run.now)
        except OSError as error:
            log.info("browser shadow record not saved", extra={"error": type(error).__name__})
        else:
            if dropped:
                log.info("api shadow records dropped", extra={"dropped": dropped})
        return line

    def _compare(self, run: AuditRun) -> tuple[dict[str, object], str]:
        settings = self._settings
        started = self._clock()
        non_get = 0
        try:
            browser, non_get = self._reader(settings, run)
            diff = compare(browser, from_run(settings, run))
        except NotSignedInError:
            log.info("browser shadow skipped", extra={"reason": "browser_signed_out"})
            timing = self._timing(run, self._clock() - started, non_get)
            return skipped_record("browser_signed_out", run.now, timing), BROWSER_SIGNED_OUT
        except Exception as error:  # the comparison must never fail the API's cycle
            reason = failure_reason(error)
            log.info("browser shadow not compared", extra={"reason": reason})
            timing = self._timing(run, self._clock() - started, non_get)
            record = failure_record(reason, run.now, timing, primary="api")
            return record, failure_line(reason, BROWSER_LABEL)
        record = shadow_record(
            diff, run.now, self._timing(run, self._clock() - started, non_get), primary="api"
        )
        log.info(
            "browser shadow compared",
            extra={
                "identical": diff.findings.identical,
                "differences": diff.findings.differences,
                "rows_matched": sum(rows.matched for rows in diff.rows.values()),
                "feeds_matched": diff.feeds.matched,
                "seconds": record["seconds"],
                "api_requests": record["api_requests"],
            },
        )
        return record, shadow_line(diff, BROWSER_LABEL)

    @staticmethod
    def _timing(run: AuditRun, browser_seconds: float | None, non_get: int) -> Timing:
        return Timing(browser_seconds, run.read_seconds, run.api_requests or 0, non_get)


def _non_get(run: AuditRun) -> int:
    return len(run.dashboard.allowed_non_get) + len(run.feeds.allowed_non_get)


# --- the report --------------------------------------------------------------------------


@dataclass
class _Tally:
    """One period's records, added up."""

    compared: int = 0
    failed: int = 0  # the compared source's read failed
    identical: int = 0
    rows_matched: int = 0
    rows_total: int = 0
    feeds_matched: int = 0
    feeds_total: int = 0
    unreadable: int = 0
    failures: Counter[str] = field(default_factory=Counter)
    fallbacks: Counter[str] = field(default_factory=Counter)  # the API primary only
    skips: Counter[str] = field(default_factory=Counter)  # the API primary only
    mismatches: Counter[str] = field(default_factory=Counter)
    tickets: dict[str, set[tuple]] = field(default_factory=dict)
    by_key: Counter[tuple[str, tuple]] = field(default_factory=Counter)
    browser_seconds: list[float] = field(default_factory=list)
    api_seconds: list[float] = field(default_factory=list)
    requests: list[int] = field(default_factory=list)
    non_get: list[int] = field(default_factory=list)

    @property
    def cycles(self) -> int:
        return self.compared + self.failed + sum(self.fallbacks.values()) + sum(self.skips.values())


def _ids(value: object) -> list[int]:
    """A record's id list; ValueError for anything else, so the record is skipped."""
    if not isinstance(value, list) or not all(isinstance(i, int) for i in value):
        raise ValueError("not a list of ticket ids")
    return value


def _record_mismatches(record: Mapping) -> list[tuple[str, tuple]]:
    """(category, key) for every mismatch of one compared record; the key is a ticket id, or
    (id, kind, reason) for a finding."""
    found: list[tuple[str, tuple]] = []
    for rows in record["rows"].values():
        found += [("row_only_browser", (i,)) for i in _ids(rows["only_browser"])]
        found += [("row_only_api", (i,)) for i in _ids(rows["only_api"])]
        for name, ids in rows["differs"].items():
            found += [(name, (i,)) for i in _ids(ids)]
    feeds = record["feeds"]
    found += [("feed_only_browser", (i,)) for i in _ids(feeds["only_browser"])]
    found += [("feed_only_api", (i,)) for i in _ids(feeds["only_api"])]
    for check, ids in feeds["differs"].items():
        found += [(check, (i,)) for i in _ids(ids)]
    for side in ("browser", "api"):
        found += [
            (f"finding_only_{side}", (int(key[0]), str(key[1]), str(key[2])))
            for key in record["findings"][f"only_{side}"]
        ]
    return [(category, key) for category, key in found if category in CATEGORIES]


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tally(records: Iterable[Mapping]) -> _Tally:
    tally = _Tally()
    for record in records:
        try:
            result = record["result"]
            if result in ("failed", "fallback", "skipped"):
                reason = str(record.get("reason") or "unknown")
                if result == "failed":
                    tally.failures[reason] += 1
                    tally.failed += 1
                elif result == "fallback":
                    tally.fallbacks[reason] += 1
                else:
                    tally.skips[reason] += 1
                continue
            if result != "compared":
                raise ValueError("unknown result")
            found = _record_mismatches(record)
            rows = record["rows"].values()
            feeds = record["feeds"]
            identical = bool(record["findings"]["identical"])
            rows_matched = sum(int(r["matched"]) for r in rows)
            rows_total = sum(int(r["browser"]) + len(_ids(r["only_api"])) for r in rows)
            feeds_total = (
                int(feeds["compared"])
                + len(_ids(feeds["only_browser"]))
                + len(_ids(feeds["only_api"]))
            )
            seconds = record["seconds"]
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            tally.unreadable += 1
            continue
        tally.compared += 1
        tally.identical += identical
        tally.rows_matched += rows_matched
        tally.rows_total += rows_total
        tally.feeds_matched += int(feeds["matched"])
        tally.feeds_total += feeds_total
        for category, key in found:
            tally.mismatches[category] += 1
            tally.tickets.setdefault(category, set()).add(key[:1])
            tally.by_key[(category, key)] += 1
        if isinstance(seconds.get("browser"), int | float):
            tally.browser_seconds.append(float(seconds["browser"]))
        if isinstance(seconds.get("api"), int | float):
            tally.api_seconds.append(float(seconds["api"]))
        tally.requests.append(int(record.get("api_requests") or 0))
        tally.non_get.append(int(record.get("browser_non_get") or 0))
    return tally


def _reasons(counts: Counter[str]) -> str:
    """`` (needs_sign_in 2, timeout 1)``, or nothing when there are none."""
    joined = ", ".join(f"{reason} {n}" for reason, n in sorted(counts.items()))
    return f" ({joined})" if joined else ""


def _comparison_lines(tally: _Tally, verdict: str) -> list[str]:
    """Matches, mismatches, medians and the verdict over a period's compared cycles."""
    lines = [
        f"Rows matched: {_percent(tally.rows_matched, tally.rows_total)} "
        f"({tally.rows_matched} of {tally.rows_total}). "
        f"Feeds matched: {_percent(tally.feeds_matched, tally.feeds_total)} "
        f"({tally.feeds_matched} of {tally.feeds_total}). "
        f"Cycles with identical findings: {_percent(tally.identical, tally.compared)} "
        f"({tally.identical} of {tally.compared}).",
    ]
    if tally.mismatches:
        parts = [
            f"{label}: {tally.mismatches[code]} ({_plural(len(tally.tickets[code]), 'ticket')})"
            for code, label in CATEGORIES.items()
            if tally.mismatches[code]
        ]
        lines.append(f"Mismatches: {', '.join(parts)}.")
    else:
        lines.append("Mismatches: none.")
    browser, api = _median(tally.browser_seconds), _median(tally.api_seconds)
    if tally.compared:
        lines.append(
            f"Median per compared cycle: browser {_seconds(browser)}, API {_seconds(api)}; "
            f"API requests {_median(tally.requests):g}; "
            f"browser allowed non-GET requests {_median(tally.non_get):g}."
        )
    line = f"Verdict: {verdict} in {tally.identical} of {tally.compared + tally.failed} cycles"
    if browser is not None and api is not None:
        faster = browser - api
        line += f"; API reads were {abs(faster):.1f}s {'faster' if faster >= 0 else 'slower'}"
    return [*lines, line + "."]


def _period_words(days: int, period: str | None) -> str:
    """``last 3 days``, or ``period`` (``today``) in its place."""
    return period if period is not None else f"last {_plural(days, 'day')}"


def _shadow_period(tally: _Tally, days: int, period: str | None = None) -> list[str]:
    """The browser primary, the API compared."""
    return [
        f"API shadow, {_period_words(days, period)}: {_plural(tally.cycles, 'cycle')}; "
        f"{tally.compared} compared, {_plural(tally.failed, 'API failure')}"
        f"{_reasons(tally.failures)}.",
        *_comparison_lines(tally, "API matched the browser"),
    ]


def _api_period(tally: _Tally, days: int, period: str | None = None) -> list[str]:
    """The API primary, with its browser fallbacks; the browser compared."""
    fallbacks = sum(tally.fallbacks.values())
    skipped = sum(tally.skips.values())
    shadow = f"Browser shadow: {tally.compared} compared"
    if tally.failed:
        shadow += f", {_plural(tally.failed, 'browser failure')}{_reasons(tally.failures)}"
    if skipped:
        shadow += f", {skipped} skipped{_reasons(tally.skips)}"
    lines = [
        f"API primary, {_period_words(days, period)}: {_plural(tally.cycles, 'cycle')}; "
        f"{tally.cycles - fallbacks} read through the API, "
        f"{_plural(fallbacks, 'browser fallback')}{_reasons(tally.fallbacks)}.",
        shadow + ".",
    ]
    if tally.compared + tally.failed:
        lines += _comparison_lines(tally, "the browser matched the API")
    return lines


def compare_report(
    records: Sequence[Mapping], *, days: int, show_ids: bool = False, period: str | None = None
) -> list[str]:
    """The ``tdx api compare`` lines over ``records``: for each period found (the browser
    primary, then the API primary), counts, percentages, medians and a verdict, and for the
    API primary its cycles by source with the fallbacks by reason; ticket ids only with
    ``show_ids``. Unreadable records are skipped and counted.

    ``period`` names the stretch in both period headings instead of ``last N days`` (the
    day-end notice says ``today``)."""
    periods: dict[str, list[Mapping]] = {"browser": [], "api": []}
    unknown = 0
    for record in records:
        primary = record.get("primary", "browser") if isinstance(record, Mapping) else None
        if primary in periods:
            periods[str(primary)].append(record)
        else:
            unknown += 1
    shadow, api = _tally(periods["browser"]), _tally(periods["api"])
    unreadable = unknown + shadow.unreadable + api.unreadable
    lines: list[str] = []
    if shadow.cycles:
        lines += _shadow_period(shadow, days, period)
    if api.cycles:
        lines += _api_period(api, days, period)
    if not lines:
        lines.append(
            f"No API shadow records in the last {_plural(days, 'day')}. Set tdx.source = "
            '"shadow" and run tdx watch (docs/USAGE.md, "Shadow mode").'
        )
    if unreadable:
        lines.append(f"Unreadable records skipped: {unreadable}.")
    by_key = shadow.by_key + api.by_key
    if show_ids and by_key and (shadow.cycles or api.cycles):
        lines.append(SHOW_IDS_WARNING)
        order = list(CATEGORIES)
        for (category, key), n in sorted(
            by_key.items(), key=lambda item: (order.index(item[0][0]), item[0][1])
        ):
            what = "  ".join(str(part) for part in key)
            lines.append(f"  {category}  {what}  {_plural(n, 'cycle')}")
    return lines


def _seconds(value: float | None) -> str:
    return f"{value:.1f}s" if value is not None else "n/a"
