"""Typed settings.

Precedence, highest first: CLI flags (applied by the caller), environment variables
(``OPS_SUITE_`` prefix, ``__`` between levels), a ``.env`` file in the working directory,
the TOML config file, built-in defaults. Secrets never go here; they belong in ``keyring``.
"""

import os
import re
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from ops_suite.common.paths import default_config_path

CONFIG_ENV_VAR = "OPS_SUITE_CONFIG"
DEFAULT_TEAM_NAME = "the help desk"
TEAM_NAME_MAX_CHARS = 40
MAILBOX_MAX_CHARS = 254

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WINDOW = re.compile(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})")
_TIME = re.compile(r"(\d{2}):(\d{2})")
# A sign-in address for a Teams mention: one "@", no spaces or markup.
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def _is_mailbox(address: str) -> bool:
    """One email address: a single ``@`` with text on both sides, no whitespace, a dot inside
    the domain, at most ``MAILBOX_MAX_CHARS`` characters."""
    if len(address) > MAILBOX_MAX_CHARS or any(c.isspace() for c in address):
        return False
    local, at, domain = address.partition("@")
    if not (at and local and "@" not in domain):
        return False
    return "." in domain.strip(".")


class ConfigProblem(Exception):
    """A setting or argument is missing or invalid; the message is the one-line explanation."""


# The TOML file for the Settings() being built; set only inside load_settings().
_config_file: ContextVar[Path | None] = ContextVar("ops_suite_config_file", default=None)


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


# An empty string in the config file means "not set".
OptionalPath = Annotated[Path | None, BeforeValidator(_blank_to_none)]


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown time zone {value!r}") from error
    return value


def parse_window(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """``"HH:MM-HH:MM"`` as ((h, m), (h, m)), or None for ``""`` (closed).

    ValueError unless the start is before the end on the same day.
    """
    text = text.strip()
    if not text:
        return None
    match = _WINDOW.fullmatch(text)
    if match is None:
        raise ValueError(f'desk hours must look like "07:30-22:00" or "" for closed, not {text!r}')
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if max(start_h, end_h) > 23 or max(start_m, end_m) > 59:
        raise ValueError(f"desk hours {text!r} name a time that does not exist")
    if (start_h, start_m) >= (end_h, end_m):
        raise ValueError(f"desk hours {text!r} must start before they end (none past midnight)")
    return (start_h, start_m), (end_h, end_m)


def parse_time(text: str) -> tuple[int, int]:
    """``"HH:MM"`` as (h, m); ValueError for anything else."""
    match = _TIME.fullmatch(text.strip())
    if match is None:
        raise ValueError(f'summary times must look like "07:30", not {text!r}')
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError(f"summary time {text!r} does not exist")
    return hour, minute


class PathsSettings(BaseModel):
    data_dir: OptionalPath = None
    log_dir: OptionalPath = None


class WorkMailSettings(BaseModel):
    mailbox_address: str = ""
    drop_dir: OptionalPath = None
    retention_days: int = Field(default=7, ge=1)
    automated_senders: list[str] = []
    automated_local_parts: list[str] = [
        "no-reply",
        "noreply",
        "donotreply",
        "do-not-reply",
        "mailer-daemon",
        "postmaster",
    ]


class MailSettings(BaseModel):
    work: WorkMailSettings = WorkMailSettings()


class TdxWidgetSettings(BaseModel):
    """Dashboard widget titles, matched case-insensitively after collapsing whitespace."""

    unclaimed_title: str = Field(default="Unclaimed Tickets in My Group(s)", min_length=1)
    claimed_title: str = Field(
        default="Active Tickets Claimed by Others in My Group(s)", min_length=1
    )


class TdxRulesSettings(BaseModel):
    """The stale rules (tdx audit). Staff names are personal data: keep them in your own config."""

    stale_hours: int = Field(default=48, ge=1, le=720)
    event_over_hours: int = Field(default=24, ge=1, le=720)
    event_tbd_days: int = Field(default=3, ge=0, le=60)
    # Optional override, normally empty. Normally not needed: desk members are learned from
    # Prim Resp, private notes, merge notices and the desk signature in their replies.
    extra_staff_names: list[str] = []
    # A visible reply whose own text (quoted mail cut) holds one of these marks its author as
    # desk. Matched case-insensitively after collapsing whitespace; the text is never kept.
    desk_signatures: list[str] = ["SCIS Commons Support", "CCI Commons Support"]
    # No client-waiting finding when the requester is a known desk member (strong signals only).
    skip_desk_requestors: bool = True
    # Remember desk members across runs (desk-members.json, salted hashes only).
    desk_memory: bool = True
    desk_memory_forget_days: int = Field(default=180, ge=30, le=730)
    # The desk replied last this many days ago, client silent since: "ready to close".
    close_after_days: int = Field(default=3, ge=1, le=30)
    # Summary rows: same requester and titles at least this similar (token Jaccard) are
    # marked "possible duplicate". Compared in memory only.
    duplicate_title_similarity: float = Field(default=0.6, gt=0, le=1)

    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        """Show only how many staff names there are, never the names."""
        for name, value in super().__repr_args__():
            yield name, (len(value) if name == "extra_staff_names" else value)


class TdxEventSettings(BaseModel):
    """Event request forms: which tickets, the reminder windows, and the form's
    option prefixes (matched case-insensitively at the start of an answer)."""

    # Unclaimed tickets still titled like this get their description and feed read. The
    # college is now the SCIS (formerly CCI), so both names match until the form changes.
    form_titles: list[str] = ["CCI Event Request Form", "SCIS Event Request Form"]
    outreach_days: int = Field(default=14, ge=1, le=90)
    details_pending_days: int = Field(default=7, ge=1, le=90)
    urgent_days: int = Field(default=3, ge=0, le=30)
    # An event this close raises its feed-state finding at once, at high priority.
    near_event_days: int = Field(default=2, ge=0, le=30)
    marketing_only_prefixes: list[str] = ["Marketing/advertising"]
    av_service_prefixes: list[str] = ["A/V & Technical Support"]
    room_space_prefixes: list[str] = ["Classroom Space"]
    known_option_prefixes: list[str] = [
        "To host an event",
        "Lobby Area",
        "Other",
        "CCI Operational Assistance",
        "SCIS Operational Assistance",
        "Event Catering",
        "Marketing Support",
    ]
    # The mailbox requesters add as a Zoom alternative host, so the desk can start and record
    # the meeting; event outreach rows show it. Empty: they say "our shared mailbox".
    alt_host_mailbox: str = ""

    @field_validator("alt_host_mailbox")
    @classmethod
    def _valid_mailbox(cls, value: str) -> str:
        address = value.strip()
        if address and not _is_mailbox(address):
            # Never echoes the value.
            raise ValueError(
                "the alternative-host mailbox must be one email address like "
                f"events@example.edu (at most {MAILBOX_MAX_CHARS} characters), or empty"
            )
        return address


class TdxAlertSettings(BaseModel):
    """tdx audit --send and tdx watch: when a finding is sent again, and the card's size."""

    realert_days: int = Field(default=3, ge=1, le=60)
    old_finding_days: int = Field(default=30, ge=1, le=3650)
    old_realert_days: int = Field(default=7, ge=1, le=90)
    title_chars: int = Field(default=80, ge=20, le=200)
    max_rows_per_card: int = Field(default=25, ge=1, le=50)
    # "Ready to close" findings go in the shift summaries only unless this is true.
    alert_ready_to_close: bool = False


class TdxWatchSettings(BaseModel):
    interval_minutes: int = Field(default=30, ge=30, le=60)
    failures_before_stop: int = Field(default=3, ge=1, le=10)
    # Minutes between quiet checks while the TDX sign-in has expired.
    signed_out_retry_minutes: int = Field(default=10, ge=5, le=30)


class TdxSummarySettings(BaseModel):
    """Queue summaries at shift starts, posted by ``tdx watch``. Off by default.

    Mention addresses are personal data: keep them in your own config, never the repo.
    """

    enabled: bool = False
    # The chat the summaries go to; only a tdx watch posting to this chat sends them.
    destination: Literal["personal", "desk"] = "desk"
    # A slot the auditor could not post on time is posted late until this many minutes after it.
    grace_minutes: int = Field(default=90, ge=15, le=240)
    # "HH:MM" per weekday, each inside that day's desk hours; a missing day has no summaries.
    times: dict[Weekday, list[str]] = {
        "mon": ["07:30", "12:00", "17:00"],
        "tue": ["07:30", "12:00", "17:00"],
        "wed": ["07:30", "12:00", "17:00"],
        "thu": ["07:30", "12:00", "17:00"],
        "fri": ["07:30", "12:00", "17:00"],
        "sat": ["08:00", "12:00", "17:00"],
        "sun": ["12:00"],
    }
    mention: Literal["none", "everyone", "people"] = "none"
    # "Events this week": unclaimed events with a date this many days from the summary time.
    events_days: int = Field(default=7, ge=1, le=30)
    # Sign-in addresses (UPNs) to mention with mention = "people".
    mention_people: list[str] = []
    # While summaries go to the desk chat, a "tdx watch --to desk" posts no alert
    # card each cycle there (summaries only) unless desk_alerts; those cards go to the personal
    # chat instead with personal_alerts. With summaries off, --to desk alerts the desk as before.
    desk_alerts: bool = False
    personal_alerts: bool = True
    # The one exception to summaries only: an event finding within tdx.events.near_event_days
    # of its event goes to the desk chat at once, on a short card, once per ticket and kind.
    # It applies only while summaries go to the desk chat and teams.desk.approved is true.
    urgent_events_to_desk: bool = True

    @field_validator("times")
    @classmethod
    def _valid_times(cls, value: dict[Weekday, list[str]]) -> dict[Weekday, list[str]]:
        for day, times in value.items():
            for text in times:
                parse_time(text)  # ValueError names the bad time
            if len(set(times)) != len(times):
                raise ValueError(f"summary times on {day} repeat a time")
        return value

    @field_validator("mention_people")
    @classmethod
    def _valid_people(cls, value: list[str]) -> list[str]:
        for position, address in enumerate(value, start=1):
            if _ADDRESS.fullmatch(address.strip()) is None:
                # Names the position, never the address.
                raise ValueError(f"entry {position} is not a sign-in address like name@example.edu")
        if len({address.strip().lower() for address in value}) != len(value):
            raise ValueError("an address is listed twice")
        return [address.strip() for address in value]

    @model_validator(mode="after")
    def _people_listed(self) -> "TdxSummarySettings":
        if self.mention == "people" and not self.mention_people:
            raise ValueError('mention = "people" needs at least one address in mention_people')
        return self

    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        """Show only how many addresses there are, never the addresses."""
        for name, value in super().__repr_args__():
            yield name, (len(value) if name == "mention_people" else value)


class TdxApiSettings(BaseModel):
    """The TDX Web API as a second, read-only source of the same rows and feeds. Off by
    default, and the browser stays the default source. The token comes from the person's own
    SSO sign-in and lives in keyring, never here.

    Unknown keys are refused rather than ignored: a misspelt report id must stop the tool,
    not read nothing."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # The Web API's path on tdx.base_url's host.
    base_path: str = "/TDWebApi/api"
    # The ticketing application's id; 0 derives it (ticket_url_template, else the first
    # ticket's own record) and says where it came from.
    app_id: int = Field(default=0, ge=0)
    # The Report Builder reports that hold the same rows as the two dashboard widgets
    # ("tdx api reports" lists the ones you can see).
    unclaimed_report_id: int = Field(default=0, ge=0)
    claimed_report_id: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    # TDX limits most read endpoints to 60 calls a minute per address (report reads to 30 a
    # minute per person); one call per 1.1 s stays under every one of them.
    min_request_interval_ms: int = Field(default=1100, ge=1000, le=60_000)
    # The Web API has a sign-in of its own: how long tdx login and tdx api token --headed
    # wait for the person to finish it in the window (MFA included).
    token_sign_in_minutes: int = Field(default=5, ge=1, le=15)
    # tdx.source = "api": a cycle's API read sends no request after this long; the browser
    # reads that cycle instead.
    max_cycle_seconds: int = Field(default=300, ge=30, le=1200)

    @field_validator("base_path")
    @classmethod
    def _valid_base_path(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", value):
            raise ValueError('must be a path such as "/TDWebApi/api" (no host or query)')
        return value


class TdxShadowSettings(BaseModel):
    """The side-by-side comparison of the two sources (counts only). With ``tdx.source =
    "shadow"`` each tdx watch cycle also reads the queue through the TDX Web API, and the
    browser makes every card; with ``"api"`` the API makes the cards and, while ``enabled``,
    the browser is read beside it for comparison only."""

    model_config = ConfigDict(extra="forbid")

    # tdx.source = "api" only: read the browser beside the API each cycle to compare.
    enabled: bool = True
    # The API part of a shadow cycle stops after this long; a stop is recorded, never a
    # failed cycle.
    max_seconds: int = Field(default=300, ge=30, le=1200)


class TdxSettings(BaseModel):
    base_url: str = ""
    # Where tdx watch and tdx audit read the queue: the browser; the browser with the TDX Web
    # API read beside it for comparison ("shadow"); or the API, with the browser reading any
    # cycle the API cannot ("api"). Both need tdx.api.enabled and both report ids.
    source: Literal["browser", "shadow", "api"] = "browser"
    profile_dir: OptionalPath = None
    # Where tdx rows starts; overrides the address saved by tdx set-dashboard. Same host.
    dashboard_url: str = ""
    display_timezone: str = "America/New_York"
    headless: bool = True
    max_pages_per_widget: int = Field(default=20, ge=1, le=100)
    page_timeout_seconds: int = Field(default=30, ge=5, le=120)
    # Seconds Chromium gets to start; Playwright's own default (30) is short for a busy Mac.
    browser_launch_timeout_seconds: int = Field(default=60, ge=15, le=180)
    # Extra "METHOD /path-pattern" entries for the read-only request guard.
    allowed_requests: list[str] = []
    widgets: TdxWidgetSettings = TdxWidgetSettings()
    # tdx audit: the pause between tickets, and how many feeds one run opens (oldest first).
    feed_delay_seconds: float = Field(default=2, ge=0, le=30)
    max_feeds_per_run: int = Field(default=40, ge=1, le=200)
    # Optional ticket address, "{id}" standing for the ticket id; used when the grid's link
    # does not lead to a feed. Same host as base_url.
    ticket_url_template: str = ""
    rules: TdxRulesSettings = TdxRulesSettings()
    events: TdxEventSettings = TdxEventSettings()
    alerts: TdxAlertSettings = TdxAlertSettings()
    watch: TdxWatchSettings = TdxWatchSettings()
    summary: TdxSummarySettings = TdxSummarySettings()
    api: TdxApiSettings = TdxApiSettings()
    shadow: TdxShadowSettings = TdxShadowSettings()

    @field_validator("display_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        return _check_timezone(value)

    @field_validator("allowed_requests")
    @classmethod
    def _valid_allowed_requests(cls, value: list[str]) -> list[str]:
        from ops_suite.tdx.guard import parse_allowed

        parse_allowed(value)  # ValueError names the bad entry
        return value


class TeamsPersonalSettings(BaseModel):
    """The person's own chat."""

    show_titles: bool = True


class TeamsDeskSettings(BaseModel):
    """The desk's shared chat, next to the person's own chat. Real ticket cards go there
    only once the desk has approved them."""

    # True only once the desk's lead has approved real ticket cards in the shared chat, in
    # writing. Until then only synthetic cards (teams test, tdx demo --send) may go there.
    approved: bool = False
    # Titles can contain names, so desk cards leave them out unless this is set.
    show_titles: bool = False
    # Who desk cards ask to act, as a sentence names them ("the help desk", "the Service Desk").
    # Personal cards say "you" instead.
    team_name: str = DEFAULT_TEAM_NAME

    @field_validator("team_name")
    @classmethod
    def _valid_team_name(cls, value: str) -> str:
        if any(char in value for char in "\r\n  "):
            raise ValueError("the team name must be one line")
        value = value.strip()
        if not 1 <= len(value) <= TEAM_NAME_MAX_CHARS:
            raise ValueError(f"the team name must be 1 to {TEAM_NAME_MAX_CHARS} characters")
        return value


class TeamsSettings(BaseModel):
    """The Workflows webhooks. Their URLs are secrets and live in keyring, never here."""

    webhook_host_suffixes: list[str] = [".logic.azure.com", ".api.powerplatform.com"]
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    personal: TeamsPersonalSettings = TeamsPersonalSettings()
    desk: TeamsDeskSettings = TeamsDeskSettings()

    def show_titles(self, destination: Literal["personal", "desk"]) -> bool:
        return self.desk.show_titles if destination == "desk" else self.personal.show_titles


class LlmSettings(BaseModel):
    """The LLM step. Off by default; text of the ``institutional`` data class (the live TDX queue,
    the shared mailbox) needs its own switch on top. The API key is a secret and lives in keyring,
    never here.

    Unknown keys are refused rather than ignored, from any source: a misspelt or retired switch
    must stop the tool loudly, not leave a gate in a state nobody chose."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allow_institutional_data: bool = False
    model: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high"] = "low"
    max_output_tokens: int = Field(default=256, ge=16, le=4096)
    max_input_chars: int = Field(default=2000, ge=200, le=20000)
    daily_token_budget: int = Field(default=100_000, ge=1_000, le=10_000_000)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    # US dollars per million tokens, for the cost estimate only (claude-haiku-5-5, prompts up
    # to 100K tokens, checked 2026-10-16). Cache writes are estimated at 1.25x the input price.
    input_usd_per_mtok: float = 0.10
    output_usd_per_mtok: float = 0.50
    cache_read_usd_per_mtok: float = 0.01


class ScheduleSettings(BaseModel):
    """Desk hours: one "HH:MM-HH:MM" window per weekday; "" or a missing day = closed."""

    timezone: str = "America/New_York"
    desk_hours: dict[Weekday, str] = {
        "mon": "07:30-22:00",
        "tue": "07:30-22:00",
        "wed": "07:30-22:00",
        "thu": "07:30-22:00",
        "fri": "07:30-22:00",
        "sat": "08:00-18:00",
        "sun": "12:00-17:00",
    }

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        return _check_timezone(value)

    @field_validator("desk_hours")
    @classmethod
    def _valid_windows(cls, value: dict[Weekday, str]) -> dict[Weekday, str]:
        for text in value.values():
            parse_window(text)  # ValueError names the bad window
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPS_SUITE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    paths: PathsSettings = PathsSettings()
    mail: MailSettings = MailSettings()
    tdx: TdxSettings = TdxSettings()
    teams: TeamsSettings = TeamsSettings()
    llm: LlmSettings = LlmSettings()
    schedule: ScheduleSettings = ScheduleSettings()

    @model_validator(mode="after")
    def _summaries_inside_desk_hours(self) -> "Settings":
        """Every summary time lies inside that weekday's desk hours (checked when enabled)."""
        summary = self.tdx.summary
        if not summary.enabled:
            return self
        for day, times in summary.times.items():
            text = self.schedule.desk_hours.get(day, "")
            window = parse_window(text)
            for time_text in times:
                when = parse_time(time_text)
                if window is None or not window[0] <= when < window[1]:
                    hours = f"desk hours {text}" if window is not None else "the desk is closed"
                    raise ValueError(
                        f"tdx.summary.times: {time_text} on {day} is outside desk hours ({hours})"
                    )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = [init_settings, env_settings, dotenv_settings]
        toml_file = _config_file.get()
        if toml_file is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_file))
        return tuple(sources)


def resolve_config_path(config_path: Path | None = None) -> Path:
    """``config_path``, else ``OPS_SUITE_CONFIG``, else the platformdirs config file."""
    if config_path is not None:
        return config_path
    from_env = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if from_env:
        return Path(from_env)
    return default_config_path()


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from every source. A missing config file is not an error."""
    token = _config_file.set(resolve_config_path(config_path))
    try:
        return Settings()
    finally:
        _config_file.reset(token)
