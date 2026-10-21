"""The LLM client: every Claude API call in the suite goes through ``LlmClient.classify``.

What it adds around the official ``anthropic`` SDK (docs/ARCHITECTURE.md, "Payload 06 design"):

- It accepts only ``ScrubbedText``, which only the scrubber can build.
- Checks before any network: ``llm.enabled``, the Drexel-data gate, the daily token budget and
  the API key. Each failure is an ``LlmUnavailable`` with a reason code.
- Structured output from a pydantic model, effort from config, and the system prompt as one
  cached block.
- TLS verified against the operating system's trust store through ``truststore`` ("TLS on the
  Mac"); verification is never turned off.
- One ``llm call`` log line per call and a daily total in ``llm-usage.json``: counts, model and
  cost only. Never the text, the system prompt or the output.

The API key is a secret: it lives only in ``keyring`` (service ``ops-suite``, user
``anthropic-api-key``) and never appears in config, logs, output or exception messages.
"""

import json
import logging
import math
import os
import ssl
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import anthropic
import keyring
import keyring.errors
import truststore
from pydantic import BaseModel, ValidationError

from ops_suite.common import paths
from ops_suite.common.scrub import DataClass, ScrubbedText

if TYPE_CHECKING:
    from ops_suite.common.config import Settings

log = logging.getLogger(__name__)

KEYRING_SERVICE = "ops-suite"  # the same service as the Teams webhook
KEYRING_API_KEY_USER = "anthropic-api-key"
API_KEY_PREFIX = "sk-ant-"
USAGE_FILE = "llm-usage.json"
USAGE_KEEP_DAYS = 31
# Cache reads count towards the daily budget at 10% (rounded up), like their price.
CACHE_READ_BUDGET_WEIGHT = 0.1
# A 5-minute cache write costs 1.25x the input price.
CACHE_WRITE_PRICE_FACTOR = 1.25
MAX_RETRIES = 2
DATA_CLASSES: tuple[DataClass, ...] = ("synthetic", "personal", "drexel")

_SCHEMA = 1


# --- the key -----------------------------------------------------------------------------


def get_api_key() -> str | None:
    """The stored Anthropic API key, or None when nothing is stored."""
    return keyring.get_password(KEYRING_SERVICE, KEYRING_API_KEY_USER) or None


def set_api_key(key: str) -> None:
    """Check the key's prefix, then store it in the system keychain. The message never
    quotes the key."""
    key = key.strip()
    if not key.startswith(API_KEY_PREFIX):
        raise ValueError(f"not an Anthropic API key (it should start with {API_KEY_PREFIX})")
    keyring.set_password(KEYRING_SERVICE, KEYRING_API_KEY_USER, key)


# --- results and errors ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LlmUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float  # estimate from LlmSettings prices


@dataclass(frozen=True, slots=True)
class LlmResult[T: BaseModel]:
    value: T | None  # the parsed pydantic object; None on refusal
    status: Literal["ok", "refused"]
    usage: LlmUsage


@dataclass(frozen=True, slots=True)
class LlmDayUsage:
    """One local day's totals in ``llm-usage.json``."""

    day: str  # ISO date in schedule.timezone
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def budget_tokens(self) -> int:
        """Tokens counted against the daily budget: input + output + cache writes, and cache
        reads at 10%, rounded up."""
        cache_reads = math.ceil(self.cache_read_tokens * CACHE_READ_BUDGET_WEIGHT)
        return self.input_tokens + self.output_tokens + self.cache_write_tokens + cache_reads

    def plus(self, usage: LlmUsage) -> "LlmDayUsage":
        return LlmDayUsage(
            day=self.day,
            calls=self.calls + 1,
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_read_tokens=self.cache_read_tokens + usage.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + usage.cache_write_tokens,
            cost_usd=self.cost_usd + usage.cost_usd,
        )


class LlmUnavailable(Exception):
    """No call was made. ``reason`` is a code."""

    def __init__(
        self, reason: Literal["disabled", "gate", "budget", "no_key", "scrub_refused"]
    ) -> None:
        super().__init__(f"LLM call not made ({reason})")
        self.reason = reason


class LlmFailed(Exception):
    """The call was made and failed. ``reason`` is a code: "timeout", "connection",
    "rate_limited", "http_<status>", "bad_output"."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"LLM call failed ({reason})")
        self.reason = reason


# --- the usage file ----------------------------------------------------------------------


class _UsageFile:
    """``llm-usage.json``: ``{"schema": 1, "days": {"<ISO date>": {"calls": n, ...}}}``.

    Counts only. A corrupt file is logged as ``llm_usage_reset`` and the totals start fresh.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, LlmDayUsage]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["schema"] != _SCHEMA:
                raise ValueError("unknown schema")
            names = {item.name for item in fields(LlmDayUsage)} - {"day"}
            days: dict[str, LlmDayUsage] = {}
            for day, record in data["days"].items():
                date.fromisoformat(day)
                if set(record) != names:
                    raise ValueError("unexpected fields")
                days[day] = LlmDayUsage(
                    day=day,
                    calls=int(record["calls"]),
                    input_tokens=int(record["input_tokens"]),
                    output_tokens=int(record["output_tokens"]),
                    cache_read_tokens=int(record["cache_read_tokens"]),
                    cache_write_tokens=int(record["cache_write_tokens"]),
                    cost_usd=float(record["cost_usd"]),
                )
            return days
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log.warning("llm_usage_reset")
            return {}

    def save(self, days: dict[str, LlmDayUsage], today: date) -> None:
        """Write atomically (a temporary file, then ``os.replace``), keeping 31 days."""
        oldest = today - timedelta(days=USAGE_KEEP_DAYS - 1)
        data = {
            "schema": _SCHEMA,
            "days": {
                day: {
                    item.name: getattr(usage, item.name)
                    for item in fields(LlmDayUsage)
                    if item.name != "day"
                }
                for day, usage in sorted(days.items())
                if date.fromisoformat(day) >= oldest
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)


# --- the client --------------------------------------------------------------------------


def _tls_context() -> ssl.SSLContext:
    """Verify certificates against the operating system's trust store (see teams.py)."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class LlmClient:
    def __init__(
        self,
        settings: "Settings",
        *,
        api_key: str | None = None,
        sdk_client: "anthropic.Anthropic | None" = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``api_key`` None reads the key from keyring; ``sdk_client`` lets tests inject a fake.

        The real SDK client is built on the first call that passes every check.
        """
        self._settings = settings
        self._llm = settings.llm
        if api_key is None:
            try:
                api_key = get_api_key()
            except keyring.errors.KeyringError as error:
                log.warning("llm key unreadable", extra={"error": type(error).__name__})
        self._api_key = api_key or None
        self._sdk_client = sdk_client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._usage = _UsageFile(paths.llm_dir(settings) / USAGE_FILE)

    def __repr__(self) -> str:
        return f"LlmClient(model={self._llm.model!r}, key_saved={self._api_key is not None})"

    # -- usage --

    def _today(self) -> date:
        return self._clock().astimezone(ZoneInfo(self._settings.schedule.timezone)).date()

    def used_today(self) -> LlmDayUsage:
        """Today's calls, tokens and cost (in ``schedule.timezone``)."""
        day = self._today().isoformat()
        return self._usage.load().get(day, LlmDayUsage(day=day))

    def budget_left(self) -> int:
        """Tokens left today under ``llm.daily_token_budget``."""
        return max(0, self._llm.daily_token_budget - self.used_today().budget_tokens)

    def _record(self, usage: LlmUsage) -> None:
        today = self._today()
        days = self._usage.load()
        day = today.isoformat()
        days[day] = days.get(day, LlmDayUsage(day=day)).plus(usage)
        try:
            self._usage.save(days, today)
        except OSError as error:
            log.warning("llm usage not saved", extra={"error": type(error).__name__})

    def _cost(self, input_tokens: int, output_tokens: int, read: int, write: int) -> float:
        llm = self._llm
        return (
            input_tokens * llm.input_usd_per_mtok
            + write * llm.input_usd_per_mtok * CACHE_WRITE_PRICE_FACTOR
            + read * llm.cache_read_usd_per_mtok
            + output_tokens * llm.output_usd_per_mtok
        ) / 1_000_000

    # -- the call --

    def _sdk(self) -> "anthropic.Anthropic":
        if self._sdk_client is None:
            self._sdk_client = anthropic.Anthropic(
                api_key=self._api_key,
                timeout=float(self._llm.timeout_seconds),
                max_retries=MAX_RETRIES,
                http_client=anthropic.DefaultHttpxClient(verify=_tls_context()),
            )
        return self._sdk_client

    def _check(self, data_class: DataClass, force_enabled: bool) -> None:
        """LlmUnavailable, in the documented order, before any network."""
        if data_class not in DATA_CLASSES:
            raise ValueError("unknown data class")
        if not (self._llm.enabled or force_enabled):
            raise LlmUnavailable("disabled")
        if data_class == "drexel" and not self._llm.allow_drexel_data:
            raise LlmUnavailable("gate")
        if self.used_today().budget_tokens >= self._llm.daily_token_budget:
            raise LlmUnavailable("budget")
        if self._api_key is None:
            raise LlmUnavailable("no_key")

    def classify[T: BaseModel](
        self,
        text: ScrubbedText,
        *,
        system: str,
        output: type[T],
        data_class: DataClass,
        purpose: str,
        ref: str = "",
        force_enabled: bool = False,
    ) -> LlmResult[T]:
        """One structured-output call on scrubbed text.

        ``purpose`` is a short code for the log ("follow_up", "llm_test"); ``ref`` is e.g. a
        ticket id. ``force_enabled`` skips only the ``llm.enabled`` check, for ``llm test``
        and the synthetic eval; the Drexel-data gate still applies.

        TypeError unless ``text`` is a ScrubbedText. LlmUnavailable when no call was made,
        LlmFailed when the call failed or its output did not parse.
        """
        if not isinstance(text, ScrubbedText):
            raise TypeError("classify() takes a ScrubbedText from scrub(), never raw text")
        self._check(data_class, force_enabled)
        model = self._llm.model
        context = {"purpose": purpose, "ref": ref, "data_class": data_class, "model": model}

        try:
            response = self._sdk().messages.create(
                model=model,
                max_tokens=self._llm.max_output_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": text.text}],
                output_config={
                    "effort": self._llm.effort,
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(output)},
                },
            )
        except anthropic.APITimeoutError as error:
            raise self._failed("timeout", context) from error
        except anthropic.APIConnectionError as error:
            raise self._failed("connection", context) from error
        except anthropic.RateLimitError as error:
            raise self._failed("rate_limited", context) from error
        except anthropic.APIStatusError as error:
            raise self._failed(f"http_{error.status_code}", context) from error

        usage = self._usage_of(response, model)
        status: str
        value: T | None = None
        if response.stop_reason == "refusal":
            status = "refused"
        else:
            value = self._parse(response, output)
            status = "ok" if value is not None else "bad_output"

        self._record(usage)
        log.info(
            "llm call",
            extra={
                **context,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cost_usd": round(usage.cost_usd, 6),
                "status": status,
                "stop_reason": response.stop_reason,
                "replaced": dict(text.replaced),
                "truncated": text.truncated,
            },
        )
        if status == "bad_output":
            raise LlmFailed("bad_output")
        return LlmResult(value, "refused" if status == "refused" else "ok", usage)

    def _usage_of(self, response: object, model: str) -> LlmUsage:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) or 0
        output_tokens = getattr(usage, "output_tokens", None) or 0
        read = getattr(usage, "cache_read_input_tokens", None) or 0
        write = getattr(usage, "cache_creation_input_tokens", None) or 0
        return LlmUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=read,
            cache_write_tokens=write,
            cost_usd=self._cost(input_tokens, output_tokens, read, write),
        )

    @staticmethod
    def _parse[T: BaseModel](response: object, output: type[T]) -> T | None:
        """The output as ``output``, or None if it is missing, cut off or does not validate."""
        if getattr(response, "stop_reason", None) == "max_tokens":
            return None
        for block in getattr(response, "content", None) or ():
            if getattr(block, "type", None) == "text":
                try:
                    return output.model_validate_json(block.text)
                except ValidationError:
                    return None
        return None

    @staticmethod
    def _failed(reason: str, context: dict[str, str]) -> LlmFailed:
        log.info("llm call failed", extra={**context, "reason": reason})
        return LlmFailed(reason)
