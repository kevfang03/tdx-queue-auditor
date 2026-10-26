"""Command-line entry point: ``ops-suite mail scan`` and ``ops-suite tdx ...``."""

import argparse
import getpass
import json
import logging
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import keyring.errors
from pydantic import BaseModel, ValidationError

from ops_suite.common import paths
from ops_suite.common.autostart import (
    AgentStatus,
    AutoStart,
    LaunchctlError,
    StartupFolderError,
    UvNotFoundError,
    WindowsStartup,
    for_platform,
    keep_awake,
)
from ops_suite.common.config import ConfigProblem, Settings, TdxSettings, load_settings
from ops_suite.common.exitcodes import (
    EXIT_API,
    EXIT_BROWSER,
    EXIT_CANCELLED,
    EXIT_CONFIG,
    EXIT_LLM,
    EXIT_NOT_SIGNED_IN,
    EXIT_OK,
    EXIT_STRUCTURE,
    EXIT_TEAMS,
)
from ops_suite.common.hints import RUN
from ops_suite.common.llm import LlmClient, LlmFailed, LlmUnavailable, get_api_key, set_api_key
from ops_suite.common.logsetup import configure_logging
from ops_suite.common.runtime import is_store_python
from ops_suite.common.scrub import scrub
from ops_suite.common.teams import (
    DESTINATIONS,
    Destination,
    TeamsPostError,
    get_webhook_url,
    post_card,
    set_webhook_command,
    set_webhook_url,
    synthetic_test_card,
    with_mentions,
)
from ops_suite.mail.classify import MailRules
from ops_suite.mail.drop_folder import DropFolderMailSource
from ops_suite.mail.report import ReportWriter
from ops_suite.mail.scan import ScanResult, run_scan
from ops_suite.mail.state import SeenState
from ops_suite.tdx import api_source
from ops_suite.tdx.alerts import AlertState, counts_phrase
from ops_suite.tdx.api_client import ApiError, TdxApiClient
from ops_suite.tdx.api_primary import ApiFirst
from ops_suite.tdx.api_token import (
    ApiToken,
    TokenError,
    TokenSource,
    browser_fetcher,
    expiry_text,
    fetch_token,
    load_token,
    save_token,
)
from ops_suite.tdx.browser import PROFILE_IN_USE_MESSAGE, BrowserStartError, TdxBrowser
from ops_suite.tdx.capture import is_valid_capture_name, write_capture
from ops_suite.tdx.dashboard import (
    KINDS,
    DashboardRead,
    DashboardStructureError,
    NotSignedInError,
    WidgetNotFoundError,
    WidgetRead,
    check_dashboard,
    find_dashboard_frame,
    read_dashboard,
)
from ops_suite.tdx.demo.app import serve
from ops_suite.tdx.demo.dataset import demo_pages, demo_tickets
from ops_suite.tdx.demo.run import (
    DEMO_HEADER,
    DemoLlmClient,
    demo_card,
    demo_settings,
    demo_summary_request,
)
from ops_suite.tdx.event_form import PART_SOURCES, READ_FIELDS
from ops_suite.tdx.followup import (
    EVAL_SETS,
    FollowUpOutcome,
    format_eval_report,
    format_follow_up_detail,
    format_follow_up_line,
    load_eval_cases,
    off_outcome,
    run_eval,
)
from ops_suite.tdx.guard import BUILT_IN_ALLOWED, AllowedRequest, parse_allowed
from ops_suite.tdx.rules import AuditResult, format_audit_summary
from ops_suite.tdx.saved import forget_dashboard, save_dashboard
from ops_suite.tdx.session import describe_path, describe_url, on_tdx, tdx_origin
from ops_suite.tdx.session_log import (
    SessionLog,
    format_lifetime,
    record_expiry,
    record_sign_in,
    session_log_path,
)
from ops_suite.tdx.shadow import (
    KEEP_DAYS,
    BrowserShadow,
    ShadowLog,
    ShadowRunner,
    compare_report,
    shadow_log_path,
)
from ops_suite.tdx.tickets import LABEL_PLACES, TEMPLATE_ID, FeedRead
from ops_suite.tdx.watch import (
    DESK_NOT_APPROVED,
    AuditRun,
    SummaryRequest,
    alert_state_path,
    classify_step,
    desk_allowed,
    draft_card,
    draft_summary,
    failed_step,
    format_send_outcome,
    read_and_judge,
    rule_settings,
    run_audit,
    send_alerts,
    send_cycle,
    start_address,
    teams_label,
    watch,
    webhook_notifier,
)

log = logging.getLogger(__name__)

KIND_ORDER = ("reply", "private", "system")
SOURCES = ("browser", "api")

STATE_FILE = Path("state") / "seen.json"
REPORT_FILE = Path("reports") / "direct-requests.csv"

LOGIN_PROMPT = (
    "Sign in to TeamDynamix in the browser window (MFA included). "
    "When you see the TDX start page, press Enter here."
)
TDX_SETTLE_SECONDS = 20  # how long to wait for a page on TDX after Enter
TDX_SETTLE_POLL_MS = 500
WAITING_FOR_TDX = "Waiting for the TDX page to finish loading…"
CAPTURE_PROMPT = (
    "Sign in if asked, then open the page to capture (dashboard, report or ticket). "
    "Press Enter here when it has fully loaded."
)
SESSION_EXPIRED = f'The saved TDX session has expired. Run "{RUN} tdx login" and sign in again.'
REPORT_BLOCKED = (
    "Review this request before adding it to tdx.allowed_requests "
    '(docs/USAGE.md, "Read-only by design").'
)
SET_DASHBOARD_HINT = (
    f'Run "{RUN} tdx set-dashboard" once to choose the dashboard that shows both widgets.'
)
SET_DASHBOARD_RETRY = "Open a dashboard that shows both widgets and try again."
SET_DASHBOARD_NOT_STANDALONE = (
    "The dashboard does not open on its own outside the TDX page, so it was not saved. "
    'Choose another dashboard, or see docs/USAGE.md, "TDX setup", the "tdx set-dashboard" step.'
)
SET_DASHBOARD_OVERRIDDEN = (
    f'Note: tdx.dashboard_url is set, so "{RUN} tdx rows" starts there and ignores the '
    "address saved here until that setting is removed."
)
DASHBOARD_SOURCES = {
    "setting": "Dashboard: setting",
    "saved": "Dashboard: saved (tdx set-dashboard)",
    "start page": "Dashboard: start page",
}
NO_WEBHOOK = f'no Teams webhook is saved; run "{RUN} teams set-webhook" first'
TO_HELP = "personal (your own chat, the default) or desk (the desk's shared chat)"
NO_API_KEY = f'no Anthropic API key is saved; run "{RUN} llm set-key" first'
DEMO_NO_KEY = f'LLM: skipped (no API key saved; "{RUN} llm set-key" to see this step).'
# llm test: a fixed synthetic sentence and a tiny schema; too short to cache, which is fine.
LLM_TEST_SENTENCE = "Synthetic connection check: the test printer in room 000 works again."
LLM_TEST_SYSTEM = (
    "You check that an API connection works. The user message is a fixed synthetic test "
    'sentence. Reply with "ok" set to true.'
)
# llm eval: the synthetic eval sets, in the source checkout (tests/fixtures/llm).
EVAL_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm"
NO_LLM_HELP = "skip the LLM step for this run, even when llm.enabled is set"
# tdx agent: the checkout that `uv run --project` starts at login (src/ops_suite/cli.py -> repo).
REPO_DIR = Path(__file__).resolve().parents[2]
AGENT_NOT_HERE = (
    f'start at login is for macOS and Windows only; run "{RUN} tdx watch" in a terminal'
)
AGENT_NO_CHECKOUT = (
    f"start at login needs the source checkout (no pyproject.toml in {REPO_DIR}); run "
    f'"{RUN} tdx agent install" from the cloned repository'
)
AGENT_NO_UV = "uv was not found on PATH; install uv or open a new Terminal, then try again"
AGENT_LOADED_WARNING = (
    "The start-at-login loop pauses while this window is open and resumes by itself afterwards."
)
AGENT_REMINDER = (
    "The Mac must stay awake with the lid open. When TDX asks you to sign in, run "
    f'"{RUN} tdx login" in a Terminal; the agent\'s loop resumes by itself.'
)
AGENT_WINDOWS_START = (
    "It starts at your next sign-in to Windows. To start it now, open the Startup file "
    "(double-click it)."
)
AGENT_WINDOWS_REMINDER = (
    "Keep the PC on and plugged in; while the loop runs it asks Windows not to sleep. When TDX "
    f'asks you to sign in, run "{RUN} tdx login" in a terminal; the loop resumes by itself.'
)
AGENT_NO_LOG = (
    "The start-at-login loop has not written its log yet. Sign out and back in, or run the "
    "script once by hand: {script}."
)
STORE_PYTHON_REFUSED = (
    "This Python is the Microsoft Store build, which hides the data folder from other "
    'programs. Rebuild the environment: see USAGE, "Windows: use uv\'s Python".'
)
STORE_PYTHON_WARNING = (
    "this Python is the Microsoft Store build, which hides the data folder from other "
    'programs, so live tdx commands refuse to run. See USAGE, "Windows: use uv\'s Python".'
)
AGENT_WINDOWS_REMOVED = (
    "Removed the start-at-login script. A tdx watch it already started keeps running until "
    "you close its minimised window."
)
SOURCE_HELP = (
    "where the queue is read from: browser (the dashboard, the default) or api (the TDX Web "
    "API, read-only; needs tdx.api.enabled)"
)
API_SOURCE_LINE = "Source: the TDX Web API (read-only trial; the browser stays the default)."
API_FIRST_LINE = (
    "Source: the TDX Web API (tdx.source = api); the browser reads this run if the API cannot."
)
API_ONLY_LINE = "Source: the TDX Web API only (--source api: no browser fallback)."
API_DISABLED = (
    "tdx.api.enabled is false; set it to true in your own config to use the TDX Web API "
    '(docs/USAGE.md, "TDX API (proof of concept)")'
)
SHADOW_NEEDS_API = (
    'tdx.source = "shadow" reads the queue through the TDX Web API too, so it needs '
    'tdx.api.enabled = true and both report ids (docs/USAGE.md, "Shadow mode")'
)
API_SOURCE_NEEDS_API = (
    'tdx.source = "api" reads the queue through the TDX Web API, so it needs '
    'tdx.api.enabled = true and both report ids (docs/USAGE.md, "Reading through the API")'
)
API_NO_SEND = (
    "--send works with the browser source only: the TDX Web API is a read-only trial that "
    'posts no cards until tdx.source = "api"'
)
API_TOKEN_HINT = f'Run "{RUN} tdx api token --headed" (or "{RUN} tdx login") to get one.'
# The Web API's sign-in is its own, apart from the TDX session's.
API_SIGN_IN_LINE = (
    "The TDX API uses its own sign-in. If a sign-in page appears, sign in again (MFA included)."
)
NEEDS_SIGN_IN = f'TDX API token: needs sign-in; run "{RUN} tdx api token --headed".'
TOKEN_RETRY = f'Run "{RUN} tdx api token --headed" to try again.'
TOKEN_HINTS = {
    "idp_refused": (
        "The sign-in service showed an error page; try again once. If it keeps failing, see "
        'docs/USAGE.md, "TDX API (proof of concept)".'
    ),
}
API_HINTS = {
    "unauthorised": f"The API refused the token. {API_TOKEN_HINT}",
    "forbidden": "Your account may not read this; check the report ids and tdx.api.app_id.",
    "not_found": "Check the report ids and tdx.api.app_id.",
    "rate_limited": "TDX's rate limit was reached; try again in a minute.",
}
REVIEW_REMINDER = (
    "Review before sharing: open kept-text.txt (only column names should be there) and check "
    "that no\nframe-*.html contains a name, email address, ticket number or ticket title."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-suite", description="IT service desk operations.")
    commands = parser.add_subparsers(dest="command", required=True)

    mail = commands.add_parser("mail", help="mail scan commands")
    mail_commands = mail.add_subparsers(dest="mail_command", required=True)

    scan = mail_commands.add_parser(
        "scan", help="report mail that a person sent to the shared mailbox"
    )
    scan.add_argument("--profile", required=True, choices=["work"], help="mail profile to scan")
    scan.add_argument("--config", type=Path, metavar="PATH", help="TOML config file")
    scan.add_argument("--drop-dir", type=Path, metavar="PATH", help="overrides mail.work.drop_dir")
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="classify and count only; write, move and delete nothing",
    )
    scan.add_argument("--verbose", action="store_true", help="log debug detail")
    scan.set_defaults(handler=_mail_scan)

    tdx = commands.add_parser(
        "tdx",
        help="TeamDynamix sign-in, page-structure capture, dashboard rows and the queue audit",
    )
    tdx_commands = tdx.add_subparsers(dest="tdx_command", required=True)

    login = tdx_commands.add_parser(
        "login", help="sign in to TDX once in the tool's own browser profile (MFA included)"
    )
    _add_common_options(login)
    login.set_defaults(handler=_tdx_login, live=True)

    capture = tdx_commands.add_parser(
        "capture",
        help="save the structure of the open TDX pages, with all personal data removed",
    )
    capture.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="capture name, 1-40 characters of a-z, 0-9 and - (for example: dashboard)",
    )
    _add_common_options(capture)
    capture.set_defaults(handler=_tdx_capture, live=True)

    rows = tdx_commands.add_parser(
        "rows",
        help="count the tickets in the dashboard's Unclaimed and Claimed by Others widgets "
        "(read-only)",
    )
    rows.add_argument(
        "--headed", action="store_true", help="show the browser window (overrides tdx.headless)"
    )
    rows.add_argument("--source", choices=SOURCES, default="browser", help=SOURCE_HELP)
    _add_common_options(rows)
    rows.set_defaults(handler=_tdx_rows, live=True)

    set_dashboard = tdx_commands.add_parser(
        "set-dashboard",
        help="once per person: open the dashboard that shows both ticket widgets by hand, and "
        "save its address for 'tdx rows'",
    )
    _add_common_options(set_dashboard)
    set_dashboard.set_defaults(handler=_tdx_set_dashboard, live=True)

    audit = tdx_commands.add_parser(
        "audit",
        help="read the dashboard, open the claimed tickets that need it (read-only) and count "
        "the tickets the desk should look at",
    )
    audit.add_argument(
        "--headed", action="store_true", help="show the browser window (overrides tdx.headless)"
    )
    audit.add_argument(
        "--show-ids",
        action="store_true",
        help="also list each finding by ticket id and reason code, for your own check in TDX; "
        "do not paste these lines into a chat",
    )
    audit.add_argument(
        "--send",
        action="store_true",
        help="also post one Teams card with the findings that are new or due again",
    )
    _add_to_option(audit, f"where --send posts: {TO_HELP}; desk needs teams.desk.approved = true")
    audit.add_argument("--no-llm", action="store_true", help=NO_LLM_HELP)
    audit.add_argument(
        "--source",
        choices=SOURCES,
        default=None,
        help=f"{SOURCE_HELP}. Default: tdx.source (api reads through the API and lets the "
        "browser read the run if the API cannot; shadow reads through the browser). Unless "
        "tdx.source is api, --source api runs without --send and without the LLM step",
    )
    _add_common_options(audit)
    audit.set_defaults(handler=_tdx_audit, live=True)

    watch_parser = tdx_commands.add_parser(
        "watch",
        help="run the audit and send alerts every tdx.watch.interval_minutes during desk "
        "hours, in the foreground, until Ctrl+C",
    )
    watch_parser.add_argument(
        "--headed", action="store_true", help="show the browser window (overrides tdx.headless)"
    )
    _add_to_option(
        watch_parser,
        f"where cards post: {TO_HELP}; desk needs teams.desk.approved = true. Sign-in "
        "notices always go to your own chat",
    )
    watch_parser.add_argument("--no-llm", action="store_true", help=NO_LLM_HELP)
    _add_common_options(watch_parser)
    watch_parser.set_defaults(handler=_tdx_watch, live=True)

    agent = tdx_commands.add_parser(
        "agent",
        help="start tdx watch at login: a launchd user agent on macOS, a Startup-folder script "
        "on Windows",
    )
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_install = agent_commands.add_parser(
        "install",
        help="start tdx watch at every login (on macOS also now, kept awake with caffeinate -i)",
    )
    agent_install.add_argument(
        "--headed", action="store_true", help="show the browser window (overrides tdx.headless)"
    )
    _add_to_option(
        agent_install,
        f"where the agent's cards post: {TO_HELP}; desk needs teams.desk.approved = true",
    )
    _add_common_options(agent_install)
    agent_install.set_defaults(handler=_tdx_agent_install, live=True)
    agent_uninstall = agent_commands.add_parser(
        "uninstall", help="stop the agent's tdx watch and remove the agent"
    )
    _add_common_options(agent_uninstall)
    agent_uninstall.set_defaults(handler=_tdx_agent_uninstall)
    agent_status = agent_commands.add_parser(
        "status",
        help="show whether the agent is installed and running, its log files and the TDX "
        "session lifetime",
    )
    _add_common_options(agent_status)
    agent_status.set_defaults(handler=_tdx_agent_status)

    api = tdx_commands.add_parser(
        "api",
        help="the TDX Web API trial: the same queue read through the API, read-only (needs "
        "tdx.api.enabled)",
    )
    api_commands = api.add_subparsers(dest="api_command", required=True)
    api_token = api_commands.add_parser(
        "token",
        help="get a fresh API token headless with the saved TDX sign-in and keep it in the "
        "system keychain; prints its expiry, never the token",
    )
    api_token.add_argument(
        "--headed",
        action="store_true",
        help="open a browser window for the API's own sign-in (MFA included) and wait up to "
        "tdx.api.token_sign_in_minutes for you to finish it",
    )
    _add_common_options(api_token)
    api_token.set_defaults(handler=_tdx_api_token, live=True)
    api_check = api_commands.add_parser(
        "check",
        help="check the API read-only: your role, the app id, both reports and one feed "
        "(counts only)",
    )
    _add_common_options(api_check)
    api_check.set_defaults(handler=_tdx_api_check, live=True)
    api_reports = api_commands.add_parser(
        "reports",
        help="list the ids and names of the reports you can see, to pick "
        "tdx.api.unclaimed_report_id and claimed_report_id",
    )
    _add_common_options(api_reports)
    api_reports.set_defaults(handler=_tdx_api_reports, live=True)
    api_compare = api_commands.add_parser(
        "compare",
        help="compare the browser and the API from the shadow-mode records (tdx.source = "
        '"shadow"): counts, percentages and a verdict line',
    )
    api_compare.add_argument(
        "--days",
        type=_compare_days,
        default=14,
        help=f"how many days back to look, 1 to {KEEP_DAYS} (default 14)",
    )
    api_compare.add_argument(
        "--show-ids",
        action="store_true",
        help="also list the ticket ids behind each mismatch, for your own check in TDX; do not "
        "paste these lines into a chat",
    )
    _add_common_options(api_compare)
    api_compare.set_defaults(handler=_tdx_api_compare)

    session = tdx_commands.add_parser(
        "session",
        help="show how long the last TDX sign-in lasted (from tdx login to the first expiry seen)",
    )
    _add_common_options(session)
    session.set_defaults(handler=_tdx_session)

    demo = tdx_commands.add_parser(
        "demo",
        help="run the audit offline on synthetic TDX pages served on 127.0.0.1 (no accounts, "
        "no real data)",
    )
    demo.add_argument("--headed", action="store_true", help="show the browser window")
    demo.add_argument(
        "--send",
        action="store_true",
        help="also post the card, marked DEMO, to the saved Teams webhook",
    )
    _add_to_option(demo, f"where --send posts the synthetic card: {TO_HELP}")
    demo.add_argument(
        "--no-llm", action="store_true", help="skip the LLM step even when an API key is saved"
    )
    demo.add_argument(
        "--card-json",
        type=Path,
        metavar="PATH",
        help="write the demo's Adaptive Card JSON to PATH (for the Adaptive Cards Designer)",
    )
    demo.add_argument(
        "--summary",
        action="store_true",
        help="build the shift-summary card (events this week, every open finding) instead of "
        "the alert card",
    )
    demo.add_argument(
        "--summary-kind",
        choices=("full", "delta"),
        help="with --summary (implied): the day's first, full summary (the default), or a later "
        "slot's delta (new findings and due reminders, cleared ones as a count)",
    )
    _add_common_options(demo)
    demo.set_defaults(handler=_tdx_demo)

    teams = commands.add_parser("teams", help="the Teams Workflows webhook that alerts post to")
    teams_commands = teams.add_subparsers(dest="teams_command", required=True)
    set_webhook = teams_commands.add_parser(
        "set-webhook",
        help="save the Workflows webhook URL in the system keychain (asked for, never echoed)",
    )
    _add_to_option(set_webhook, f"which webhook to save: {TO_HELP}")
    _add_common_options(set_webhook)
    set_webhook.set_defaults(handler=_teams_set_webhook)
    teams_test = teams_commands.add_parser(
        "test", help="post a synthetic test card to the saved webhook"
    )
    _add_to_option(teams_test, f"where the synthetic card goes: {TO_HELP}")
    teams_test.add_argument(
        "--mention",
        action="store_true",
        help="add the shift summaries' mention (tdx.summary.mention), to check who is notified",
    )
    _add_common_options(teams_test)
    teams_test.set_defaults(handler=_teams_test)

    llm = commands.add_parser("llm", help="the Claude API step (off unless llm.enabled is set)")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    set_key = llm_commands.add_parser(
        "set-key",
        help="save the Anthropic API key in the system keychain (asked for, never echoed)",
    )
    _add_common_options(set_key)
    set_key.set_defaults(handler=_llm_set_key)
    llm_status = llm_commands.add_parser(
        "status", help="show the LLM settings and today's token use (no network call)"
    )
    _add_common_options(llm_status)
    llm_status.set_defaults(handler=_llm_status)
    llm_test = llm_commands.add_parser(
        "test", help="send one fixed synthetic sentence to check the key and the connection"
    )
    _add_common_options(llm_test)
    llm_test.set_defaults(handler=_llm_test)
    llm_eval = llm_commands.add_parser(
        "eval",
        help="run the follow-up classifier on a synthetic eval set and print counts only",
    )
    llm_eval.add_argument(
        "--set", dest="eval_set", choices=EVAL_SETS, default="dev", help="which set (dev)"
    )
    _add_common_options(llm_eval)
    llm_eval.set_defaults(handler=_llm_eval)
    return parser


def _add_to_option(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument(
        "--to", choices=DESTINATIONS, default="personal", metavar="CHAT", help=help_text
    )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, metavar="PATH", help="TOML config file")
    parser.add_argument("--verbose", action="store_true", help="log debug detail")


def format_summary(result: ScanResult) -> str:
    lines = [
        f"Scanned {result.scanned}, flagged {result.flagged}, "
        f"skipped {sum(result.skipped.values())}, duplicates {result.duplicates}, "
        f"rejected {result.rejected}"
    ]
    if result.skipped:
        reasons = ", ".join(f"{reason} {count}" for reason, count in sorted(result.skipped.items()))
        lines.append(f"  skipped: {reasons}")
    if result.report_path is None:
        lines.append("Report: (dry run, nothing written)")
        # Received time and message id only; sender and subject stay out of the terminal.
        for received_at, message_id in result.dry_run_flagged:
            lines.append(f"  would report: {received_at:%Y-%m-%dT%H:%M:%SZ}  {message_id}")
    else:
        lines.append(f"Report: {result.report_path}")
    return "\n".join(lines)


def _config_problem(explanation: str) -> int:
    print(f"ops-suite: {explanation}", file=sys.stderr)
    return EXIT_CONFIG


def _load_settings(config_path: Path | None) -> Settings:
    """Settings from every source; ConfigProblem with a one-line explanation if invalid."""
    try:
        return load_settings(config_path)
    except tomllib.TOMLDecodeError as error:
        raise ConfigProblem(f"the config file is not valid TOML ({error})") from error
    except ValidationError as error:
        raise ConfigProblem(f"invalid settings ({_settings_problems(error)})") from error


def _settings_problems(error: ValidationError) -> str:
    """One line per failed check, joined. A section that refuses unknown keys names the key, so
    a retired or misspelt setting from the file, the environment or .env is easy to find."""
    problems = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        if item["type"] == "extra_forbidden":
            problems.append(f"unknown setting {where}; see config.example.toml for the valid keys")
        elif where:
            problems.append(f"{where}: {item['msg']}")
        else:
            # A whole-settings check (a summary time outside desk hours) has no location.
            problems.append(item["msg"])
    return "; ".join(problems)


def _mail_scan(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    work = settings.mail.work
    drop_dir = args.drop_dir or work.drop_dir
    if drop_dir is None:
        return _config_problem("no drop folder set; set mail.work.drop_dir or pass --drop-dir")
    if not drop_dir.is_dir():
        return _config_problem(f"the drop folder does not exist: {drop_dir}")
    if not work.mailbox_address.strip():
        return _config_problem("mail.work.mailbox_address is empty; set the shared mailbox address")

    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    profile_dir = paths.profile_dir(settings, args.profile)
    result = run_scan(
        DropFolderMailSource(drop_dir, retention_days=work.retention_days, read_only=args.dry_run),
        MailRules.from_settings(work),
        SeenState(profile_dir / STATE_FILE),
        ReportWriter(profile_dir / REPORT_FILE),
        dry_run=args.dry_run,
    )
    print(format_summary(result))
    return EXIT_OK


def _tdx_settings(config_path: Path | None) -> tuple[Settings, str]:
    settings = _load_settings(config_path)
    base_url = settings.tdx.base_url.strip()
    if not base_url:
        raise ConfigProblem("tdx.base_url is empty; set it to the TDX web app's start page")
    try:
        tdx_origin(base_url)
    except ValueError as error:
        raise ConfigProblem("tdx.base_url must be an http:// or https:// URL") from error
    return settings, base_url


def _wait_for_tdx(browser: TdxBrowser, base_url: str, prompt: str) -> int:
    """Open the start page, wait for Enter, then give the browser up to TDX_SETTLE_SECONDS
    to reach TDX. Returns how many open pages are on TDX.

    The identity provider's sign-in ends with a form post and redirects back to TDX, which
    can still be running when the person presses Enter on what looks like a finished page.
    The log record tells a slow redirect (several polls) from a page whose reported address
    was only lagging (one poll).
    """
    browser.open_start_page()
    print(prompt, flush=True)
    try:
        input()
    except EOFError:
        pass  # no terminal input; check what is open now
    at_enter = browser.current_urls()
    urls = at_enter
    polls = 0
    if urls and not any(on_tdx(url, base_url) for url in urls):
        print(WAITING_FOR_TDX, flush=True)
        # Counting polls rather than reading a clock keeps the limit exact in tests.
        while polls < TDX_SETTLE_SECONDS * 1000 // TDX_SETTLE_POLL_MS:
            browser.wait(TDX_SETTLE_POLL_MS)
            polls += 1
            urls = browser.current_urls()
            if not urls or any(on_tdx(url, base_url) for url in urls):
                break  # on TDX, or the person closed the window
    log.info(
        "pages open",
        extra={
            "pages_at_enter": [describe_url(url) for url in at_enter],
            "pages": [describe_url(url) for url in urls],
            "waited_ms": polls * TDX_SETTLE_POLL_MS,
        },
    )
    return sum(on_tdx(url, base_url) for url in urls)


def _still_on(urls: Sequence[str]) -> str | None:
    """The host of the last open web page, or None. The host only: a sign-in page's path and
    query can carry a request token."""
    for url in reversed(urls):
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            continue
        if parts.scheme.lower() in ("http", "https") and parts.hostname:
            return parts.hostname
    return None


def _not_signed_in(command: str, base_url: str, urls: Sequence[str] = ()) -> int:
    origin = tdx_origin(base_url)
    host = _still_on(urls)
    where = f"no open page is on {origin}"
    if host is not None and host != urlsplit(origin).hostname:
        where += f"; the browser is still on {host}"
    print(
        f"ops-suite: sign-in is not finished ({where}). "
        f'Run "{RUN} tdx {command}" again and finish signing in first. If the sign-in page '
        "refused the automated browser, stop here: this tool does not work around that.",
        file=sys.stderr,
    )
    return EXIT_NOT_SIGNED_IN


def _browser_failed(error: BrowserStartError) -> int:
    if str(error) == PROFILE_IN_USE_MESSAGE:
        print(f"ops-suite: the browser could not start: {error}.", file=sys.stderr)
        return EXIT_BROWSER
    print(
        f"ops-suite: the browser could not start ({error}). If Chromium is not installed, run: "
        "uv run playwright install chromium",
        file=sys.stderr,
    )
    return EXIT_BROWSER


def _finished(command: str, code: int, **counts: int) -> int:
    log.info("command finished", extra={"command": command, "exit_code": code, **counts})
    return code


def _tdx_login(args: argparse.Namespace) -> int:
    settings, base_url = _tdx_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    profile = paths.tdx_profile_dir(settings)
    log.info("tdx login started", extra={"url": describe_url(base_url)})
    agent = _autostart(settings)
    try:
        if agent is not None and agent.status().loaded:
            print(AGENT_LOADED_WARNING)
    except StartupFolderError:
        pass  # only the warning is lost
    try:
        with TdxBrowser(profile, base_url) as browser:
            if not _wait_for_tdx(browser, base_url, LOGIN_PROMPT):
                return _finished(
                    "tdx login", _not_signed_in("login", base_url, browser.current_urls())
                )
            record_sign_in(settings, datetime.now(UTC))
            print(f"Signed in. The session is saved in {profile}.")
            if settings.tdx.api.enabled:
                tdx = settings.tdx.model_copy(update={"base_url": base_url})
                print(_login_token_line(browser, tdx), flush=True)
    except BrowserStartError as error:
        return _finished("tdx login", _browser_failed(error))
    return _finished("tdx login", EXIT_OK)


def _tdx_capture(args: argparse.Namespace) -> int:
    if not is_valid_capture_name(args.name):
        raise ConfigProblem("--name must be 1-40 characters of a-z, 0-9 and - (e.g. dashboard)")
    settings, base_url = _tdx_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(
        "tdx capture started", extra={"url": describe_url(base_url), "capture_name": args.name}
    )
    try:
        with TdxBrowser(paths.tdx_profile_dir(settings), base_url) as browser:
            if not _wait_for_tdx(browser, base_url, CAPTURE_PROMPT):
                return _finished(
                    "tdx capture", _not_signed_in("capture", base_url, browser.current_urls())
                )
            pages = len(browser.current_urls())
            frames = browser.collect_frames()
            requests = browser.requests_seen()
            skipped = browser.skipped_frames
            folder = write_capture(
                paths.tdx_captures_dir(settings), args.name, frames, requests, datetime.now()
            )
    except BrowserStartError as error:
        return _finished("tdx capture", _browser_failed(error))
    print(
        f"Captured {len(frames)} frame(s) from {pages} page(s), {skipped} skipped, "
        f"{len(requests)} request(s)."
    )
    print(f"Saved to {folder}")
    print(REVIEW_REMINDER)
    return _finished(
        "tdx capture",
        EXIT_OK,
        frames=len(frames),
        pages=pages,
        skipped=skipped,
        requests=len(requests),
    )


def _counted(items: tuple[str, ...]) -> list[str]:
    """``item (n)`` for each distinct item, in the order first seen."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return [f"{item} ({count})" for item, count in counts.items()]


def _limit_line(kinds: list[str], page_limit: int, row_limit: bool) -> str:
    if row_limit:
        return f"Stopped at the report's row limit (MaxResults) for: {', '.join(kinds)}"
    return f"Stopped at the page limit ({page_limit}) for: {', '.join(kinds)}"


def format_rows_summary(
    read: DashboardRead, page_limit: int, *, verbose: bool = False, row_limit: bool = False
) -> str:
    """Counts, date shapes and masked request paths only; never ticket text."""
    unclaimed = read.widgets["unclaimed"]
    claimed = read.widgets["claimed"]
    widgets = (unclaimed, claimed)
    unreadable = tuple(shape for widget in widgets for shape in widget.unreadable_dates)
    duplicates = sum(widget.duplicates for widget in widgets)
    without_id = sum(widget.rows_without_id for widget in widgets)
    lines = [
        f"Unclaimed: {len(unclaimed.rows)} ticket(s) on {unclaimed.pages} page(s).",
        f"Claimed by others: {len(claimed.rows)} ticket(s) on {claimed.pages} page(s).",
        f"Unreadable dates: {len(unreadable)}. Duplicates: {duplicates}. "
        f"Blocked requests: {len(read.blocked)}. Allowed non-GET: {len(read.allowed_non_get)}.",
    ]
    if without_id:
        lines.append(f"Rows without a ticket id (skipped): {without_id}.")
    if unreadable:
        lines.append(f"Unreadable date shapes: {'; '.join(_counted(unreadable))}")
    lines.extend(f"Blocked: {line}" for line in _counted(read.blocked))
    if verbose:
        lines.extend(f"Allowed non-GET: {line}" for line in _counted(read.allowed_non_get))
    limited = [widget.kind for widget in widgets if widget.hit_page_limit]
    if limited:
        lines.append(_limit_line(limited, page_limit, row_limit))
    return "\n".join(lines)


def _structure_failed(error: DashboardStructureError, what: str = "the dashboard") -> int:
    lines = [f"ops-suite: {what} did not have the expected structure: {error}."]
    if error.blocked:
        lines.extend(f"Blocked: {line}" for line in _counted(error.blocked))
        lines.append(REPORT_BLOCKED)
    print("\n".join(lines), file=sys.stderr)
    return EXIT_STRUCTURE


def _guarded_settings(
    config_path: Path | None,
) -> tuple[Settings, TdxSettings, tuple[AllowedRequest, ...]]:
    """Settings for the guarded commands; ConfigProblem if any TDX setting is unusable."""
    settings, base_url = _tdx_settings(config_path)
    tdx = settings.tdx.model_copy(update={"base_url": base_url})
    dashboard_url = tdx.dashboard_url.strip()
    if dashboard_url and not on_tdx(dashboard_url, base_url):
        raise ConfigProblem("tdx.dashboard_url must be on the same host as tdx.base_url")
    return settings, tdx, BUILT_IN_ALLOWED + parse_allowed(tdx.allowed_requests)


def _tdx_rows(args: argparse.Namespace) -> int:
    if args.source == "api":
        return _tdx_rows_api(args)
    settings, tdx, allowed = _guarded_settings(args.config)
    source, start_url = start_address(settings, tdx)
    headless = tdx.headless and not args.headed
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(
        "tdx rows started",
        extra={
            "dashboard_source": source,
            "path": describe_path(start_url),
            "headless": headless,
            "allowed_requests": [f"{entry.method} {entry.path_pattern}" for entry in allowed],
        },
    )
    print(DASHBOARD_SOURCES[source], flush=True)
    now = datetime.now(ZoneInfo(tdx.display_timezone))
    try:
        with TdxBrowser(
            paths.tdx_profile_dir(settings),
            tdx.base_url,
            headless=headless,
            allowed_requests=allowed,
        ) as browser:
            read = read_dashboard(browser, tdx, now, start_url=start_url)
    except BrowserStartError as error:
        return _finished("tdx rows", _browser_failed(error))
    except NotSignedInError:
        record_expiry(settings, datetime.now(UTC))
        print(f"ops-suite: {SESSION_EXPIRED}", file=sys.stderr)
        return _finished("tdx rows", EXIT_NOT_SIGNED_IN)
    except DashboardStructureError as error:
        log.info("dashboard structure problem", extra={"blocked": list(error.blocked)})
        code = _structure_failed(error)
        if source == "start page" and isinstance(error, WidgetNotFoundError):
            print(SET_DASHBOARD_HINT, file=sys.stderr)
        return _finished("tdx rows", code)

    print(format_rows_summary(read, tdx.max_pages_per_widget, verbose=args.verbose))
    widgets = read.widgets.values()
    log.info(
        "tdx rows summary",
        extra={
            "unreadable_date_shapes": [s for w in widgets for s in w.unreadable_dates],
            "blocked": list(read.blocked),
            "allowed_non_get": list(read.allowed_non_get),
            "hit_page_limit": [w.kind for w in widgets if w.hit_page_limit],
        },
    )
    return _finished(
        "tdx rows",
        EXIT_OK,
        unclaimed=len(read.widgets["unclaimed"].rows),
        unclaimed_pages=read.widgets["unclaimed"].pages,
        claimed=len(read.widgets["claimed"].rows),
        claimed_pages=read.widgets["claimed"].pages,
        duplicates=sum(w.duplicates for w in widgets),
        unreadable_dates=sum(len(w.unreadable_dates) for w in widgets),
        blocked=len(read.blocked),
        allowed_non_get=len(read.allowed_non_get),
    )


def _set_dashboard_prompt(tdx: TdxSettings) -> str:
    return (
        "Sign in if asked, then open the dashboard that shows both "
        f'"{tdx.widgets.unclaimed_title}" and "{tdx.widgets.claimed_title}". '
        "Press Enter here when both have loaded."
    )


def _tdx_set_dashboard(args: argparse.Namespace) -> int:
    command = "tdx set-dashboard"
    settings, tdx, allowed = _guarded_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    profile = paths.tdx_profile_dir(settings)
    saved_file = paths.tdx_dashboard_file(settings)
    log.info(f"{command} started", extra={"url": describe_url(tdx.base_url)})
    if tdx.dashboard_url.strip():
        print(SET_DASHBOARD_OVERRIDDEN)

    # Steps 1-3: the person drives a headed, unguarded window, as in tdx capture.
    try:
        with TdxBrowser(profile, tdx.base_url) as browser:
            if not _wait_for_tdx(browser, tdx.base_url, _set_dashboard_prompt(tdx)):
                return _finished(
                    command,
                    _not_signed_in("set-dashboard", tdx.base_url, browser.current_urls()),
                )
            try:
                url = find_dashboard_frame(browser, tdx)
            except DashboardStructureError as error:
                print(f"ops-suite: {error}.\n{SET_DASHBOARD_RETRY}", file=sys.stderr)
                return _finished(command, EXIT_STRUCTURE)
    except BrowserStartError as error:
        return _finished(command, _browser_failed(error))
    if not on_tdx(url, tdx.base_url):
        print(
            f"ops-suite: the frame that shows both widgets is not on {tdx_origin(tdx.base_url)}, "
            "so it was not saved.",
            file=sys.stderr,
        )
        return _finished(command, EXIT_STRUCTURE)
    save_dashboard(saved_file, url, datetime.now(UTC))
    log.info("dashboard frame saved", extra={"path": describe_path(url)})

    # Step 4: does the address work on its own, in a guarded browser like tdx rows?
    checked = False
    try:
        with TdxBrowser(
            profile, tdx.base_url, headless=tdx.headless, allowed_requests=allowed
        ) as b:
            check_dashboard(b, tdx, url)
        checked = True
    except BrowserStartError as error:
        return _finished(command, _browser_failed(error))
    except (NotSignedInError, DashboardStructureError) as error:
        reason = (
            "opening the address on its own led to the sign-in page"
            if isinstance(error, NotSignedInError)
            else str(error)
        )
        lines = [SET_DASHBOARD_NOT_STANDALONE, f"Reason: {reason}."]
        blocked = getattr(error, "blocked", ())
        lines.extend(f"Blocked: {line}" for line in _counted(blocked))
        print("\n".join(lines), file=sys.stderr)
        return _finished(command, EXIT_STRUCTURE)
    finally:
        if not checked:
            forget_dashboard(saved_file)
    print(
        "Saved. Both widgets load from the saved dashboard address "
        f'({describe_path(url)}). "{RUN} tdx rows" will start there.'
    )
    return _finished(command, EXIT_OK)


def format_audit_output(
    dashboard: DashboardRead,
    feeds: FeedRead,
    result: AuditResult,
    page_limit: int,
    *,
    show_ids: bool = False,
    verbose: bool = False,
    desk_remembered: int | None = None,
    llm: FollowUpOutcome | None = None,
    llm_line: str | None = None,
    row_limit: bool = False,
) -> str:
    """The tdx audit summary: counts, reason codes, shapes and masked request paths.

    ``llm_line``, the follow-up classifier's ``LLM:`` line, goes right after ``Findings:``;
    ``llm`` adds its detail line with ``verbose``. Ticket ids appear only with ``show_ids``;
    names, titles and feed text never.
    """
    unclaimed = dashboard.widgets["unclaimed"]
    claimed = dashboard.widgets["claimed"]
    widgets = (unclaimed, claimed)
    unreadable = tuple(shape for widget in widgets for shape in widget.unreadable_dates)
    duplicates = sum(widget.duplicates for widget in widgets)
    without_id = sum(widget.rows_without_id for widget in widgets)
    blocked = dashboard.blocked + feeds.blocked
    allowed = dashboard.allowed_non_get + feeds.allowed_non_get
    entries = [entry for feed in feeds.feeds.values() for entry in feed.entries]
    kinds = ", ".join(
        f"{sum(entry.kind == kind for entry in entries)} {kind}" for kind in KIND_ORDER
    )
    entries_unreadable = sum(feed.unreadable for feed in feeds.feeds.values())
    needed = len(feeds.feeds) + len(feeds.failed) + len(feeds.deferred)
    summary = format_audit_summary(result, show_ids=show_ids)
    if llm_line is not None:
        summary.insert(1, llm_line)
    lines = [
        f"Unclaimed: {len(unclaimed.rows)} ticket(s) on {unclaimed.pages} page(s).",
        f"Claimed by others: {len(claimed.rows)} ticket(s) on {claimed.pages} page(s).",
        f"Feeds: {needed} needed, {len(feeds.feeds)} read, {len(feeds.failed)} failed, "
        f"{len(feeds.deferred)} deferred. Entries: {len(entries)} ({kinds}), "
        f"{entries_unreadable} unreadable.",
        *summary,
    ]
    if show_ids:
        lines.extend(f"  feed_failed  {ticket_id}" for ticket_id in feeds.failed)
        lines.extend(f"  feed_deferred  {ticket_id}" for ticket_id in feeds.deferred)
    lines.append(
        f"Unreadable dates: {len(unreadable)}. Duplicates: {duplicates}. "
        f"Blocked requests: {len(blocked)}. "
        f"Live updates blocked: {len(feeds.live_updates_blocked)}. "
        f"Allowed non-GET: {len(allowed)}."
    )
    if without_id:
        lines.append(f"Rows without a ticket id (skipped): {without_id}.")
    if unreadable:
        lines.append(f"Unreadable date shapes: {'; '.join(_counted(unreadable))}")
    lines.extend(f"Blocked: {line}" for line in _counted(blocked))
    if verbose:
        if feeds.unreadable_shapes:
            lines.append(f"Unreadable entries: {'; '.join(_counted(feeds.unreadable_shapes))}")
        lines.append(format_marker_line(feeds.marker_stats))
        if feeds.description_shapes.get("read") or feeds.description_shapes.get("forms"):
            lines.extend(format_description_lines(feeds.description_shapes))
        lines.extend(
            f"Live update blocked: {line}" for line in _counted(feeds.live_updates_blocked)
        )
        lines.extend(f"Allowed non-GET: {line}" for line in _counted(allowed))
        if desk_remembered is not None:
            lines.append(f"Desk members remembered: {desk_remembered}.")
        if llm is not None and llm.state == "ran":
            lines.append(format_follow_up_detail(llm))
    limited = [widget.kind for widget in widgets if widget.hit_page_limit]
    if limited:
        lines.append(_limit_line(limited, page_limit, row_limit))
    return "\n".join(lines)


def format_marker_line(stats: Mapping[str, int]) -> str:
    """The private-marker diagnostic for --verbose: counts only."""
    return (
        f"Entry markers: {stats['entries']} entries, {stats['nested']} nested; private marker "
        f"present on {stats['private_present']}, visible on {stats['private_visible']}; "
        f"inherited-private {stats['inherited_private']}; merge notes {stats['merge_notes']}; "
        f"reassignments {stats['reassignments']}; "
        f"signature desk replies {stats['signature_desk_replies']}; "
        f"visibility fallback {stats['visibility_fallback']}."
    )


def format_description_lines(shapes: Mapping[str, int]) -> list[str]:
    """The event form descriptions for --verbose: counts and place names only.

    How the page shows them (hidden text or a toggle: the page collapses long descriptions),
    in which places the space and services labels appear (``shown`` is the rendered text,
    ``anywhere`` the frame's whole HTML), and what the reader made of them: the "Response
    Summary" blocks found, the forms whose block held all the read answers' labels, and
    where the block used came from."""
    places = ", ".join(
        f"{place} {shapes[f'space_{place}']}/{shapes[f'services_{place}']}"
        for place in LABEL_PLACES
    )
    sources = ", ".join(f"{source} {shapes[f'from_{source}']}" for source in PART_SOURCES)
    return [
        f"Event form descriptions: {shapes['read']} read; with hidden text {shapes['hidden']}, "
        f"clipped {shapes['clipped']}, with a toggle {shapes['toggle']}.",
        f"Event form labels (space/services) found in: {places}.",
        f"Event form blocks: {shapes['forms']} forms, {shapes['blocks']} Response Summary "
        f"blocks, {shapes['complete']} with all {len(READ_FIELDS)} answers; used from "
        f"{sources}.",
    ]


class FeedProgress:
    """``Reading feeds: 7/16`` on stderr: rewritten in place on a terminal, else every 5.

    With ``verbose`` it prints every 5 even on a terminal, because console log records also
    go to stderr and would break an in-place line. Counts only; never a ticket id or title.
    """

    EVERY = 5

    def __init__(self, stream: TextIO, *, verbose: bool = False) -> None:
        self.stream = stream
        self.in_place = stream.isatty() and not verbose
        self.shown = False

    def __call__(self, done: int, total: int) -> None:
        text = f"Reading feeds: {done}/{total}"
        if self.in_place:
            self.stream.write(f"\r{text}")
            if done == total:
                self.stream.write("\n")
        elif done % self.EVERY == 0 or done == total:
            self.stream.write(f"{text}\n")
        else:
            return
        self.stream.flush()
        self.shown = True


def _check_ticket_template(tdx: TdxSettings) -> None:
    template = tdx.ticket_url_template.strip()
    if not template:
        return
    if TEMPLATE_ID not in template:
        raise ConfigProblem('tdx.ticket_url_template must contain "{id}" for the ticket id')
    if not on_tdx(template.replace(TEMPLATE_ID, "1"), tdx.base_url):
        raise ConfigProblem("tdx.ticket_url_template must be on the same host as tdx.base_url")


def _tdx_audit(args: argparse.Namespace) -> int:
    command = "tdx audit"
    settings, tdx, allowed = _guarded_settings(args.config)
    api_primary = tdx.source == "api"
    api_first = args.source is None and api_primary  # the API, the browser as fallback
    api = args.source == "api"  # the API only
    trial = api and not api_primary  # the read-only trial: no cards, no LLM step
    if trial and args.send:
        return _finished(command, _config_problem(API_NO_SEND))
    if api and not tdx.api.enabled:
        raise ConfigProblem(API_DISABLED)
    if api_first:
        _check_api_source(tdx)
    if args.send and not desk_allowed(settings, args.to):
        return _finished(command, _config_problem(DESK_NOT_APPROVED))
    _check_ticket_template(tdx)
    source, start_url = ("api", tdx.base_url) if api else start_address(settings, tdx)
    headless = tdx.headless and not args.headed
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(
        f"{command} started",
        extra={
            "dashboard_source": source,
            "path": describe_path(start_url),
            "headless": headless,
            "rules": repr(rule_settings(tdx)),
            "max_feeds_per_run": tdx.max_feeds_per_run,
            "allowed_requests": [f"{entry.method} {entry.path_pattern}" for entry in allowed],
            "send": args.send,
            "to": args.to,
            "source": "api_first" if api_first else (args.source or "browser"),
        },
    )
    if api_first:
        print(API_FIRST_LINE, flush=True)
    elif api:
        print(API_SOURCE_LINE if trial else API_ONLY_LINE, flush=True)
    else:
        print(DASHBOARD_SOURCES[source], flush=True)
    settings = settings.model_copy(update={"tdx": tdx})
    progress = FeedProgress(sys.stderr, verbose=args.verbose)
    use_llm = not (args.no_llm or trial)  # no LLM step on the API trial
    try:
        if api_first:
            run = ApiFirst(settings, headless=headless, use_llm=use_llm)(progress)
            if run.fallback is not None:
                print(
                    "Read through the browser instead: the TDX Web API is unavailable "
                    f"({run.fallback}).",
                    flush=True,
                )
        else:
            run = run_audit(
                settings,
                headless=headless,
                progress=progress,
                use_llm=use_llm,
                source="api" if api else "browser",
            )
    except (ApiError, TokenError) as error:
        return _finished(command, _api_problem(error))
    except keyring.errors.KeyringError as error:
        return _finished(command, _keychain_problem(error))
    except BrowserStartError as error:
        return _finished(command, _browser_failed(error))
    except NotSignedInError:
        record_expiry(settings, datetime.now(UTC))
        print(f"ops-suite: {SESSION_EXPIRED}", file=sys.stderr)
        return _finished(command, EXIT_NOT_SIGNED_IN)
    except DashboardStructureError as error:
        step = failed_step(error)
        log.info("structure problem", extra={"step": step, "blocked": list(error.blocked)})
        code = _structure_failed(error, step)
        if source == "start page" and isinstance(error, WidgetNotFoundError):
            print(SET_DASHBOARD_HINT, file=sys.stderr)
        return _finished(command, code)

    dashboard, feeds, result = run.dashboard, run.feeds, run.result
    print(
        format_audit_output(
            dashboard,
            feeds,
            result,
            tdx.max_pages_per_widget,
            show_ids=args.show_ids,
            verbose=args.verbose,
            desk_remembered=run.desk_remembered,
            llm=run.llm,
            llm_line=_llm_line(run, settings),
            row_limit=run.source == "api",
        )
    )
    log.info(
        f"{command} summary",
        extra={
            "counts": dict(result.counts),
            "desk_remembered": run.desk_remembered,
            "llm": dict(run.llm.counts) if run.llm is not None else None,
            "feeds_failed": list(feeds.failed),
            "feeds_deferred": list(feeds.deferred),
            "unreadable_entry_shapes": list(feeds.unreadable_shapes),
            "blocked": list(dashboard.blocked + feeds.blocked),
            "live_updates_blocked": len(feeds.live_updates_blocked),
            "marker_stats": dict(feeds.marker_stats),
            "allowed_non_get": list(dashboard.allowed_non_get + feeds.allowed_non_get),
        },
    )
    if args.send:
        outcome = send_alerts(
            run, settings, alert_state_path(settings, args.to), _stored_webhook(args.to), args.to
        )
        stream = sys.stderr if outcome.status == "no_webhook" else sys.stdout
        print(format_send_outcome(outcome), file=stream)
    return _finished(
        command,
        EXIT_OK,
        findings=len(result.findings),
        feeds_read=len(feeds.feeds),
        feeds_failed=len(feeds.failed),
        feeds_deferred=len(feeds.deferred),
        blocked=len(dashboard.blocked) + len(feeds.blocked),
        live_updates_blocked=len(feeds.live_updates_blocked),
    )


# --- the TDX Web API trial -----------------------------------------------------------------

REPORT_LABELS = {"unclaimed": "Unclaimed report", "claimed": "Claimed report"}
APP_ID_SOURCES = {
    "setting": "tdx.api.app_id",
    "ticket_url_template": "from tdx.ticket_url_template",
    "first ticket": "from the first ticket's record; set tdx.api.app_id to save that request",
}


def _keychain_problem(error: Exception) -> int:
    return _config_problem(f"the system keychain could not be used ({type(error).__name__})")


def _token_line(token: ApiToken, tdx: TdxSettings) -> str:
    """The token's expiry for the person; never the token."""
    when = expiry_text(token, ZoneInfo(tdx.display_timezone))
    return f"TDX API token: saved, expires {when}."


def _token_failed_line(reason: str) -> str:
    return f"TDX API token: not saved ({reason}). {TOKEN_HINTS.get(reason, TOKEN_RETRY)}"


def _headed_token(browser: TdxBrowser, tdx: TdxSettings) -> ApiToken:
    """The API's own sign-in in ``browser``'s window: the person signs in, this waits."""
    print(API_SIGN_IN_LINE, flush=True)
    return fetch_token(browser, tdx, interactive=True)


def _login_token_line(browser: TdxBrowser, tdx: TdxSettings) -> str:
    """After a sign-in: ask for the API token in the same window and keep it. The TDX
    sign-in has worked either way, so a failure is a line here, not an error."""
    try:
        token = _headed_token(browser, tdx)
        save_token(token)
    except TokenError as error:
        reason = error.reason
    except keyring.errors.KeyringError as error:
        reason = f"keychain ({type(error).__name__})"
    else:
        return _token_line(token, tdx)
    log.info("api token not saved", extra={"reason": reason})
    return _token_failed_line(reason)


def _api_problem(error: ApiError | TokenError) -> int:
    """An API or token failure: reason codes, endpoint patterns and masked paths only."""
    if isinstance(error, TokenError) and error.reason == "needs_sign_in":
        # The API's own sign-in, not the TDX session: no session log, no "expired" message.
        log.info("api problem", extra={"reason": error.reason})
        print(NEEDS_SIGN_IN, file=sys.stderr)
        return EXIT_NOT_SIGNED_IN
    lines = [f"ops-suite: {error}."]
    if isinstance(error, TokenError):
        lines.extend(f"Blocked: {line}" for line in _counted(error.blocked))
        if error.blocked:
            lines.append(REPORT_BLOCKED)
        elif error.reason == "no_token":
            lines.append(API_TOKEN_HINT)
        elif error.reason in TOKEN_HINTS:
            lines.append(TOKEN_HINTS[error.reason])
    elif error.reason in API_HINTS:
        lines.append(API_HINTS[error.reason])
    log.info(
        "api problem",
        extra={"reason": error.reason, "endpoint": getattr(error, "endpoint", "")},
    )
    print("\n".join(lines), file=sys.stderr)
    return EXIT_API


def _api_settings(config_path: Path | None) -> Settings:
    """Settings for the API commands; ConfigProblem unless tdx.api.enabled is set and the TDX
    addresses are usable."""
    settings, tdx, _ = _guarded_settings(config_path)
    if not tdx.api.enabled:
        raise ConfigProblem(API_DISABLED)
    _check_ticket_template(tdx)
    return settings.model_copy(update={"tdx": tdx})


def _api_run(command: str, body: Callable[[], int]) -> int:
    """``body``, with the API's, the token's and the browser's failures as exit codes. A token
    that needs a sign-in is the API's own sign-in, never an expired TDX session: nothing here
    writes the session log."""
    try:
        return body()
    except (ApiError, TokenError) as error:
        return _finished(command, _api_problem(error))
    except keyring.errors.KeyringError as error:
        return _finished(command, _keychain_problem(error))
    except BrowserStartError as error:
        return _finished(command, _browser_failed(error))
    except DashboardStructureError as error:
        return _finished(command, _structure_failed(error, "the report"))


def _api_client(settings: Settings) -> tuple[TdxApiClient, TokenSource]:
    """A client whose token is refreshed headless, with the saved sign-in, when needed."""
    tokens = TokenSource(browser_fetcher(settings))
    return TdxApiClient(settings.tdx, tokens), tokens


def _requests_line(client: TdxApiClient) -> str:
    total = sum(client.requests.values())
    parts = ", ".join(f"{name} {count}" for name, count in client.requests.items())
    return f"API requests: {total} ({parts or 'none'})."


def _tdx_api_token(args: argparse.Namespace) -> int:
    command = "tdx api token"
    settings = _api_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(f"{command} started", extra={"headed": args.headed})

    def body() -> int:
        if args.headed:
            # Unguarded, as in tdx login: the person signs in, and the identity provider's
            # pages post forms (to itself, then back to TDX) that the read-only guard refuses.
            with TdxBrowser(paths.tdx_profile_dir(settings), settings.tdx.base_url) as browser:
                try:
                    token = _headed_token(browser, settings.tdx)
                except TokenError as error:
                    log.info("api token not saved", extra={"reason": error.reason})
                    print(_token_failed_line(error.reason), file=sys.stderr)
                    return _finished(command, EXIT_API)
        else:
            token = browser_fetcher(settings)()
        save_token(token)
        print(_token_line(token, settings.tdx))
        return _finished(command, EXIT_OK)

    return _api_run(command, body)


def _tdx_rows_api(args: argparse.Namespace) -> int:
    command = "tdx rows"
    settings = _api_settings(args.config)
    tdx = settings.tdx
    api_source.report_ids(tdx)  # ConfigProblem before any request
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(f"{command} started", extra={"source": "api"})
    print(API_SOURCE_LINE, flush=True)

    def body() -> int:
        client, _ = _api_client(settings)
        widgets, _, _ = api_source.read_reports(tdx, client)
        read = DashboardRead(widgets=widgets, blocked=())
        print(
            format_rows_summary(
                read, tdx.max_pages_per_widget, verbose=args.verbose, row_limit=True
            )
        )
        if args.verbose:
            print(_requests_line(client))
        values = widgets.values()
        log.info(
            f"{command} summary",
            extra={
                "source": "api",
                "unreadable_date_shapes": [s for w in values for s in w.unreadable_dates],
                "hit_row_limit": [w.kind for w in values if w.hit_page_limit],
                "requests": dict(client.requests),
            },
        )
        return _finished(
            command,
            EXIT_OK,
            unclaimed=len(widgets["unclaimed"].rows),
            claimed=len(widgets["claimed"].rows),
            duplicates=sum(w.duplicates for w in values),
            unreadable_dates=sum(len(w.unreadable_dates) for w in values),
        )

    return _api_run(command, body)


def _tdx_api_check(args: argparse.Namespace) -> int:
    command = "tdx api check"
    settings = _api_settings(args.config)
    tdx = settings.tdx
    tz = ZoneInfo(tdx.display_timezone)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(f"{command} started")

    def body() -> int:
        client, tokens = _api_client(settings)
        user = client.get("/auth/getuser")
        role = api_source.text_value(user.get("SecurityRoleName")) if isinstance(user, dict) else ""
        # The role is interface wording; the person's name is never printed.
        print(f"API: signed in as {role or 'a user with no role name'}.")
        print(_token_line(tokens.get(), tdx))
        widgets: dict[str, WidgetRead] = {}
        unset: list[str] = []
        for kind in KINDS:
            report_id = getattr(tdx.api, f"{kind}_report_id")
            label = REPORT_LABELS[kind]
            if not report_id:
                unset.append(kind)
                print(f"{label}: not set (tdx.api.{kind}_report_id).")
                continue
            report = client.get(f"/reports/{report_id}", {"withData": "true"})
            read = api_source.map_report(report, kind, tz)
            limit = " (at its row limit)" if read.hit_page_limit else ""
            print(
                f"{label}: {len(read.rows)} row(s){limit}, "
                f"{api_source.column_count(report)} column(s), every needed column found."
            )
            widgets[kind] = read
        first = next((row.ticket_id for read in widgets.values() for row in read.rows), None)
        app_id, source = api_source.resolve_app_id(tdx, client, first)
        print(f"App id: {app_id} ({APP_ID_SOURCES[source]}).")
        if first is None:
            print("One feed: no ticket in the reports to read.")
        else:
            details = api_source.read_feed(client, app_id, first, tz)
            entries = details.feed.entries
            kinds = ", ".join(
                f"{sum(entry.kind == kind for entry in entries)} {kind}" for kind in KIND_ORDER
            )
            print(
                f"One feed: {len(entries)} entries ({kinds}); "
                f"{details.marker_stats['nested']} of them replies, "
                f"{details.feed.unreadable} unreadable."
            )
        print(_requests_line(client))
        if unset:
            print(
                f"Set {' and '.join(f'tdx.api.{kind}_report_id' for kind in unset)}; "
                f'"{RUN} tdx api reports" lists the reports you can see.',
                file=sys.stderr,
            )
        code = EXIT_CONFIG if unset else EXIT_OK
        return _finished(command, code, requests=sum(client.requests.values()))

    return _api_run(command, body)


def _tdx_api_reports(args: argparse.Namespace) -> int:
    command = "tdx api reports"
    settings = _api_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(f"{command} started")

    def body() -> int:
        client, _ = _api_client(settings)
        answer = client.get("/reports")
        found = api_source.report_list(answer)
        print(f"Reports you can see: {len(found)}.")
        for report in found:
            source = f"  ({report.source})" if report.source else ""
            print(f"  {report.report_id:>8}  {report.name}{source}")
        print(
            "Set tdx.api.unclaimed_report_id and claimed_report_id to the two reports that show "
            "the same tickets and columns as the dashboard's two widgets."
        )
        return _finished(command, EXIT_OK, reports=len(found))

    return _api_run(command, body)


def _compare_days(text: str) -> int:
    try:
        days = int(text)
    except ValueError:
        days = 0
    if not 1 <= days <= KEEP_DAYS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {KEEP_DAYS}")
    return days


def _tdx_api_compare(args: argparse.Namespace) -> int:
    """The shadow-mode comparison from the local records: counts and codes, ids only with
    --show-ids. Reads no TDX data and needs no token."""
    command = "tdx api compare"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(f"{command} started", extra={"days": args.days, "show_ids": args.show_ids})
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    records = ShadowLog(shadow_log_path(settings)).since(cutoff)
    for line in compare_report(records, days=args.days, show_ids=args.show_ids):
        print(line)
    return _finished(command, EXIT_OK, records=len(records))


def _check_api_source(tdx: TdxSettings) -> None:
    """ConfigProblem when tdx.source is "shadow" or "api" without the API enabled and both
    report ids set."""
    if tdx.source not in ("shadow", "api"):
        return
    problem = SHADOW_NEEDS_API if tdx.source == "shadow" else API_SOURCE_NEEDS_API
    if not tdx.api.enabled:
        raise ConfigProblem(problem)
    try:
        api_source.report_ids(tdx)
    except ConfigProblem:
        raise ConfigProblem(problem) from None


def _llm_line(run: AuditRun, settings: Settings) -> str | None:
    if run.llm is None:
        return None
    return format_follow_up_line(run.llm, run.llm_today, settings.llm.daily_token_budget)


def _tdx_watch(args: argparse.Namespace) -> int:
    command = "tdx watch"
    settings, tdx, allowed = _guarded_settings(args.config)
    if not desk_allowed(settings, args.to):
        return _finished(command, _config_problem(DESK_NOT_APPROVED))
    _check_ticket_template(tdx)
    source, start_url = start_address(settings, tdx)
    headless = tdx.headless and not args.headed
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    log.info(
        f"{command} started",
        extra={
            "dashboard_source": source,
            "path": describe_path(start_url),
            "headless": headless,
            "rules": repr(rule_settings(tdx)),
            "interval_minutes": tdx.watch.interval_minutes,
            "allowed_requests": [f"{entry.method} {entry.path_pattern}" for entry in allowed],
            "llm": settings.llm.enabled and not args.no_llm,
            "to": args.to,
            "source": tdx.source,
        },
    )
    _check_api_source(tdx)
    settings = settings.model_copy(update={"tdx": tdx})
    destination: Destination = args.to
    tz = ZoneInfo(settings.schedule.timezone)
    notify = webhook_notifier(settings)
    use_llm = not args.no_llm
    shadow: ShadowRunner | BrowserShadow | None = None
    if tdx.source == "api":
        # The API reads each run, the browser any run the API cannot; the browser beside it
        # for comparison only.
        read = ApiFirst(settings, headless=headless, use_llm=use_llm, notify=notify)
        shadow = BrowserShadow(settings, headless=headless)
    else:

        def read() -> AuditRun:
            return run_audit(settings, headless=headless, progress=None, use_llm=use_llm)

        if tdx.source == "shadow":
            shadow = ShadowRunner(settings, notify)

    def run_cycle(summary: SummaryRequest | None = None):
        run = read()
        return run, send_cycle(run, settings, destination, summary)

    # Windows only: the PC must not sleep while the loop runs (the Mac agent has caffeinate).
    with keep_awake():
        code = watch(
            settings,
            clock=lambda: datetime.now(tz),
            sleep=time.sleep,
            run_cycle=run_cycle,
            notify=notify,
            destination=destination,
            shadow=shadow,
            day_end=load_token,
        )
    return _finished(command, code)


def _autostart(settings: Settings) -> AutoStart | None:
    """This OS's start-at-login agent; None on Linux."""
    return for_platform(repo=REPO_DIR, log_dir=paths.log_dir(settings))


def _lifetime_lines(settings: Settings) -> list[str]:
    """The TDX session lifetime from ``session-log.json`` (timestamps only)."""
    sessions = SessionLog(session_log_path(settings)).sessions
    return format_lifetime(sessions, ZoneInfo(settings.schedule.timezone))


def _tdx_agent_install(args: argparse.Namespace) -> int:
    command = "tdx agent install"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    agent = _autostart(settings)
    if agent is None:
        return _finished(command, _config_problem(AGENT_NOT_HERE))
    if not (REPO_DIR / "pyproject.toml").is_file():
        return _finished(command, _config_problem(AGENT_NO_CHECKOUT))
    destination: Destination = args.to
    # The checks tdx watch makes on start, made now: at login nobody would see them fail.
    _check_api_source(settings.tdx)
    if not desk_allowed(settings, destination):
        return _finished(command, _config_problem(DESK_NOT_APPROVED))
    if _stored_webhook(destination) is None:
        return _finished(command, _config_problem(no_webhook(destination)))
    try:
        installed = agent.status().installed
    except StartupFolderError as error:
        return _finished(command, _config_problem(str(error)))
    if installed:
        try:
            answer = input("Replace the installed start-at-login agent? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Kept the installed agent.")
            return _finished(command, EXIT_OK, installed=0)
    extra_args = ["--headed"] if args.headed else []
    if args.config is not None:
        extra_args += ["--config", str(args.config.resolve())]
    try:
        agent_file = agent.install(destination=destination, extra_args=extra_args)
    except UvNotFoundError:
        return _finished(command, _config_problem(AGENT_NO_UV))
    except LaunchctlError as error:
        return _finished(command, _config_problem(f"the agent was not loaded ({error})"))
    except ValueError as error:
        return _finished(command, _config_problem(f"the Startup script was not written ({error})"))
    status = agent.status()
    to_desk = "" if destination == "personal" else f" Cards go to the {destination} chat."
    windows = isinstance(agent, WindowsStartup)
    if windows:
        print(f"Installed: tdx watch starts at every sign-in, in a minimised window.{to_desk}")
        print(f"Startup file: {agent_file}")
    else:
        print(f"Installed: tdx watch starts now and at every login.{to_desk}")
        print(f"Agent file: {agent_file}")
    print(f"Logs: {status.out_log}")
    print(f"      {status.err_log}")
    if windows:
        print(AGENT_WINDOWS_START)
        print(AGENT_WINDOWS_REMINDER)
    else:
        print(AGENT_REMINDER)
    return _finished(command, EXIT_OK, installed=1)


def _tdx_agent_uninstall(args: argparse.Namespace) -> int:
    command = "tdx agent uninstall"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    agent = _autostart(settings)
    if agent is None:
        return _finished(command, _config_problem(AGENT_NOT_HERE))
    try:
        removed = agent.uninstall()
    except LaunchctlError as error:
        return _finished(command, _config_problem(f"the agent was not unloaded ({error})"))
    except StartupFolderError as error:
        return _finished(command, _config_problem(str(error)))
    if not removed:
        print("No start-at-login agent is installed.")
    elif isinstance(agent, WindowsStartup):
        print(AGENT_WINDOWS_REMOVED)
    else:
        print("Removed the start-at-login agent; its tdx watch has stopped.")
    return _finished(command, EXIT_OK, removed=int(removed))


def _agent_status_lines(status: AgentStatus, *, windows: bool) -> list[str]:
    if windows:
        running = {True: "yes", False: "not found", None: "unknown"}[status.running]
        return [
            f"Agent: installed {_yes_no(status.installed)}; tdx watch window running {running}.",
            f"Startup file: {status.plist_path}",
        ]
    if status.last_exit_code is None:
        last_exit = "no exit recorded"
    else:
        last_exit = f"last exit code {status.last_exit_code}"
    return [
        f"Agent: installed {_yes_no(status.installed)}; loaded {_yes_no(status.loaded)}; "
        f"{last_exit}.",
        f"Agent file: {status.plist_path}",
    ]


def _agent_log_warnings(status: AgentStatus) -> list[str]:
    """Windows: the Startup script is installed but has not written ``watch.out.log``, for
    example because cmd could not append to a log in a missing folder."""
    if not status.installed or status.out_log.is_file():
        return []
    return [AGENT_NO_LOG.format(script=status.plist_path)]


def _tdx_agent_status(args: argparse.Namespace) -> int:
    command = "tdx agent status"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    agent = _autostart(settings)
    if agent is None:
        return _finished(command, _config_problem(AGENT_NOT_HERE))
    try:
        status = agent.status()
    except StartupFolderError as error:
        return _finished(command, _config_problem(str(error)))
    windows = isinstance(agent, WindowsStartup)
    for line in _agent_status_lines(status, windows=windows):
        print(line)
    print(f"Logs: {status.out_log}")
    print(f"      {status.err_log}")
    for line in _agent_log_warnings(status) if windows else []:
        print(line)
    for line in _lifetime_lines(settings):
        print(line)
    return _finished(command, EXIT_OK)


def _tdx_session(args: argparse.Namespace) -> int:
    command = "tdx session"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    for line in _lifetime_lines(settings):
        print(line)
    return _finished(command, EXIT_OK)


def _tdx_demo(args: argparse.Namespace) -> int:
    command = "tdx demo"
    settings = _load_settings(args.config)
    summary_wanted = args.summary or args.summary_kind is not None
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    # A synthetic card: it may go to the desk chat without teams.desk.approved.
    url = _stored_webhook(args.to) if args.send else None
    if args.send and url is None:
        return _finished(command, _config_problem(no_webhook(args.to)))
    client = None
    if not args.no_llm:
        try:
            key = get_api_key()
        except keyring.errors.KeyringError:
            key = None
        if key is not None:
            client = DemoLlmClient(settings, api_key=key)
    log.info(
        f"{command} started",
        extra={
            "send": args.send,
            "to": args.to,
            "llm": client is not None,
            "card_json": bool(args.card_json),
            "summary": args.summary,
            "summary_kind": args.summary_kind,
        },
    )
    tz = ZoneInfo(TdxSettings().display_timezone)
    now = datetime.now(tz)
    pages = demo_pages(demo_tickets(now), now, tz)
    progress = FeedProgress(sys.stderr, verbose=args.verbose)
    with (
        tempfile.TemporaryDirectory(prefix="ops-suite-demo-", ignore_cleanup_errors=True) as tmp,
        serve(pages) as server,
    ):
        demo = demo_settings(settings, server, Path(tmp), headless=not args.headed)
        print(DEMO_HEADER, flush=True)
        try:
            run = read_and_judge(demo, headless=not args.headed, progress=progress)
        except BrowserStartError as error:
            return _finished(command, _browser_failed(error))
        except NotSignedInError:
            print("ops-suite: the demo pages asked for a sign-in.", file=sys.stderr)
            return _finished(command, EXIT_NOT_SIGNED_IN)
        except DashboardStructureError as error:
            return _finished(command, _structure_failed(error, failed_step(error)))
        if client is None:
            run = replace(run, llm=off_outcome(run.result))
            llm_line = _llm_line(run, settings) if args.no_llm else DEMO_NO_KEY
        else:
            result, outcome, today = classify_step(
                demo,
                run.result,
                run.rows,
                run.feeds,
                rule_settings(demo.tdx),
                run.now,
                client=client,
                data_class="synthetic",
            )
            run = replace(run, result=result, llm=outcome, llm_today=today)
            llm_line = _llm_line(run, settings)
        print(
            format_audit_output(
                run.dashboard,
                run.feeds,
                run.result,
                demo.tdx.max_pages_per_widget,
                show_ids=True,
                verbose=args.verbose,
                desk_remembered=run.desk_remembered,
                llm=run.llm,
                llm_line=llm_line,
            )
        )
        state = AlertState(alert_state_path(demo, args.to))
        if summary_wanted:
            # As if a summary slot were due now; a full card is built even for a clear queue.
            request = demo_summary_request(run, demo, args.summary_kind or "full")
            summary = draft_summary(run, demo, state, args.to, request)
            card = demo_card(summary.card) if summary.card is not None else None
            if request.kind == "delta":
                sent = f"{summary.new} new, {summary.cleared} cleared, {summary.open} open"
            else:
                sent = f"{summary.open} open, {summary.new} new"
        else:
            draft = draft_card(run, demo, state, args.to)
            card = demo_card(draft.card) if draft.card is not None else None
            sent = counts_phrase(draft.fitted.new, 0)
    log.info(
        f"{command} summary",
        extra={
            "counts": dict(run.result.counts),
            "llm": dict(run.llm.counts) if run.llm is not None else None,
        },
    )
    if card is None:
        print("Teams: nothing to send.")
        return _finished(command, EXIT_OK, findings=0)
    if args.card_json:
        args.card_json.write_text(json.dumps(card, indent=2), encoding="utf-8")
        print(f"Card JSON: {args.card_json}")
    if url is not None:
        try:
            post_card(url, card, timeout=settings.teams.timeout_seconds)
        except TeamsPostError as error:
            label = teams_label(args.to)
            print(f"{label}: the demo card was not sent ({error.reason}).", file=sys.stderr)
            return _finished(command, EXIT_TEAMS)
        kind = "summary" if summary_wanted else "card"
        print(f"{teams_label(args.to)}: sent 1 demo {kind} ({sent}).")
    return _finished(command, EXIT_OK, findings=len(run.result.findings))


def no_webhook(destination: Destination) -> str:
    """The one-line problem when ``destination`` has no saved webhook."""
    if destination == "personal":
        return NO_WEBHOOK
    return f'no {destination} webhook is saved; run "{set_webhook_command(destination)}" first'


def _stored_webhook(destination: Destination = "personal") -> str | None:
    """The saved webhook URL, None if there is none; ConfigProblem if the keychain fails."""
    try:
        return get_webhook_url(destination)
    except keyring.errors.KeyringError as error:
        raise ConfigProblem(
            f"the system keychain could not be read ({type(error).__name__})"
        ) from None


def _teams_set_webhook(args: argparse.Namespace) -> int:
    command = "teams set-webhook"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    destination: Destination = args.to
    # "webhook" for the personal chat, as before; "desk webhook" for the desk chat.
    which = "webhook" if destination == "personal" else f"{destination} webhook"
    if _stored_webhook(destination) is not None:
        try:
            answer = input(f"Replace the saved {which}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print(f"Kept the saved {which}.")
            return _finished(command, EXIT_OK, replaced=0)
    url = getpass.getpass(f"{which.capitalize()} URL (input hidden): ")
    try:
        set_webhook_url(url, settings.teams.webhook_host_suffixes, destination)
    except ValueError as error:
        return _finished(command, _config_problem(f"the webhook was not saved: {error}"))
    except keyring.errors.KeyringError as error:
        explanation = f"the system keychain refused the {which} ({type(error).__name__})"
        return _finished(command, _config_problem(explanation))
    print(f"{which.capitalize()} saved in the system keychain.")
    return _finished(command, EXIT_OK, replaced=1)


def _teams_test(args: argparse.Namespace) -> int:
    command = "teams test"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    # A synthetic card: it may go to the desk chat without teams.desk.approved.
    url = _stored_webhook(args.to)
    if url is None:
        return _finished(command, _config_problem(no_webhook(args.to)))
    summary = settings.tdx.summary
    if args.mention and summary.mention == "none":
        explanation = (
            'tdx.summary.mention is "none"; set it to "everyone" or "people" (with '
            "tdx.summary.mention_people) in your own config to test a mention"
        )
        return _finished(command, _config_problem(explanation))
    card = synthetic_test_card(datetime.now(ZoneInfo(settings.schedule.timezone)))
    if args.mention:
        card = with_mentions(card, summary.mention, summary.mention_people)
    try:
        post_card(url, card, timeout=settings.teams.timeout_seconds)
    except TeamsPostError as error:
        print(f"ops-suite: the test card was not sent ({error.reason}).", file=sys.stderr)
        return _finished(command, EXIT_TEAMS)
    sent = "Test card sent" if args.to == "personal" else f"Test card sent to the {args.to} chat"
    if args.mention:
        # The mode and a count only: the addresses stay in the config.
        who = (
            "everyone"
            if summary.mention == "everyone"
            else f"{len(summary.mention_people)} "
            f"{'person' if len(summary.mention_people) == 1 else 'people'}"
        )
        sent += f" with a mention ({who}); check in Teams who was notified"
    print(f"{sent}.")
    return _finished(command, EXIT_OK)


class _LlmTestReply(BaseModel):
    ok: bool


def _stored_api_key() -> str | None:
    """The saved API key, None if there is none; ConfigProblem if the keychain fails."""
    try:
        return get_api_key()
    except keyring.errors.KeyringError as error:
        raise ConfigProblem(
            f"the system keychain could not be read ({type(error).__name__})"
        ) from None


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _llm_set_key(args: argparse.Namespace) -> int:
    command = "llm set-key"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    if _stored_api_key() is not None:
        try:
            answer = input("Replace the saved API key? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Kept the saved API key.")
            return _finished(command, EXIT_OK, replaced=0)
    key = getpass.getpass("Anthropic API key (input hidden): ")
    try:
        set_api_key(key)
    except ValueError as error:
        return _finished(command, _config_problem(f"the key was not saved: {error}"))
    except keyring.errors.KeyringError as error:
        explanation = f"the system keychain refused the key ({type(error).__name__})"
        return _finished(command, _config_problem(explanation))
    print("API key saved in the system keychain.")
    return _finished(command, EXIT_OK, replaced=1)


def _llm_status(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    key = _stored_api_key()
    llm = settings.llm
    used = LlmClient(settings, api_key=key or "").used_today()
    calls = "call" if used.calls == 1 else "calls"
    print(
        f"LLM: enabled {_yes_no(llm.enabled)}; "
        f"Institutional data allowed {_yes_no(llm.allow_institutional_data)}; "
        f"model {llm.model}; key saved {_yes_no(key is not None)}."
    )
    print(
        f"Today: {used.calls} {calls}, {used.budget_tokens:,} of {llm.daily_token_budget:,} "
        f"tokens, ~${used.cost_usd:.4f}."
    )
    return _finished("llm status", EXIT_OK)


def _llm_test(args: argparse.Namespace) -> int:
    command = "llm test"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    key = _stored_api_key()
    if key is None:
        return _finished(command, _config_problem(NO_API_KEY))
    client = LlmClient(settings, api_key=key)
    try:
        result = client.classify(
            scrub(LLM_TEST_SENTENCE),
            system=LLM_TEST_SYSTEM,
            output=_LlmTestReply,
            data_class="synthetic",
            purpose="llm_test",
            force_enabled=True,
        )
    except (LlmUnavailable, LlmFailed) as error:
        print(f"LLM test: failed ({error.reason}).", file=sys.stderr)
        return _finished(command, EXIT_LLM)
    if result.status == "refused":
        print("LLM test: failed (refused).", file=sys.stderr)
        return _finished(command, EXIT_LLM)
    usage = result.usage
    tokens_in = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    cost = f"~${usage.cost_usd:.4f}"
    print(f"LLM test: ok (in {tokens_in}, out {usage.output_tokens} tokens, {cost}).")
    return _finished(command, EXIT_OK)


def _llm_eval(args: argparse.Namespace) -> int:
    command = "llm eval"
    settings = _load_settings(args.config)
    configure_logging(paths.log_dir(settings), verbose=args.verbose)
    key = _stored_api_key()
    if key is None:
        return _finished(command, _config_problem(NO_API_KEY))
    path = EVAL_DIR / f"follow_up_{args.eval_set}.jsonl"
    try:
        cases = load_eval_cases(path)
    except (OSError, ValueError) as error:
        explanation = f"the eval set could not be read ({type(error).__name__}: {error})"
        return _finished(command, _config_problem(explanation))
    client = LlmClient(settings, api_key=key)
    report = run_eval(cases, client, max_input_chars=settings.llm.max_input_chars)
    for line in format_eval_report(report, args.eval_set):
        print(line)
    counts = {"total": report.total, "correct": report.correct, "unsafe": report.unsafe}
    if report.stopped == "no_key":
        return _finished(command, _config_problem(NO_API_KEY), **counts)
    if report.stopped:
        print(f"LLM eval: stopped ({report.stopped}).", file=sys.stderr)
        return _finished(command, EXIT_LLM, **counts)
    return _finished(command, EXIT_OK, **counts)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if is_store_python():
        # The Store Python's private AppData copy would hide the session cookies.
        if getattr(args, "live", False):
            return _config_problem(STORE_PYTHON_REFUSED)
        print(f"ops-suite: warning: {STORE_PYTHON_WARNING}", file=sys.stderr)
    try:
        return args.handler(args)
    except ConfigProblem as problem:
        return _config_problem(str(problem))
    except KeyboardInterrupt:
        print("ops-suite: cancelled; nothing was saved.", file=sys.stderr)
        return EXIT_CANCELLED
