"""One audit-and-send cycle, and the desk-hours loop that repeats it (``tdx watch``).

``run_audit`` is the read-only audit that ``tdx audit`` prints: read the dashboard, open the
feeds that need it (and the event request forms' descriptions), apply the rules (with the
local desk-member memory unless it is off), then the follow-up classifier on "requestor
wrote last" findings when ``llm.enabled`` is set (``--no-llm`` turns it off for one run).
Its data class is always ``institutional``; ``tdx demo`` reuses ``read_and_judge``,
``classify_step`` and ``draft_card`` with ``synthetic`` on the synthetic app.
``send_alerts`` plans the findings that are new or due again, posts one card and records the
state only after a successful post. Cards go to a named destination (``personal`` or
``desk``), each with its own state file; real ticket cards go
to ``desk`` only with ``teams.desk.approved``. Operator notices always go to ``personal``.
``watch`` runs the cycle every ``tdx.watch.interval_minutes`` during desk hours, in the
foreground, and waits out an expired sign-in instead of exiting; its clock, sleep, cycle and
notifier are injected, so tests need no browser, network or real time.

Shift summaries (``tdx.summary``): the loop also wakes at the configured summary
times, and a cycle that runs while a slot is due posts ``send_summary`` instead of the alert
card, recording its findings as alerted. The day's first summary posted is full
(every open finding, with the mention); later slots post a delta (new findings and due
reminders, cleared ones as a count) or nothing when no row is new or due. While summaries go to the
desk chat, a desk watch's alert cards go to the personal chat (``alert_destination``,
``tdx.summary.desk_alerts`` / ``personal_alerts``). The summary state file
(``summary-state-desk.json``) holds the last slot dealt with, the last one posted and the
(ticket id, kind) set open at that post. A slot posted late, or not at all
within ``grace_minutes``, sends one plain notice to the personal chat. The summary also lists
the coming week's events and marks possible duplicates, both worked out in memory
from this run; "ready to close" findings go on it but, unless ``tdx.alerts.alert_ready_to_close``,
never on an alert card or into the alert state.

Urgent event findings (``tdx.summary.urgent_events_to_desk``): an event finding within
``tdx.events.near_event_days`` of its event reaches the desk chat in the run it first appears,
on one short "Event soon" card per run (``send_urgent``), or marked "(event soon)" in the
summary when one is due that run. The summary state remembers it, so the next delta does not
list it as new and it is not posted again until it clears.

Day-end notice (``tdx.watch.day_end_notice``, while ``tdx.source`` is "shadow" or "api"): at
each desk day's close the loop sends one plain notice to the personal chat with the day's
comparison (counts only) and whether the stored API token lasts through the next desk day,
saying when to refresh it if not. ``day-end-state.json`` holds the last day sent.

Output and log records carry counts, kinds, reason codes, times and error class names only:
never a ticket title, a name or the webhook URL.
"""

import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import keyring.errors

from ops_suite.common import paths
from ops_suite.common.config import ConfigProblem, Settings, TdxSettings
from ops_suite.common.exitcodes import (
    EXIT_BROWSER,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_STRUCTURE,
)
from ops_suite.common.hints import RUN
from ops_suite.common.llm import LlmClient, LlmDayUsage
from ops_suite.common.schedule import DeskHours, SummarySlots
from ops_suite.common.scrub import DataClass
from ops_suite.common.teams import (
    Destination,
    TeamsPostError,
    get_webhook_url,
    post_card,
    set_webhook_command,
    text_card,
)
from ops_suite.tdx import api_source
from ops_suite.tdx.alerts import (
    DESK_STATE_FILE_NAME,
    STATE_FILE_NAME,
    SUMMARY_STATE_FILE_NAMES,
    AlertPlan,
    AlertPolicy,
    AlertState,
    Reader,
    StateKey,
    SummaryState,
    alertable,
    card_links,
    counts_phrase,
    fit_card,
    fit_delta_card,
    fit_summary_card,
    fit_urgent_card,
    open_findings,
    plan_alerts,
    summary_change,
    urgent_event,
)
from ops_suite.tdx.api_client import TdxApiClient
from ops_suite.tdx.api_token import (
    TOKEN_LIFETIME,
    ApiToken,
    TokenError,
    TokenSource,
    browser_fetcher,
)
from ops_suite.tdx.browser import BrowserStartError, TdxBrowser
from ops_suite.tdx.card_time import card_time
from ops_suite.tdx.dashboard import (
    DashboardRead,
    DashboardStructureError,
    NotSignedInError,
    read_dashboard,
)
from ops_suite.tdx.desk_memory import MEMORY_FILE_NAME, DeskMemory
from ops_suite.tdx.duplicates import possible_duplicates
from ops_suite.tdx.event_form import EventForm, EventFormOptions
from ops_suite.tdx.feed_types import TicketFeed
from ops_suite.tdx.followup import (
    VERDICTS_FILE_NAME,
    FollowUpOutcome,
    VerdictCache,
    classify_follow_ups,
    off_outcome,
)
from ops_suite.tdx.guard import BUILT_IN_ALLOWED, parse_allowed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import (
    AuditResult,
    Finding,
    RuleSettings,
    apply_verdicts,
    evaluate,
    plan_feeds,
    strong_desk_names,
)
from ops_suite.tdx.saved import load_saved_dashboard
from ops_suite.tdx.session import on_tdx
from ops_suite.tdx.session_log import record_expiry
from ops_suite.tdx.tickets import FeedRead, read_feeds
from ops_suite.tdx.week import events_this_week

log = logging.getLogger(__name__)

SLEEP_CHUNK_SECONDS = 30
SET_WEBHOOK_HINT = f'run "{RUN} teams set-webhook" first'
STATE_FILE_NAMES: dict[Destination, str] = {
    "personal": STATE_FILE_NAME,
    "desk": DESK_STATE_FILE_NAME,
}
DESK_NOT_APPROVED = (
    "real ticket cards go to the desk chat only with teams.desk.approved = true in your own "
    "config, set only once the desk has approved real cards there "
    '(docs/USAGE.md, "Desk chat"); nothing was read or sent'
)
NO_PERSONAL_FOR_NOTICES = (
    "ops-suite: warning: sign-in notices need a personal webhook; they go to your own chat, "
    f'never the desk chat. Run "{RUN} teams set-webhook" to get them. Watching anyway.'
)
NOTICE_FALLBACK = "ops-suite: TDX auditor notice"
CYCLE_ERRORS: dict[type[Exception], int] = {
    BrowserStartError: EXIT_BROWSER,  # includes the profile-in-use error
    DashboardStructureError: EXIT_STRUCTURE,
}

# A summary cycle that starts more than this after its slot is marked late.
SUMMARY_LATE_AFTER = timedelta(minutes=15)

SendStatus = Literal[
    "sent", "nothing_new", "failed", "no_webhook", "not_approved", "no_change", "off"
]
SummaryKind = Literal["full", "delta"]
NO_PERSONAL_FOR_ALERTS = (
    "ops-suite: warning: alert cards go to your personal chat while the desk chat gets "
    "summaries only (tdx.summary.personal_alerts), and no personal webhook is saved, so no "
    f'alert cards are sent. Run "{RUN} teams set-webhook" to get them.'
)


# Where an audit reads the queue: the browser (the default) or the TDX Web API.
Source = Literal["browser", "api"]


@dataclass(frozen=True, slots=True)
class AuditRun:
    dashboard: DashboardRead
    feeds: FeedRead
    result: AuditResult
    rows: tuple[TicketRow, ...]
    now: datetime
    desk_remembered: int | None = None  # members in desk-members.json; None when it is off
    llm: FollowUpOutcome | None = None
    llm_today: LlmDayUsage | None = None  # today's LLM totals after the step; None when off
    read_seconds: float | None = None  # the read and the rules, before any LLM step
    source: Source = "browser"  # the reader that read this run
    # tdx.source = "api": the API's reason code when the browser read this cycle instead.
    fallback: str | None = None
    api_requests: int | None = None  # requests the API read sent; None for the browser


@dataclass(frozen=True, slots=True)
class SendOutcome:
    status: SendStatus
    new: int = 0
    reminders: int = 0
    overflow: int = 0
    reason: str = ""
    destination: Destination = "personal"
    summary: bool = False  # a shift summary, not an alert card
    open: int = 0  # summaries only: every open finding, on the card or not
    delta: bool = False  # summaries only: a later slot's delta, not the full summary
    cleared: int = 0  # deltas only: findings open at the last post and gone now
    slot: datetime | None = None  # summaries only: the slot
    # Summaries only: the (ticket id, kind) set to remember as open at this post.
    open_keys: frozenset[StateKey] = frozenset()
    # The urgent event findings this cycle took to the desk chat, on their own card or in the
    # summary; None when there were none.
    urgent: "UrgentOutcome | None" = None


@dataclass(frozen=True, slots=True)
class UrgentOutcome:
    """Urgent event findings new for the desk chat: on their own card (``sent`` or
    ``failed``, retried next run) or marked in this cycle's summary (``in_summary``)."""

    status: Literal["sent", "failed", "in_summary"]
    rows: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """Post the shift summary for ``slot`` (aware, desk time) in this cycle: the full summary,
    or a delta against ``previous``, the set open at the last post (at ``since``)."""

    slot: datetime
    late: bool = False
    kind: SummaryKind = "full"
    since: datetime | None = None  # the last slot posted; a delta needs it
    previous: frozenset[StateKey] = frozenset()


# --- one audit ---------------------------------------------------------------------------


def alert_state_path(settings: Settings, destination: Destination = "personal") -> Path:
    """``alerts-state.json`` (``alerts-state-desk.json`` for the desk chat) in the TDX data
    folder; ticket ids, kinds and times only. One file per destination, so each chat gets
    every open finding once."""
    return paths.tdx_dir(settings) / STATE_FILE_NAMES[destination]


def card_reader(settings: Settings, destination: Destination) -> Reader:
    """Who ``destination``'s cards speak to: the desk chat by its team's name
    (``teams.desk.team_name``), the personal chat as "you". Both name the alternative-host
    mailbox from ``tdx.events.alt_host_mailbox``."""
    return Reader(
        destination,
        settings.teams.desk.team_name,
        alt_host_mailbox=settings.tdx.events.alt_host_mailbox,
    )


def summary_state_path(settings: Settings, destination: Destination) -> Path:
    """``summary-state-<destination>.json`` in the TDX data folder: the last slot posted."""
    return paths.tdx_dir(settings) / SUMMARY_STATE_FILE_NAMES[destination]


def desk_allowed(settings: Settings, destination: Destination) -> bool:
    """Whether real ticket cards may go to ``destination``: always for ``personal``, and for
    ``desk`` only with ``teams.desk.approved``. Synthetic cards do not ask."""
    return destination != "desk" or settings.teams.desk.approved


def start_address(settings: Settings, tdx: TdxSettings) -> tuple[str, str]:
    """(source, url) to start from: the setting, else the saved address, else the start page."""
    if tdx.dashboard_url.strip():
        return "setting", tdx.dashboard_url.strip()
    saved = load_saved_dashboard(paths.tdx_dashboard_file(settings))
    if saved is not None:
        if not on_tdx(saved.url, tdx.base_url):
            raise ConfigProblem(
                "the saved dashboard address is not on tdx.base_url's host; "
                f'run "{RUN} tdx set-dashboard" again'
            )
        return "saved", saved.url
    return "start page", tdx.base_url


def desk_memory_path(settings: Settings) -> Path:
    """``desk-members.json`` in the TDX data folder; salted hashes and times only."""
    return paths.tdx_dir(settings) / MEMORY_FILE_NAME


def open_desk_memory(settings: Settings) -> DeskMemory | None:
    """The desk-member memory, or None when ``tdx.rules.desk_memory`` is off (no file)."""
    rules = settings.tdx.rules
    if not rules.desk_memory:
        return None
    return DeskMemory(desk_memory_path(settings), forget_days=rules.desk_memory_forget_days)


def judge(
    rows: Sequence[TicketRow],
    feeds: Mapping[int, TicketFeed],
    now: datetime,
    rules: RuleSettings,
    tz: ZoneInfo,
    memory: DeskMemory | None,
    forms: Mapping[int, EventForm] | None = None,
) -> tuple[AuditResult, int | None]:
    """Apply the rules, with the desk-member memory when there is one, and the parsed event
    request forms.

    The memory first learns this run's strong desk names, then answers ``known_desk``, and is
    saved afterwards. Returns the result and the number of members remembered (None without
    a memory).
    """
    if memory is None:
        return evaluate(rows, feeds, now, rules, tz, forms=forms), None
    memory.remember(strong_desk_names(rows, feeds), now)
    result = evaluate(rows, feeds, now, rules, tz, known_desk=memory.knows, forms=forms)
    memory.save(now)
    return result, len(memory)


def verdict_cache_path(settings: Settings) -> Path:
    """``llm-verdicts.json`` in the TDX data folder; ids, ISO times and verdict codes only."""
    return paths.tdx_dir(settings) / VERDICTS_FILE_NAME


def follow_up(
    settings: Settings,
    result: AuditResult,
    rows: Sequence[TicketRow],
    feeds: FeedRead,
    rules: RuleSettings,
    now: datetime,
    *,
    use_llm: bool,
) -> tuple[AuditResult, FollowUpOutcome, LlmDayUsage | None]:
    """The follow-up classifier on the rules' result, when ``use_llm`` and ``llm.enabled``.

    The data class is always ``institutional``: this path only ever reads the live TDX queue, so the
    calls also need ``llm.allow_institutional_data``. Off, no client is built and nothing changes.
    """
    if not (use_llm and settings.llm.enabled):
        return result, off_outcome(result), None
    return classify_step(
        settings,
        result,
        rows,
        feeds,
        rules,
        now,
        client=LlmClient(settings),
        data_class="institutional",
    )


def classify_step(
    settings: Settings,
    result: AuditResult,
    rows: Sequence[TicketRow],
    feeds: FeedRead,
    rules: RuleSettings,
    now: datetime,
    *,
    client: LlmClient,
    data_class: DataClass,
) -> tuple[AuditResult, FollowUpOutcome, LlmDayUsage]:
    """Classify with ``client`` and apply the verdicts. Internal: ``follow_up`` calls it with
    ``institutional`` for the live queue, and only ``tdx demo`` with ``synthetic``."""
    desk_names = [*strong_desk_names(rows, feeds.feeds), *rules.extra_staff_names]
    outcome = classify_follow_ups(
        result,
        rows,
        feeds.feeds,
        desk_names,
        client=client,
        cache=VerdictCache(verdict_cache_path(settings)),
        data_class=data_class,
        max_input_chars=settings.llm.max_input_chars,
        now=now,
        protect=[*feeds.failed, *feeds.deferred],
    )
    return apply_verdicts(result, outcome.verdicts), outcome, client.used_today()


def rule_settings(tdx: TdxSettings) -> RuleSettings:
    rules = tdx.rules
    events = tdx.events
    return RuleSettings(
        stale_hours=rules.stale_hours,
        event_over_hours=rules.event_over_hours,
        event_tbd_days=rules.event_tbd_days,
        extra_staff_names=tuple(rules.extra_staff_names),
        skip_desk_requestors=rules.skip_desk_requestors,
        form_titles=tuple(events.form_titles),
        outreach_days=events.outreach_days,
        details_pending_days=events.details_pending_days,
        urgent_days=events.urgent_days,
        near_event_days=events.near_event_days,
        close_after_days=rules.close_after_days,
    )


def form_options(tdx: TdxSettings) -> EventFormOptions:
    """The event request form's option prefixes from ``tdx.events``."""
    events = tdx.events
    return EventFormOptions(
        marketing_only_prefixes=tuple(events.marketing_only_prefixes),
        av_service_prefixes=tuple(events.av_service_prefixes),
        room_space_prefixes=tuple(events.room_space_prefixes),
        known_option_prefixes=tuple(events.known_option_prefixes),
    )


def failed_step(error: DashboardStructureError) -> str:
    """Which part of ``run_audit`` raised: "the dashboard" or "the ticket pages"."""
    return getattr(error, "audit_step", "the dashboard")


def run_audit(
    settings: Settings,
    *,
    headless: bool,
    progress: Callable[[int, int], None] | None,
    use_llm: bool = True,
    source: Source = "browser",
) -> AuditRun:
    """Read the dashboard and the feeds that need it (read-only), apply the rules, then the
    follow-up classifier (see ``follow_up``, always ``institutional`` here).

    ``source`` "api" reads the same data through the TDX Web API (``read_and_judge_api``).
    Raises BrowserStartError, NotSignedInError and DashboardStructureError (with
    ``failed_step``) as ``tdx audit`` always has, and with "api" also ApiError and
    TokenError. Settings are checked by the caller.
    """
    started = time.monotonic()
    if source == "api":
        run = read_and_judge_api(settings, progress=progress)
    else:
        run = read_and_judge(settings, headless=headless, progress=progress)
    return finish_audit(settings, run, time.monotonic() - started, use_llm=use_llm)


def finish_audit(
    settings: Settings, run: AuditRun, read_seconds: float, *, use_llm: bool
) -> AuditRun:
    """A read's run with its time and the follow-up step (see ``follow_up``) applied."""
    run = replace(run, read_seconds=read_seconds)
    result, outcome, today = follow_up(
        settings,
        run.result,
        run.rows,
        run.feeds,
        rule_settings(settings.tdx),
        run.now,
        use_llm=use_llm,
    )
    return replace(run, result=result, llm=outcome, llm_today=today)


def read_and_judge(
    settings: Settings,
    *,
    headless: bool,
    progress: Callable[[int, int], None] | None,
) -> AuditRun:
    """The browser read and the rules, shared by ``run_audit`` and ``tdx demo``; no LLM step
    (``llm`` is None). Raises as ``run_audit`` does."""
    now = datetime.now(ZoneInfo(settings.tdx.display_timezone))
    dashboard, feeds, rows = read_browser(settings, headless=headless, now=now, progress=progress)
    return _judged(settings, dashboard, feeds, rows, now)


def read_browser(
    settings: Settings,
    *,
    headless: bool,
    now: datetime,
    progress: Callable[[int, int], None] | None,
    plan_rows: Sequence[TicketRow] | None = None,
) -> tuple[DashboardRead, FeedRead, list[TicketRow]]:
    """The browser read at ``now``: the dashboard's rows, then the feeds and event forms of
    the tickets the rules plan for: on ``plan_rows`` when given (another source's rows, so
    both read the same tickets), else on the dashboard's own. Raises as ``run_audit`` does."""
    tdx = settings.tdx.model_copy(update={"base_url": settings.tdx.base_url.strip()})
    allowed = BUILT_IN_ALLOWED + parse_allowed(tdx.allowed_requests)
    _, start_url = start_address(settings, tdx)
    rules = rule_settings(tdx)
    tz = ZoneInfo(tdx.display_timezone)
    step = "the dashboard"
    try:
        with TdxBrowser(
            paths.tdx_profile_dir(settings),
            tdx.base_url,
            headless=headless,
            allowed_requests=allowed,
            launch_timeout_seconds=tdx.browser_launch_timeout_seconds,
        ) as browser:
            dashboard = read_dashboard(browser, tdx, now, start_url=start_url)
            rows = [row for widget in dashboard.widgets.values() for row in widget.rows]
            plan = plan_feeds(rows if plan_rows is None else plan_rows, now, rules, tz)
            step = "the ticket pages"
            # The page's own rows win; the other source's fill in a ticket the page lacks.
            feeds = read_feeds(
                browser,
                tdx,
                {row.ticket_id: row for row in (*(plan_rows or ()), *rows)},
                plan.feed_ticket_ids,
                now,
                progress,
                form_ticket_ids=set(plan.form_ticket_ids),
                form_options=form_options(tdx),
            )
    except DashboardStructureError as error:
        error.audit_step = step  # type: ignore[attr-defined]
        raise
    return dashboard, feeds, rows


def read_and_judge_api(
    settings: Settings,
    *,
    progress: Callable[[int, int], None] | None,
    client: TdxApiClient | None = None,
) -> AuditRun:
    """``read_and_judge`` through the TDX Web API: the two reports instead of the widgets,
    the feed and ticket endpoints instead of the ticket pages; the same rules after. The
    client asks for a fresh token headless when it needs one. Raises ApiError, TokenError
    (``needs_sign_in``: a fresh token needs the API's own sign-in; see ``ApiSignInNotices``)
    and DashboardStructureError."""
    tdx = settings.tdx.model_copy(update={"base_url": settings.tdx.base_url.strip()})
    now = datetime.now(ZoneInfo(tdx.display_timezone))
    if client is None:
        client = TdxApiClient(tdx, TokenSource(browser_fetcher(settings)))
    step = "the reports"
    try:
        api_source.report_ids(tdx)  # ConfigProblem before any request
        queue = api_source.read_queue(
            tdx, client, now, rule_settings(tdx), form_options(tdx), progress
        )
    except DashboardStructureError as error:
        error.audit_step = step  # type: ignore[attr-defined]
        raise
    log.info("api requests", extra={"requests": dict(queue.requests)})
    run = _judged(settings, queue.dashboard, queue.feeds, list(queue.rows), now)
    return replace(run, source="api", api_requests=sum(queue.requests.values()))


def _judged(
    settings: Settings,
    dashboard: DashboardRead,
    feeds: FeedRead,
    rows: list[TicketRow],
    now: datetime,
) -> AuditRun:
    tdx = settings.tdx
    result, remembered = judge(
        rows,
        feeds.feeds,
        now,
        rule_settings(tdx),
        ZoneInfo(tdx.display_timezone),
        open_desk_memory(settings),
        feeds.forms,
    )
    return AuditRun(
        dashboard=dashboard,
        feeds=feeds,
        result=result,
        rows=tuple(rows),
        now=now,
        desk_remembered=remembered,
    )


# --- send --------------------------------------------------------------------------------


def teams_label(destination: Destination) -> str:
    """``Teams`` for the personal chat, ``Teams (desk)`` for the desk chat."""
    return "Teams" if destination == "personal" else f"Teams ({destination})"


def format_send_outcome(outcome: SendOutcome) -> str:
    """The one ``Teams:`` line: counts and a reason code only."""
    label = teams_label(outcome.destination)
    if outcome.summary and outcome.status == "sent" and outcome.delta:
        return (
            f"{label}: delta posted ({outcome.new} new, {outcome.cleared} cleared, "
            f"{outcome.open} open)."
        )
    if outcome.summary and outcome.status == "sent":
        return f"{label}: summary posted ({outcome.open} open, {outcome.new} new)."
    if outcome.status == "no_change":
        slot = f"{outcome.slot:%H:%M} " if outcome.slot is not None else ""
        return f"{label}: {slot}summary skipped (nothing new to show)."
    if outcome.status == "off":
        why = "no personal webhook" if outcome.reason == "no_personal_webhook" else "summaries only"
        return f"{label}: no alert card ({why})."
    if outcome.summary and outcome.status == "failed":
        return f"{label}: summary not posted ({outcome.reason}); will retry next run."
    if outcome.status == "sent":
        return f"{label}: sent 1 card ({counts_phrase(outcome.new, outcome.reminders)})."
    if outcome.status == "nothing_new":
        return f"{label}: nothing new."
    if outcome.status == "failed":
        return f"{label}: not sent ({outcome.reason}); will retry next run."
    if outcome.status == "not_approved":
        return f"{label}: not sent; teams.desk.approved is false."
    return f'{label}: not sent; run "{set_webhook_command(outcome.destination)}" first.'


def format_urgent(urgent: UrgentOutcome) -> str:
    """The desk chat's urgent event part of the cycle line: counts and a reason code only."""
    label = teams_label("desk")
    if urgent.status == "sent":
        return f"{label}: urgent event card ({urgent.rows})."
    if urgent.status == "in_summary":
        return f"{label}: urgent events in the summary ({urgent.rows})."
    return f"{label}: urgent event card not posted ({urgent.reason}); will retry next run."


@dataclass(frozen=True, slots=True)
class CardDraft:
    plan: AlertPlan  # every finding due now; cleared records already applied to the state
    fitted: AlertPlan  # the part of ``plan`` on the card (the rest is overflow)
    card: dict[str, object] | None  # None when nothing is due


def draft_card(
    run: AuditRun, settings: Settings, state: AlertState, destination: Destination = "personal"
) -> CardDraft:
    """Plan the findings due now against ``state`` (dropping cleared records from it, not
    saved) and build the one card, with titles only if ``destination`` shows them. Shared by
    ``send_alerts`` and ``tdx demo``."""
    tdx = settings.tdx
    rows_by_id = {row.ticket_id: row for row in run.rows}
    plan = plan_alerts(
        alertable(run.result.findings, alert_ready_to_close=tdx.alerts.alert_ready_to_close),
        state,
        run.now,
        AlertPolicy.from_settings(tdx.alerts),
        seen_ticket_ids=set(rows_by_id),
        unsettled_ticket_ids=set(run.feeds.failed) | set(run.feeds.deferred),
        queue_complete=not any(w.hit_page_limit for w in run.dashboard.widgets.values()),
    )
    plan.apply_cleared(state)
    if not plan.on_card:
        return CardDraft(plan, plan, None)
    summary = _summary_counts(run)
    links = card_links(rows_by_id, [due.finding.ticket_id for due in plan.on_card], tdx)
    fitted, card = fit_card(
        plan,
        rows_by_id,
        links,
        run.now,
        ZoneInfo(tdx.display_timezone),
        summary,
        title_chars=tdx.alerts.title_chars,
        show_titles=settings.teams.show_titles(destination),
        reader=card_reader(settings, destination),
    )
    return CardDraft(plan, fitted, card)


def _summary_counts(run: AuditRun) -> dict[str, int]:
    return {
        "unclaimed": run.result.counts.get("rows_unclaimed", 0),
        "feeds_read": len(run.feeds.feeds),
        "feeds_failed": len(run.feeds.failed),
    }


def _plan_all(run: AuditRun, settings: Settings, state: AlertState) -> AlertPlan:
    """Every alertable finding due now (new or a reminder), with no row cap; cleared records
    applied to ``state`` (not saved)."""
    rows_by_id = {row.ticket_id: row for row in run.rows}
    alerts = settings.tdx.alerts
    findings = alertable(run.result.findings, alert_ready_to_close=alerts.alert_ready_to_close)
    plan = plan_alerts(
        findings,
        state,
        run.now,
        replace(AlertPolicy.from_settings(alerts), max_rows_per_card=max(len(findings), 1)),
        seen_ticket_ids=set(rows_by_id),
        unsettled_ticket_ids=set(run.feeds.failed) | set(run.feeds.deferred),
        queue_complete=not any(w.hit_page_limit for w in run.dashboard.widgets.values()),
    )
    plan.apply_cleared(state)
    return plan


@dataclass(frozen=True, slots=True)
class SummaryDraft:
    plan: AlertPlan  # every finding due now; cleared records already applied to the state
    record: AlertPlan  # the due findings shown by id on the card: what a post records
    card: dict[str, object] | None  # None for a delta with nothing to say
    open: int
    new: int
    shown: int
    kind: SummaryKind = "full"
    cleared: int = 0
    open_keys: frozenset[StateKey] = frozenset()  # what the post leaves as the open set
    urgent: int = 0  # rows marked "(event soon)"


def draft_summary(
    run: AuditRun,
    settings: Settings,
    state: AlertState,
    destination: Destination,
    request: SummaryRequest,
    urgent: Set[StateKey] = frozenset(),
) -> SummaryDraft:
    """The summary card for ``request``, with titles only if ``destination`` shows them.

    New and cleared are counted against ``request.previous``, the alertable findings open at
    the last post ("ready to close" is never new unless ``tdx.alerts.alert_ready_to_close``).

    - **Full** (the day's first post): the events this week, every open finding (the new ones
      marked, possible duplicates pointed out) and the configured mentions.
    - **Delta** (later slots): rows only for the new findings and the reminders due under the
      alert policy (``state``), the cleared ones as a count; no events and no mention. Its
      ``card`` is None when no row is new, due or urgent, cleared findings or not: the slot
      is skipped and ``open_keys`` leaves the cleared ones out.

    ``urgent``: urgent event findings new for the chat this cycle. They go into this summary,
    marked "(event soon)", instead of on a card of their own; a delta lists them even when
    they were open at the last post (cleared and back since).
    """
    tdx = settings.tdx
    rows_by_id = {row.ticket_id: row for row in run.rows}
    plan = _plan_all(run, settings, state)
    findings = open_findings(run.result.findings)
    alert_on = alertable(findings, alert_ready_to_close=tdx.alerts.alert_ready_to_close)
    change = summary_change(
        alert_on,
        request.previous,
        seen_ticket_ids=set(rows_by_id),
        unsettled_ticket_ids=set(run.feeds.failed) | set(run.feeds.deferred),
        queue_complete=not any(w.hit_page_limit for w in run.dashboard.widgets.values()),
    )
    tz = ZoneInfo(tdx.display_timezone)
    rules = rule_settings(tdx)
    duplicates = possible_duplicates(
        run.rows, tdx.rules.duplicate_title_similarity, rules.form_titles
    )
    show_titles = settings.teams.show_titles(destination)
    card: dict[str, object] | None
    if request.kind == "delta":
        if request.since is None:
            raise ValueError("a delta summary needs the last post's time")
        reminder_keys = {
            (d.finding.ticket_id, d.finding.kind) for d in plan.on_card if d.why == "reminder"
        } - change.new
        listed = change.new | reminder_keys | urgent
        rows = [f for f in alert_on if (f.ticket_id, f.kind) in listed]
        if not rows:
            # Nothing to act on: in a shared chat that card is noise. The cleared findings
            # still leave the open set, so a later update does not count them again.
            empty = AlertPlan(on_card=(), overflow=0, cleared=())
            return SummaryDraft(plan, empty, None, len(findings), 0, 0, "delta", 0, change.open)
        links = card_links(rows_by_id, [finding.ticket_id for finding in rows], tdx)
        # The lead counts tickets, not findings: one ticket can carry two findings, and a
        # ticket cleared of one finding still needs attention while another stays open.
        open_ids = {finding.ticket_id for finding in findings}
        cleared_ids = {ticket_id for ticket_id, _ in change.cleared} - open_ids
        shown, card = fit_delta_card(
            rows,
            change.new,
            reminder_keys,
            len(cleared_ids),
            len(open_ids),
            rows_by_id,
            links,
            request.since,
            run.now,
            tz,
            _summary_counts(run),
            title_chars=tdx.alerts.title_chars,
            show_titles=show_titles,
            max_rows=tdx.alerts.max_rows_per_card,
            duplicates=duplicates,
            urgent_keys=urgent,
            reader=card_reader(settings, destination),
        )
    else:
        events = events_this_week(
            run.rows,
            run.feeds.forms,
            run.result.event_states,
            run.now,
            tz,
            rules,
            tdx.summary.events_days,
        )
        ids = [*(event.ticket_id for event in events), *(f.ticket_id for f in findings)]
        links = card_links(rows_by_id, ids, tdx)
        shown, card = fit_summary_card(
            findings,
            change.new,
            rows_by_id,
            links,
            request.slot,
            run.now,
            tz,
            _summary_counts(run),
            title_chars=tdx.alerts.title_chars,
            show_titles=show_titles,
            max_rows=tdx.alerts.max_rows_per_card,
            late=request.late,
            mention=tdx.summary.mention,
            mention_people=tdx.summary.mention_people,
            events=events,
            duplicates=duplicates,
            since=request.since,
            urgent_keys=urgent,
            reader=card_reader(settings, destination),
        )
    shown_keys = {(finding.ticket_id, finding.kind) for finding in shown}
    record = AlertPlan(
        on_card=tuple(
            d for d in plan.on_card if (d.finding.ticket_id, d.finding.kind) in shown_keys
        ),
        overflow=0,
        cleared=(),
    )
    return SummaryDraft(
        plan,
        record,
        card,
        len(findings),
        len(change.new),
        len(shown),
        request.kind,
        len(change.cleared),
        change.open,
        len(shown_keys & set(urgent)),
    )


def send_summary(
    run: AuditRun,
    settings: Settings,
    state_path: Path,
    url: str | None,
    destination: Destination,
    request: SummaryRequest,
    urgent: Set[StateKey] = frozenset(),
) -> SendOutcome:
    """Post the shift summary (full or delta, ``request.kind``) to ``destination`` instead of
    this cycle's alert card.

    After a successful post, every new or due finding shown on it is recorded in
    ``state_path`` as alerted, so the next cycle does not repeat it; due findings counted only
    in a "+N more" line stay due. Cleared records are dropped (and saved) whatever happens to
    the post. A delta with no new, due or urgent row posts nothing (``no_change``). ``desk``
    without ``teams.desk.approved`` sends nothing and touches no state. The caller records the
    slot and the outcome's ``open_keys`` itself (``SummaryState``). ``urgent``: the urgent
    event findings new for the chat, marked on the summary instead of a card of their own.
    """
    if not desk_allowed(settings, destination):
        log.info("tdx summary", extra={"outcome": "not_approved", "destination": destination})
        return SendOutcome("not_approved", destination=destination, summary=True)
    if url is None:
        return SendOutcome("no_webhook", destination=destination, summary=True)
    state = AlertState(state_path)
    draft = draft_summary(run, settings, state, destination, request, urgent)
    record = draft.record
    delta = draft.kind == "delta"
    counts = {
        "kind": draft.kind,
        "open": draft.open,
        "shown": draft.shown,
        "kinds": dict(Counter(f.kind for f in open_findings(run.result.findings))),
        "new": draft.new,
        "summary_cleared": draft.cleared,
        "recorded": len(record.on_card),
        "cleared": len(draft.plan.cleared),
        "slot": request.slot.isoformat(),
        "late": request.late,
        "mention": "none" if delta else settings.tdx.summary.mention,
        "destination": destination,
        "urgent": draft.urgent,
    }

    def outcome(
        status: SendStatus, new: int = 0, reminders: int = 0, reason: str = ""
    ) -> SendOutcome:
        return SendOutcome(
            status,
            new=new,
            reminders=reminders,
            overflow=0 if delta or status != "sent" else draft.open - draft.shown,
            reason=reason,
            destination=destination,
            summary=True,
            open=draft.open,
            delta=delta,
            cleared=draft.cleared,
            slot=request.slot,
            open_keys=draft.open_keys,
            urgent=(
                UrgentOutcome("in_summary", draft.urgent)
                if status == "sent" and draft.urgent
                else None
            ),
        )

    if draft.card is None:
        state.save()  # only the cleared records changed
        log.info("tdx summary", extra={"outcome": "no_change", **counts})
        return outcome("no_change")
    try:
        status = post_card(url, draft.card, timeout=settings.teams.timeout_seconds)
    except TeamsPostError as error:
        state.save()  # only the cleared records changed
        log.info(
            "tdx summary",
            extra={"outcome": "failed", "status": error.status, "reason": error.reason, **counts},
        )
        return outcome("failed", new=draft.new, reason=error.reason)
    record.record(state, run.now)
    state.save()
    log.info("tdx summary", extra={"outcome": "sent", "status": status, **counts})
    return outcome("sent", new=draft.new, reminders=record.reminders)


def alert_destination(settings: Settings, destination: Destination) -> Destination | None:
    """Where a ``tdx watch --to <destination>`` cycle posts its alert card.

    While shift summaries go to the desk chat, a desk watch posts no alert cards there unless
    ``tdx.summary.desk_alerts``; they go to the personal chat with ``personal_alerts``, or
    nowhere (None). Otherwise, as before, to ``destination``.
    """
    summary = settings.tdx.summary
    summaries_to_desk = summary.enabled and summary.destination == "desk"
    if destination != "desk" or not summaries_to_desk or summary.desk_alerts:
        return destination
    return "personal" if summary.personal_alerts else None


def urgent_events_on(settings: Settings, destination: Destination) -> bool:
    """Whether a ``tdx watch --to <destination>`` cycle takes urgent event findings to the desk
    chat at once: only a desk watch, while summaries go to the desk chat, with
    ``tdx.summary.urgent_events_to_desk`` and ``teams.desk.approved``."""
    summary = settings.tdx.summary
    return (
        destination == "desk"
        and summary.enabled
        and summary.destination == "desk"
        and summary.urgent_events_to_desk
        and settings.teams.desk.approved
    )


def desk_urgent(
    run: AuditRun, settings: Settings, known: Set[StateKey]
) -> tuple[frozenset[StateKey], tuple[Finding, ...]]:
    """(``known`` less the findings that cleared this run, the urgent event findings not in
    it): what the desk chat has been shown and still stands, and what it needs now.

    Urgent means an event finding within ``tdx.events.near_event_days`` of its event
    (``urgent_event``). Clearing follows the alert state's rule: a finding gone from a ticket
    judged this run, or from a queue read in full."""
    rows_by_id = {row.ticket_id: row for row in run.rows}
    alert_on = alertable(
        open_findings(run.result.findings),
        alert_ready_to_close=settings.tdx.alerts.alert_ready_to_close,
    )
    change = summary_change(
        alert_on,
        known,
        seen_ticket_ids=set(rows_by_id),
        unsettled_ticket_ids=set(run.feeds.failed) | set(run.feeds.deferred),
        queue_complete=not any(w.hit_page_limit for w in run.dashboard.widgets.values()),
    )
    still = frozenset(known) - change.cleared
    near = settings.tdx.events.near_event_days
    urgent = tuple(
        finding
        for finding in alert_on
        if urgent_event(finding, near) and (finding.ticket_id, finding.kind) not in still
    )
    return still, urgent


def send_urgent(
    run: AuditRun,
    settings: Settings,
    summary_state: SummaryState,
    url: str | None,
    urgent: Sequence[Finding],
    known: Set[StateKey],
) -> UrgentOutcome | None:
    """Post the urgent event findings to the desk chat on one short card (none when there
    are none), then remember them: in ``summary_state`` (shown, so the next delta does not
    list them as new and they are not posted again until they clear) and in the desk's alert
    state, as a summary post would. A failed post records nothing, so the next run tries
    again. ``known`` (cleared findings dropped) is saved either way."""
    if not urgent:
        summary_state.update_known(known)
        return None
    tdx = settings.tdx
    if url is None:
        summary_state.update_known(known)
        return UrgentOutcome("failed", len(urgent), "no_webhook")
    rows_by_id = {row.ticket_id: row for row in run.rows}
    alert_state = AlertState(alert_state_path(settings, "desk"))
    plan = _plan_all(run, settings, alert_state)
    links = card_links(rows_by_id, [finding.ticket_id for finding in urgent], tdx)
    shown, card = fit_urgent_card(
        urgent,
        rows_by_id,
        links,
        run.now,
        ZoneInfo(tdx.display_timezone),
        title_chars=tdx.alerts.title_chars,
        show_titles=settings.teams.show_titles("desk"),
        max_rows=tdx.alerts.max_rows_per_card,
        reader=card_reader(settings, "desk"),
    )
    counts = {
        "rows": len(shown),
        "kinds": dict(Counter(finding.kind for finding in shown)),
        "destination": "desk",
    }
    try:
        status = post_card(url, card, timeout=settings.teams.timeout_seconds)
    except TeamsPostError as error:
        alert_state.save()  # only the cleared records changed
        summary_state.update_known(known)
        log.info(
            "tdx urgent events",
            extra={"outcome": "failed", "status": error.status, "reason": error.reason, **counts},
        )
        return UrgentOutcome("failed", len(shown), error.reason)
    shown_keys = {(finding.ticket_id, finding.kind) for finding in shown}
    AlertPlan(
        on_card=tuple(
            due for due in plan.on_card if (due.finding.ticket_id, due.finding.kind) in shown_keys
        ),
        overflow=0,
        cleared=(),
    ).record(alert_state, run.now)
    alert_state.save()
    summary_state.update_known(known, shown=shown_keys)
    log.info("tdx urgent events", extra={"outcome": "sent", "status": status, **counts})
    return UrgentOutcome("sent", len(shown))


def send_cycle(
    run: AuditRun,
    settings: Settings,
    destination: Destination,
    summary: SummaryRequest | None = None,
) -> SendOutcome:
    """What one ``tdx watch`` cycle posts: the summary to ``destination`` when ``summary`` is
    given, otherwise the alert card to ``alert_destination``, each against that chat's own
    alert state and saved webhook. Alert cards moved to the personal chat are not sent
    (``off``) when no personal webhook is saved, and neither are alert cards with nowhere to
    go.

    With ``urgent_events_on``, urgent event findings new for the desk chat also go there at
    once: marked in the summary when one is due, else on one short card of their own beside
    the alert card, which is unchanged. Either way the desk summary state remembers them."""
    summary_state: SummaryState | None = None
    known: frozenset[StateKey] = frozenset()
    urgent: tuple[Finding, ...] = ()
    if urgent_events_on(settings, destination):
        summary_state = SummaryState(summary_state_path(settings, "desk"))
        known, urgent = desk_urgent(run, settings, summary_state.known)
    if summary is not None:
        if summary_state is not None:
            # The post's open set joins ``known`` when the caller records the slot.
            summary_state.update_known(known)
        url = _saved_url(destination)
        state_path = alert_state_path(settings, destination)
        keys = frozenset((finding.ticket_id, finding.kind) for finding in urgent)
        return send_summary(run, settings, state_path, url, destination, summary, keys)
    outcome = _send_alert_card(run, settings, destination)
    if summary_state is None:
        return outcome
    sent = send_urgent(run, settings, summary_state, _saved_url("desk"), urgent, known)
    return outcome if sent is None else replace(outcome, urgent=sent)


def _send_alert_card(run: AuditRun, settings: Settings, destination: Destination) -> SendOutcome:
    alerts_to = alert_destination(settings, destination)
    if alerts_to is None:
        return SendOutcome("off", reason="summaries_only", destination=destination)
    url = _saved_url(alerts_to)
    if alerts_to != destination and url is None:
        return SendOutcome("off", reason="no_personal_webhook", destination=alerts_to)
    return send_alerts(run, settings, alert_state_path(settings, alerts_to), url, alerts_to)


def send_alerts(
    run: AuditRun,
    settings: Settings,
    state_path: Path,
    url: str | None,
    destination: Destination = "personal",
) -> SendOutcome:
    """Post one card with the findings due now to ``destination``; record them in
    ``state_path`` (that destination's file) only after a successful post.

    Cleared records are dropped (and saved) whatever happens to the post. ``desk`` without
    ``teams.desk.approved`` sends nothing and touches no state.
    """
    if not desk_allowed(settings, destination):
        log.info("tdx alerts", extra={"outcome": "not_approved", "destination": destination})
        return SendOutcome("not_approved", destination=destination)
    if url is None:
        return SendOutcome("no_webhook", destination=destination)
    state = AlertState(state_path)
    draft = draft_card(run, settings, state, destination)
    plan, fitted, card = draft.plan, draft.fitted, draft.card
    if card is None:
        state.save()
        log.info(
            "tdx alerts",
            extra={
                "outcome": "nothing_new",
                "cleared": len(plan.cleared),
                "destination": destination,
            },
        )
        return SendOutcome("nothing_new", destination=destination)

    counts = {
        "rows": len(fitted.on_card),
        "kinds": dict(Counter(due.finding.kind for due in fitted.on_card)),
        "new": fitted.new,
        "reminders": fitted.reminders,
        "overflow": fitted.overflow,
        "cleared": len(plan.cleared),
        "destination": destination,
    }
    try:
        status = post_card(url, card, timeout=settings.teams.timeout_seconds)
    except TeamsPostError as error:
        state.save()  # only the cleared records changed
        log.info(
            "tdx alerts",
            extra={"outcome": "failed", "status": error.status, "reason": error.reason, **counts},
        )
        return SendOutcome(
            "failed", fitted.new, fitted.reminders, fitted.overflow, error.reason, destination
        )
    fitted.record(state, run.now)
    state.save()
    log.info("tdx alerts", extra={"outcome": "sent", "status": status, **counts})
    return SendOutcome("sent", fitted.new, fitted.reminders, fitted.overflow, "", destination)


# --- the loop ----------------------------------------------------------------------------


def _sleep_until(
    target: datetime, clock: Callable[[], datetime], sleep: Callable[[float], None]
) -> None:
    """Sleep in chunks of 30 s or less, re-reading the clock each time, so Ctrl+C is quick
    and a computer waking from sleep past ``target`` returns at once."""
    while (remaining := (target - clock()).total_seconds()) > 0:
        sleep(min(remaining, SLEEP_CHUNK_SECONDS))


def sign_in_notice(retry_minutes: int) -> str:
    """The Teams notice sent when the TDX sign-in has expired (no ticket data)."""
    return (
        f'TDX sign-in expired. On the computer running the auditor, run "{RUN} tdx login"; '
        f"the auditor keeps running and picks up again within {retry_minutes} minutes."
    )


API_SIGN_IN_NOTICE = f'TDX API token needs a sign-in: run "{RUN} tdx api token --headed".'


class ApiSignInNotices:
    """A headless token refresh that reached the identity provider (TokenError
    ``needs_sign_in``) in a background loop: a plain notice to the personal chat, at most
    once a day.

    It is the Web API's own sign-in, not the TDX session's, which may be fine. So it is never
    a cycle failure, never counts toward a stop, and never touches the browser's signed-out
    state or the session log: the caller records nothing else for it.
    """

    def __init__(self, notify: Callable[[str], None], tz: ZoneInfo) -> None:
        self._notify = notify
        self._tz = tz
        self.day: date | None = None  # the day of the last notice

    def handled(self, error: Exception, now: datetime) -> bool:
        """True if ``error`` is ``needs_sign_in``: noticed (once a day) and nothing else to
        do. False for any other error, which the caller handles as before."""
        if not (isinstance(error, TokenError) and error.reason == "needs_sign_in"):
            return False
        today = now.astimezone(self._tz).date()
        if self.day != today:
            self.day = today
            log.info("api token needs a sign-in; notice sent")
            self._notify(API_SIGN_IN_NOTICE)
        return True


def _profile_in_use(error: Exception) -> bool:
    """The ``tdx login`` window has the profile open (someone is signing in)."""
    return isinstance(error, BrowserStartError) and error.reason == "profile_in_use"


def _failure_label(error: Exception) -> str:
    """``BrowserStartError: launch_timeout`` or the error class name: never the message, which
    can hold the profile path."""
    name = type(error).__name__
    return f"{name}: {error.reason}" if isinstance(error, BrowserStartError) else name


def busy_line(retry_minutes: int) -> str:
    """Printed once per spell while another window (``tdx login``) holds the profile."""
    return (
        'TDX browser profile in use (a "tdx login" window?); '
        f"checking again every {retry_minutes} min."
    )


def source_part(run: AuditRun) -> str:
    """``source: api`` or ``source: browser (fallback: needs_sign_in)``."""
    if run.fallback is not None:
        return f"source: {run.source} (fallback: {run.fallback})"
    return f"source: {run.source}"


def _cycle_line(
    run: AuditRun, outcome: SendOutcome, shadow: str | None = None, *, show_source: bool = False
) -> str:
    unclaimed = run.result.counts.get("rows_unclaimed", 0)
    feeds = len(run.feeds.feeds)
    findings = len(run.result.findings)
    after = f" {shadow}" if shadow else ""
    before = f"{source_part(run)}; " if show_source else ""
    urgent = f" {format_urgent(outcome.urgent)}" if outcome.urgent is not None else ""
    return (
        f"{before}{unclaimed} unclaimed, {feeds} feed{'' if feeds == 1 else 's'}, "
        f"{findings} finding{'' if findings == 1 else 's'}; {format_send_outcome(outcome)}"
        f"{urgent}{after}"
    )


SHADOW_ON = (
    "API shadow mode: each run also reads the same tickets through the TDX Web API and records "
    f'counts only; cards come from the browser. "{RUN} tdx api compare" shows the comparison.'
)
SHADOW_ERROR = "API shadow: not compared (error)."
API_PRIMARY_ON = (
    "Reading the queue through the TDX Web API; the browser reads any run the API cannot "
    "(each run's line names its source)."
)
BROWSER_SHADOW_ON = (
    "Browser shadow: each run also reads the same tickets through the browser and records "
    f'counts only; cards come from the API. "{RUN} tdx api compare" shows the comparison.'
)
BROWSER_SHADOW_ERROR = "Browser shadow: not compared (error)."


def _shadow_line(
    shadow: Callable[[AuditRun], str | None], run: AuditRun, error_line: str
) -> str | None:
    """The shadow step's line, or None when it has none. It handles its own failures;
    anything that still escapes it is logged by class name and never touches the cycle."""
    try:
        return shadow(run)
    except Exception as error:  # the comparison must never fail a cycle
        log.info("api shadow failed", extra={"reason": type(error).__name__})
        return error_line


def summary_skipped_notice(slot: datetime, reason: str) -> str:
    """``07:30 summary not posted: TDX sign-in expired``; no ticket data."""
    return f"{slot:%H:%M} summary not posted: {reason}"


def summary_late_notice(slot: datetime, posted: datetime) -> str:
    """``07:30 summary posted late at 08:12``; no ticket data."""
    return f"{slot:%H:%M} summary posted late at {posted:%H:%M}"


def _utc(when: datetime) -> datetime:
    return when.astimezone(UTC)


# --- the day-end notice ------------------------------------------------------------------

DAY_END_STATE_FILE = "day-end-state.json"
TOKEN_COMMAND = f'"{RUN} tdx api token --headed"'
DAY_END_MISMATCH_ADVICE = (
    "The browser and the API disagreed. To let the browser make the cards again, set "
    'tdx.source = "shadow" and restart tdx watch; "tdx api compare --days 1 --show-ids" lists '
    "the tickets on this computer."
)
# The day-end notice's line when no run recorded a comparison during the day.
DAY_END_NO_RECORDS = "No runs were recorded for comparison today."
TokenCode = Literal["ok", "refresh_now", "refresh_later", "missing", "off"]


def day_end_state_path(settings: Settings) -> Path:
    """``day-end-state.json`` in the TDX data folder: the last desk day whose notice went out."""
    return paths.tdx_dir(settings) / DAY_END_STATE_FILE


class DayEndState:
    """``day-end-state.json``: ``{"last_sent": "YYYY-MM-DD"}``, the local date of the last desk
    day whose day-end notice was sent. A missing or unreadable file counts as never sent: at
    worst one notice goes out twice."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_sent: date | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.last_sent = date.fromisoformat(str(data["last_sent"]))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("day-end state unreadable")

    def __repr__(self) -> str:
        return f"DayEndState(last_sent={self.last_sent!r})"

    def record(self, day: date) -> None:
        """Remember ``day`` as sent and write the file atomically."""
        self.last_sent = day
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps({"last_sent": day.isoformat()}), encoding="utf-8")
        os.replace(temp_path, self.path)


def token_line(token: ApiToken | None, now: datetime, hours: DeskHours) -> tuple[str, TokenCode]:
    """Whether the stored API token lasts through the next desk day, and when to refresh it if
    not: the notice's line and its log code.

    A token lasts ``TOKEN_LIFETIME`` from its refresh, so one refreshed after
    ``next_close - TOKEN_LIFETIME`` covers the whole next desk day. The arithmetic is on aware
    datetimes in UTC, so a DST change in between is counted right; the times shown are in the
    desk's time zone, 12-hour like the cards.
    """
    tz = hours.tz
    next_start, next_close = hours.next_window(now)
    if token is None or _utc(token.expires) <= _utc(now):
        return (
            "API token missing or expired. Refresh it before the desk opens "
            f"{card_time(next_start, tz, weekday=True)}: {TOKEN_COMMAND}.",
            "missing",
        )
    expires = card_time(token.expires, tz, weekday=True)
    if _utc(token.expires) >= _utc(next_close):
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        covers = "tomorrow" if next_start.date() == tomorrow else f"{next_start:%a}"
        return f"API token valid until {expires}; it covers {covers}.", "ok"
    before = (
        f"API token expires {expires}, before the desk closes "
        f"{card_time(next_close, tz, weekday=True)}."
    )
    refresh_after = max(_utc(now), _utc(next_close) - TOKEN_LIFETIME)
    if refresh_after <= _utc(now):
        return f"{before} Refresh it now: {TOKEN_COMMAND}.", "refresh_now"
    return (
        f"{before} Refresh it after {card_time(refresh_after, tz, weekday=True)} so it lasts "
        f"the whole day: {TOKEN_COMMAND}.",
        "refresh_later",
    )


def day_end_notice(
    settings: Settings,
    window: tuple[datetime, datetime],
    now: datetime,
    hours: DeskHours,
    records: Sequence[Mapping],
    token: ApiToken | None,
) -> tuple[str, dict[str, object]]:
    """The day-end notice for the desk day ``window`` and its log extras. Pure: ``records``
    are the comparison records written since the window opened, ``token`` the stored API
    token (or None).

    The first line names the day and the token's state, so a phone's preview shows what to
    do; then the day's comparison, counts only (``compare_report`` never shows ticket ids
    here); then, while the API makes the cards, what to do about a mismatch.
    """
    # The comparison report lives beside the shadow runner, which imports this module.
    from ops_suite.tdx.shadow import compare_report

    if settings.tdx.api.enabled:
        token_text, code = token_line(token, now, hours)
    else:
        token_text, code = "", "off"
    day = window[0].astimezone(hours.tz)
    lines = [f"Desk day done ({day:%a}). {token_text}".rstrip()]
    if records:
        report = compare_report(records, days=1, show_ids=False, period="today")
    else:
        report = [DAY_END_NO_RECORDS]
    lines += report
    mismatches = any(
        line.startswith("Mismatches:") and line != "Mismatches: none." for line in report
    )
    if mismatches and settings.tdx.source == "api":
        lines.append(DAY_END_MISMATCH_ADVICE)
    compared = sum(1 for record in records if record.get("result") == "compared")
    extras: dict[str, object] = {
        "day": day.date().isoformat(),
        "compared": compared,
        "mismatches": mismatches,
        "token": code,
    }
    # Blank lines between them: card text is Markdown, where a single line break may be joined.
    return "\n\n".join(lines), extras


def day_end_on(settings: Settings) -> bool:
    """Whether ``tdx watch`` sends the day-end notice: the setting, and a source that records
    a comparison (``shadow`` or ``api``)."""
    return settings.tdx.watch.day_end_notice and settings.tdx.source in ("shadow", "api")


def _day_records(settings: Settings, since: datetime) -> list[dict[str, object]]:
    """The comparison records written from ``since`` on."""
    from ops_suite.tdx.shadow import ShadowLog, shadow_log_path  # see day_end_notice

    return ShadowLog(shadow_log_path(settings)).since(since)


def watch(
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    run_cycle: Callable[..., tuple[AuditRun, SendOutcome]],
    notify: Callable[[str], None],
    destination: Destination = "personal",
    shadow: Callable[[AuditRun], str | None] | None = None,
    day_end: Callable[[datetime], ApiToken | None] | None = None,
) -> int:
    """Run ``run_cycle`` every ``interval_minutes`` during desk hours until Ctrl+C.

    An expired session does not stop it: one notice (one more on each new local day), then a
    quiet check every ``signed_out_retry_minutes`` until a cycle succeeds again; the first
    expiry after a ``tdx login`` is written to ``session-log.json``. A
    profile-in-use error (the person's own ``tdx login`` window), signed in or out, is never a
    failure: one line per busy spell, then a quiet check every ``signed_out_retry_minutes``.
    Stops after ``failures_before_stop`` failed cycles in a row (one notice); expiry never
    counts and resets the count, since the browser started and reached TDX. A Teams post
    failure is not a cycle failure. Browser failures print and log a reason code only.

    ``destination`` is where ``run_cycle`` posts cards: its webhook must be saved, and
    ``desk`` needs ``teams.desk.approved``, or it returns EXIT_CONFIG before any cycle.
    Notices (``notify``) always go to the personal chat; with ``desk`` and no personal webhook
    it warns once and runs anyway.

    Shift summaries, when ``tdx.summary.enabled`` and its destination is ``destination``: the
    next wake is the earlier of the next interval run and the next summary slot. A slot is due
    from its time until ``grace_minutes`` later (and only inside desk hours) until posted; a
    cycle that runs while one is due is called as ``run_cycle(summary=SummaryRequest(...))``,
    otherwise as ``run_cycle()``. The request is ``full`` when nothing was posted yet that
    day (so a missed first slot makes the next one full), else a ``delta`` against the set open
    at the last post. A sent summary records its slot and open set in the summary state, so a
    restart in the same slot does not post again; a delta with nothing new or due records its
    slot without a post or a notice. A slot posted more than 15 minutes late, or skipped because no
    cycle posted it in time, sends one notice naming the time (and, when skipped, the last
    failure's reason).

    While summaries go to the desk chat, ``alert_destination`` says where a desk
    watch's alert cards go (the personal chat by default, with a start warning when no
    personal webhook is saved); ``run_cycle`` is expected to follow it (``send_cycle``).
    ``send_cycle`` may also post urgent event findings to the desk chat and update the
    summary state's ``known`` set, so the loop re-reads the state before and after a cycle.

    ``shadow``: called with each successful cycle's run after its post, it reads the same
    tickets through the other source, records the comparison and returns one line for the
    cycle's output (or None). It never posts, never touches the alert, summary or failure
    state, and nothing it raises fails the cycle.

    ``tdx.source = "api"``: ``run_cycle`` reads through the API and falls back to the browser
    for the cycle by itself (``ApiFirst``), so only the browser's errors reach this loop and
    are handled as above. Each cycle's line starts with its source.

    ``day_end``: the stored API token's reader (no refresh, no network). Given, and with
    ``day_end_on``, the loop also wakes at each desk window's close (unless the next run or a
    summary slot comes first) and sends one day-end notice to the personal chat
    (``day_end_notice``): the day's comparison, counts only, and the token's state. Any wake
    while the desk is closed sends the notice for the most recent closed day if it was not
    sent yet (``day-end-state.json``), so a restart or a wake from sleep after the close still
    sends it once; a day the loop slept through until the next open gets none. It never
    touches the failure count, the alert, summary or sign-in state, and nothing it raises
    stops the loop.
    """
    hours = DeskHours.from_settings(settings.schedule)
    interval = timedelta(minutes=settings.tdx.watch.interval_minutes)
    retry_minutes = settings.tdx.watch.signed_out_retry_minutes
    retry = timedelta(minutes=retry_minutes)
    limit = settings.tdx.watch.failures_before_stop
    if not desk_allowed(settings, destination):
        print(f"ops-suite: {DESK_NOT_APPROVED}.", file=sys.stderr)
        return EXIT_CONFIG
    if _saved_url(destination) is None:
        which = "Teams webhook" if destination == "personal" else f"{destination} webhook"
        print(
            f'ops-suite: no {which} is saved; run "{set_webhook_command(destination)}" first.',
            file=sys.stderr,
        )
        return EXIT_CONFIG
    alerts_to = alert_destination(settings, destination)
    if destination != "personal" and _saved_url("personal") is None:
        print(NO_PERSONAL_FOR_NOTICES, file=sys.stderr, flush=True)
        if alerts_to == "personal":
            print(NO_PERSONAL_FOR_ALERTS, file=sys.stderr, flush=True)

    def local(when: datetime) -> datetime:
        return when.astimezone(hours.tz)

    summary = settings.tdx.summary
    grace = timedelta(minutes=summary.grace_minutes)
    slots: SummarySlots | None = None
    summary_path: Path | None = None
    summary_state: SummaryState | None = None
    if summary.enabled and summary.destination == destination:
        slots = SummarySlots.from_settings(summary, settings.schedule)
        summary_path = summary_state_path(settings, destination)
        summary_state = SummaryState(summary_path)
    day_end_active = day_end is not None and day_end_on(settings)

    def next_run(cycle_start: datetime, step: timedelta) -> datetime:
        due = cycle_start + step
        if hours.is_open(due):
            wake = due
        else:
            wake = hours.next_open(due)
            window = hours.window_at(cycle_start) if day_end_active else None
            if window is not None and _utc(window[1]) < _utc(wake):
                wake = window[1]  # the close, for the day-end notice
        slot = slots.next_after(cycle_start) if slots is not None else None
        if slot is not None and _utc(slot) < _utc(wake):
            return slot
        return wake

    def due_slot(now: datetime) -> datetime | None:
        """The summary slot due now: passed, not posted, within grace, inside desk hours."""
        if slots is None or summary_state is None or not hours.is_open(now):
            return None
        slot = slots.latest_at_or_before(now)
        if slot is None or summary_state.posted(slot) or _utc(now) >= _utc(slot) + grace:
            return None
        return slot

    def send_day_end(now: datetime) -> None:
        """The day-end notice for the most recent closed day, unless it went out already."""
        assert day_end is not None
        try:
            window = hours.last_closed(now)
            if window is None:
                return
            day = local(window[0]).date()
            state = DayEndState(day_end_state_path(settings))
            if state.last_sent is not None and state.last_sent >= day:
                return
            token = day_end(now) if settings.tdx.api.enabled else None
            records = _day_records(settings, window[0])
            text, extras = day_end_notice(settings, window, now, hours, records, token)
            notify(text)
            # Recorded even when the post failed (the notifier logs that), so a broken webhook
            # does not send it again at every wake.
            state.record(day)
        except Exception as error:  # the notice must never stop the loop
            log.info("tdx watch day-end notice failed", extra={"reason": type(error).__name__})
            return
        print(f"{local(now):%H:%M} Day-end notice sent.", flush=True)
        log.info("tdx watch day-end notice", extra=extras)

    if alerts_to == destination:
        to_desk = "" if destination == "personal" else f" Cards go to the {destination} chat."
    elif alerts_to is None:
        to_desk = f" No alert cards; the {destination} chat gets summaries only."
    else:
        to_desk = (
            f" Alert cards go to the {alerts_to} chat; the {destination} chat gets summaries only."
        )
    print(
        f"Watching the TDX queue every {settings.tdx.watch.interval_minutes} min during desk "
        f"hours ({settings.schedule.timezone}).{to_desk} Ctrl+C stops.",
        flush=True,
    )
    if slots is not None:
        upcoming = slots.next_after(clock())
        nxt = f"; next {local(upcoming):%a %H:%M}" if upcoming is not None else ""
        print(f"Shift summaries go to the {destination} chat{nxt}.", flush=True)
    elif summary.enabled:
        print(
            f"Shift summaries go to the {summary.destination} chat (tdx.summary.destination); "
            f"this watch posts to the {destination} chat, so it sends none.",
            flush=True,
        )
    api_first = settings.tdx.source == "api"
    if api_first:
        print(API_PRIMARY_ON, flush=True)
        if shadow is not None and settings.tdx.shadow.enabled:
            print(BROWSER_SHADOW_ON, flush=True)
    elif shadow is not None:
        print(SHADOW_ON, flush=True)
    shadow_error = BROWSER_SHADOW_ERROR if api_first else SHADOW_ERROR
    # The summary slot due now and not yet posted, and why its last attempt failed.
    pending: datetime | None = None
    pending_reason: str | None = None
    failures = 0
    # The local day of the last sign-in notice; None while signed in.
    notice_day: date | None = None
    # True while the profile is in use, so the busy line prints once per spell.
    busy = False
    try:
        while True:
            now = clock()
            if summary_path is not None:
                # A cycle's urgent event card updates the file too: always start from it.
                summary_state = SummaryState(summary_path)
            due = due_slot(now)
            if pending is not None and due != pending:
                # Grace ran out, the desk closed or a newer slot came: this one is skipped.
                reason = pending_reason or "no run finished in time"
                log.info(
                    "tdx watch summary skipped",
                    extra={"slot": pending.isoformat(), "reason": reason},
                )
                notify(summary_skipped_notice(local(pending), reason))
                print(f"{local(now):%H:%M} {summary_skipped_notice(local(pending), reason)}.")
                pending_reason = None
            pending = due
            if not hours.is_open(now):
                if day_end_active:
                    send_day_end(now)
                wake = hours.next_open(now)
                print(f"Outside desk hours; next run {local(wake):%a %H:%M}.", flush=True)
                log.info("tdx watch waiting", extra={"next_run": wake.isoformat()})
                _sleep_until(wake, clock, sleep)
                continue
            signed_out = notice_day is not None
            request = None
            if pending is not None and summary_state is not None:
                full = summary_state.full_due(pending)
                request = SummaryRequest(
                    pending,
                    late=_utc(now) - _utc(pending) > SUMMARY_LATE_AFTER,
                    kind="full" if full else "delta",
                    since=summary_state.last_posted,
                    previous=summary_state.open,
                )
            try:
                run, outcome = run_cycle() if request is None else run_cycle(summary=request)
            except NotSignedInError:
                record_expiry(settings, now)
                if pending is not None:
                    pending_reason = "TDX sign-in expired"
                busy = False
                failures = 0  # the browser started and reached TDX
                wake = next_run(now, retry)
                today = local(now).date()
                if notice_day != today:
                    event = "tdx watch still signed out" if signed_out else "tdx watch signed out"
                    log.info(event, extra={"next_run": wake.isoformat()})
                    notice_day = today
                    notify(sign_in_notice(retry_minutes))
                    print(
                        f'{local(now):%H:%M} TDX sign-in expired; run "{RUN} tdx login". '
                        f"Checking again every {retry_minutes} min.",
                        file=sys.stderr,
                        flush=True,
                    )
                _sleep_until(wake, clock, sleep)
                continue
            except tuple(CYCLE_ERRORS) as error:
                if pending is not None:
                    pending_reason = (
                        "TDX browser profile in use"
                        if _profile_in_use(error)
                        else _failure_label(error)
                    )
                if _profile_in_use(error):
                    wake = next_run(now, retry)
                    if not busy:
                        busy = True
                        log.info("tdx watch profile busy", extra={"next_run": wake.isoformat()})
                        print(f"{local(now):%H:%M} {busy_line(retry_minutes)}", flush=True)
                    _sleep_until(wake, clock, sleep)
                    continue
                busy = False
                failures += 1
                label = _failure_label(error)
                wake = next_run(now, retry if signed_out else interval)
                log.info(
                    "tdx watch cycle failed",
                    extra={
                        "error": type(error).__name__,
                        "reason": (error.reason if isinstance(error, BrowserStartError) else None),
                        "failures": failures,
                        "next_run": wake.isoformat(),
                    },
                )
                if failures >= limit:
                    message = (
                        f"The TDX auditor stopped after {failures} failed runs ({label}). "
                        "Check the computer running it."
                    )
                    notify(message)
                    print(f"ops-suite: {message}", file=sys.stderr)
                    return next(
                        code for kind, code in CYCLE_ERRORS.items() if isinstance(error, kind)
                    )
                print(
                    f"{local(now):%H:%M} cycle failed ({label}); trying again at "
                    f"{local(wake):%H:%M}.",
                    flush=True,
                )
                _sleep_until(wake, clock, sleep)
                continue
            if signed_out:
                notice_day = None
                if run.source == "api":
                    # The API answered; the browser's own sign-in may still be expired.
                    print(f"{local(now):%H:%M} The TDX Web API answered; resuming.", flush=True)
                    log.info("tdx watch reading through the api again")
                else:
                    print(f"{local(now):%H:%M} Signed in again; resuming.", flush=True)
                    log.info("tdx watch signed in again")
            busy = False
            failures = 0
            if request is not None and summary_path is not None:
                summary_state = SummaryState(summary_path)  # as this cycle left it
                if outcome.status == "sent":
                    summary_state.record(request.slot, posted=True, open_keys=outcome.open_keys)
                    pending = pending_reason = None
                    if request.late:
                        notify(summary_late_notice(local(request.slot), local(clock())))
                elif outcome.status == "no_change":
                    # Nothing to say is not a miss: no notice, and the slot is done.
                    summary_state.record(request.slot, posted=False, open_keys=outcome.open_keys)
                    pending = pending_reason = None
                else:
                    pending_reason = (
                        f"Teams post failed ({outcome.reason})"
                        if outcome.status == "failed"
                        else f"Teams {outcome.status}"
                    )
            shadow_line = _shadow_line(shadow, run, shadow_error) if shadow is not None else None
            wake = next_run(now, interval)
            line = _cycle_line(run, outcome, shadow_line, show_source=api_first)
            print(f"{local(now):%H:%M} {line} Next run {local(wake):%H:%M}.", flush=True)
            log.info(
                "tdx watch cycle",
                extra={
                    "unclaimed": run.result.counts.get("rows_unclaimed", 0),
                    "feeds_read": len(run.feeds.feeds),
                    "feeds_failed": len(run.feeds.failed),
                    "findings": len(run.result.findings),
                    "desk_remembered": run.desk_remembered,
                    "llm": dict(run.llm.counts) if run.llm is not None else None,
                    "send": outcome.status,
                    "new": outcome.new,
                    "reminders": outcome.reminders,
                    "overflow": outcome.overflow,
                    "reason": outcome.reason,
                    "summary": request.slot.isoformat() if request is not None else None,
                    "summary_kind": request.kind if request is not None else None,
                    "urgent": outcome.urgent.status if outcome.urgent is not None else None,
                    "urgent_rows": outcome.urgent.rows if outcome.urgent is not None else 0,
                    "source": run.source,
                    "fallback": run.fallback,
                    "next_run": wake.isoformat(),
                },
            )
            _sleep_until(wake, clock, sleep)
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        log.info("tdx watch stopped", extra={"error": "KeyboardInterrupt"})
        return EXIT_OK


def _saved_url(destination: Destination) -> str | None:
    try:
        return get_webhook_url(destination)
    except keyring.errors.KeyringError:
        return None


def webhook_notifier(settings: Settings) -> Callable[[str], None]:
    """``notify`` for ``watch``: a plain card with no ticket data; failures are only logged.

    Always the personal webhook, whatever chat the cards go to: "sign in again" and "auditor
    stopped" are for the person running the auditor, never the desk chat.
    """

    def notify(text: str) -> None:
        try:
            url = get_webhook_url("personal")
            if url is None:
                return
            post_card(
                url,
                text_card("ops-suite: TDX auditor", text, fallback=NOTICE_FALLBACK),
                timeout=settings.teams.timeout_seconds,
            )
        except (TeamsPostError, keyring.errors.KeyringError) as error:
            log.info("tdx watch notice not sent", extra={"error": type(error).__name__})

    return notify
