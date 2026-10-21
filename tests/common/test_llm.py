"""The LLM client, against a fake SDK client: no test here makes a network call."""

import json
import logging
import ssl
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import anthropic
import httpx2
import pytest
import truststore
from pydantic import BaseModel

from ops_suite.common import llm as llm_module
from ops_suite.common import paths
from ops_suite.common.config import Settings
from ops_suite.common.llm import (
    LlmClient,
    LlmDayUsage,
    LlmFailed,
    LlmUnavailable,
    get_api_key,
    set_api_key,
)
from ops_suite.common.scrub import scrub

SENTENCE = "The synthetic printer on floor two is jammed again, please advise."
SYSTEM = "You sort synthetic desk messages. Answer with the verdict field only."
KEY = "sk-ant-test-0000"
NOON_UTC = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)  # 12:00 in New York


class Verdict(BaseModel):
    verdict: Literal["expects_reply", "no_reply_needed"]


def response(
    text: str = '{"verdict": "expects_reply"}',
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read: int | None = 0,
    cache_write: int | None = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


class FakeMessages:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0) if self.results else response()
        if isinstance(result, BaseException):
            raise result
        return result


class FakeSdk:
    def __init__(self, *results: object) -> None:
        self.messages = FakeMessages(*results)


class Clock:
    def __init__(self, now: datetime = NOON_UTC) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_settings(**llm: object) -> Settings:
    return Settings(llm={"enabled": True, **llm})


def make_client(
    settings: Settings | None = None,
    *results: object,
    api_key: str | None = KEY,
    clock: Clock | None = None,
) -> tuple[LlmClient, FakeSdk]:
    sdk = FakeSdk(*results)
    client = LlmClient(
        settings or make_settings(), api_key=api_key, sdk_client=sdk, clock=clock or Clock()
    )
    return client, sdk


def classify(client: LlmClient, data_class: str = "synthetic", **options: object):
    return client.classify(
        scrub(SENTENCE),
        system=SYSTEM,
        output=Verdict,
        data_class=data_class,  # type: ignore[arg-type]
        purpose="test",
        ref="5501001",
        **options,  # type: ignore[arg-type]
    )


def usage_file(settings: Settings) -> Path:
    return paths.llm_dir(settings) / "llm-usage.json"


# --- the request ---------------------------------------------------------------------------


def test_ok_call_returns_the_parsed_value_and_usage():
    client, sdk = make_client()
    result = classify(client)
    assert result.status == "ok"
    assert result.value == Verdict(verdict="expects_reply")
    assert result.usage.model == "claude-haiku-5-5"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 10)
    assert len(sdk.messages.calls) == 1


def test_request_shape():
    client, sdk = make_client()
    classify(client)
    call = sdk.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == 256
    assert call["system"] == [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    assert call["messages"] == [{"role": "user", "content": scrub(SENTENCE).text}]
    output_config = call["output_config"]
    assert output_config["effort"] == "low"
    assert output_config["format"]["type"] == "json_schema"
    schema = output_config["format"]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["expects_reply", "no_reply_needed"]
    assert schema["additionalProperties"] is False
    for sampling in ("temperature", "top_p", "top_k", "thinking"):
        assert sampling not in call


def test_effort_comes_from_config():
    client, sdk = make_client(make_settings(effort="medium"))
    classify(client)
    assert sdk.messages.calls[0]["output_config"]["effort"] == "medium"


def test_plain_string_is_a_type_error():
    client, sdk = make_client()
    with pytest.raises(TypeError):
        client.classify(
            SENTENCE,  # type: ignore[arg-type]
            system=SYSTEM,
            output=Verdict,
            data_class="synthetic",
            purpose="test",
        )
    assert sdk.messages.calls == []


def test_unknown_data_class_is_refused():
    client, sdk = make_client()
    with pytest.raises(ValueError):
        classify(client, data_class="public")
    assert sdk.messages.calls == []


# --- checks before any network --------------------------------------------------------------


def unavailable(client: LlmClient, sdk: FakeSdk, data_class: str = "synthetic", **options):
    with pytest.raises(LlmUnavailable) as caught:
        classify(client, data_class, **options)
    assert sdk.messages.calls == []
    return caught.value.reason


def test_disabled_by_default():
    client, sdk = make_client(Settings())
    assert unavailable(client, sdk) == "disabled"


def test_force_enabled_skips_only_the_enabled_check():
    client, sdk = make_client(Settings())
    assert classify(client, force_enabled=True).status == "ok"
    client, sdk = make_client(Settings())
    assert unavailable(client, sdk, "institutional", force_enabled=True) == "gate"


def test_institutional_needs_the_gate():
    client, sdk = make_client(make_settings())
    assert unavailable(client, sdk, "institutional") == "gate"
    client, sdk = make_client(make_settings(allow_institutional_data=True))
    assert classify(client, "institutional").status == "ok"


@pytest.mark.parametrize("data_class", ["synthetic", "personal"])
def test_synthetic_and_personal_need_only_enabled(data_class: str):
    client, sdk = make_client(make_settings(allow_institutional_data=False))
    assert classify(client, data_class).status == "ok"


def test_disabled_comes_before_the_gate():
    client, sdk = make_client(Settings())
    assert unavailable(client, sdk, "institutional") == "disabled"


def test_no_key():
    client, sdk = make_client(api_key="")
    assert unavailable(client, sdk) == "no_key"


def test_no_key_reads_the_keyring(memory_keyring):
    settings = make_settings()
    client = LlmClient(settings, sdk_client=FakeSdk(), clock=Clock())
    with pytest.raises(LlmUnavailable) as caught:
        classify(client)
    assert caught.value.reason == "no_key"
    set_api_key(KEY)
    assert classify(LlmClient(settings, sdk_client=FakeSdk(), clock=Clock())).status == "ok"


def test_budget_comes_before_the_key():
    settings = make_settings(daily_token_budget=1_000)
    client, sdk = make_client(settings, response(input_tokens=990, output_tokens=10))
    classify(client)
    client, sdk = make_client(settings, api_key="")
    assert unavailable(client, sdk) == "budget"


# --- the budget -----------------------------------------------------------------------------


def test_budget_counts_cache_reads_at_ten_percent_rounded_up():
    settings = make_settings(daily_token_budget=1_000)
    client, sdk = make_client(
        settings, response(input_tokens=500, output_tokens=40, cache_read=1001, cache_write=300)
    )
    classify(client)
    used = client.used_today()
    assert used.calls == 1
    assert used.cache_read_tokens == 1001
    assert used.budget_tokens == 500 + 40 + 300 + 101
    assert client.budget_left() == 1_000 - 941


def test_budget_hard_stops_once_reached():
    settings = make_settings(daily_token_budget=1_000)
    client, sdk = make_client(
        settings, response(input_tokens=600, output_tokens=0), response(input_tokens=400)
    )
    classify(client)
    classify(client)  # 600 used, under the budget: this call is made and goes over
    assert client.budget_left() == 0
    with pytest.raises(LlmUnavailable) as caught:
        classify(client)
    assert caught.value.reason == "budget"
    assert len(sdk.messages.calls) == 2


def test_missing_usage_counts_as_zero():
    client, sdk = make_client(None, response(cache_read=None, cache_write=None))
    result = classify(client)
    assert (result.usage.cache_read_tokens, result.usage.cache_write_tokens) == (0, 0)


def test_day_rolls_over_in_the_schedule_timezone():
    clock = Clock(datetime(2026, 10, 17, 3, 30, tzinfo=UTC))  # 23:30 on the 16th in New York
    client, sdk = make_client(None, clock=clock)
    classify(client)
    assert client.used_today().day == "2026-10-16"
    assert client.used_today().calls == 1
    clock.now = datetime(2026, 10, 17, 4, 30, tzinfo=UTC)  # 00:30 on the 17th
    assert client.used_today() == LlmDayUsage(day="2026-10-17")
    classify(client)
    days = json.loads(usage_file(client._settings).read_text(encoding="utf-8"))["days"]
    assert days["2026-10-16"]["calls"] == 1 and days["2026-10-17"]["calls"] == 1


def test_usage_keeps_31_days():
    settings = make_settings()
    path = usage_file(settings)
    path.parent.mkdir(parents=True)
    today = date(2026, 10, 16)
    record = {
        "calls": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
    }
    old = {(today - timedelta(days=n)).isoformat(): record for n in range(1, 41)}
    path.write_text(json.dumps({"schema": 1, "days": old}), encoding="utf-8")
    client, sdk = make_client(settings)
    classify(client)
    days = json.loads(path.read_text(encoding="utf-8"))["days"]
    assert len(days) == 31
    assert min(days) == (today - timedelta(days=30)).isoformat()
    assert max(days) == today.isoformat()
    assert not path.with_name(path.name + ".tmp").exists()


def test_cost_estimate():
    client, sdk = make_client(
        None,
        response(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read=1_000_000,
            cache_write=1_000_000,
        ),
    )
    result = classify(client)
    assert result.usage.cost_usd == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)


@pytest.mark.parametrize("content", ["{not json", '{"schema": 2, "days": {}}', '{"days": []}'])
def test_corrupt_usage_file_starts_fresh(content: str, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger="ops_suite")
    settings = make_settings()
    path = usage_file(settings)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    client, sdk = make_client(settings)
    assert client.used_today().calls == 0
    assert any(record.getMessage() == "llm_usage_reset" for record in caplog.records)
    classify(client)
    assert json.loads(path.read_text(encoding="utf-8"))["days"]["2026-10-16"]["calls"] == 1


# --- results --------------------------------------------------------------------------------


def test_refusal():
    client, sdk = make_client(None, response("", stop_reason="refusal", output_tokens=3))
    result = classify(client)
    assert result.status == "refused"
    assert result.value is None
    assert client.used_today().calls == 1


@pytest.mark.parametrize(
    "bad",
    [
        response("not json"),
        response('{"verdict": "maybe"}'),
        response('{"verdict": "expects_reply"}', stop_reason="max_tokens"),
        SimpleNamespace(stop_reason="end_turn", content=[], usage=response().usage),
    ],
)
def test_bad_output_is_recorded_then_raised(bad: SimpleNamespace):
    client, sdk = make_client(None, bad)
    with pytest.raises(LlmFailed) as caught:
        classify(client)
    assert caught.value.reason == "bad_output"
    assert client.used_today().calls == 1


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=_REQUEST)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (anthropic.APITimeoutError(request=_REQUEST), "timeout"),
        (anthropic.APIConnectionError(request=_REQUEST), "connection"),
        (anthropic.RateLimitError("slow down", response=_status(429), body=None), "rate_limited"),
        (anthropic.AuthenticationError("no", response=_status(401), body=None), "http_401"),
        (anthropic.APIStatusError("down", response=_status(529), body=None), "http_529"),
        (anthropic.BadRequestError("bad", response=_status(400), body=None), "http_400"),
    ],
)
def test_sdk_errors_become_reason_codes(error: Exception, reason: str):
    client, sdk = make_client(None, error)
    with pytest.raises(LlmFailed) as caught:
        classify(client)
    assert caught.value.reason == reason
    assert caught.value.__cause__ is error
    assert str(caught.value) == f"LLM call failed ({reason})"
    assert client.used_today().calls == 0


# --- logging --------------------------------------------------------------------------------


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        json.dumps({**record.__dict__, "message": record.getMessage()}, default=str)
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "result",
    [
        response(),
        response("", stop_reason="refusal"),
        response("not json"),
        anthropic.APIConnectionError(request=_REQUEST),
    ],
)
def test_logs_hold_counts_never_text(result: object, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger="ops_suite")
    client, sdk = make_client(None, result)
    try:
        classify(client)
    except LlmFailed:
        pass
    logged = _logged(caplog)
    assert SENTENCE not in logged and "printer" not in logged
    assert SYSTEM not in logged and "verdict field" not in logged
    assert "expects_reply" not in logged
    assert KEY not in logged


def test_the_call_log_line(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger="ops_suite")
    client, sdk = make_client(None, response(cache_read=7, cache_write=5))
    client.classify(
        scrub("Hi Sam,\nmail helper@example.edu", known_names=["Sam Sample"]),
        system=SYSTEM,
        output=Verdict,
        data_class="synthetic",
        purpose="follow_up",
        ref="5501001",
    )
    (record,) = [r for r in caplog.records if r.getMessage() == "llm call"]
    assert record.purpose == "follow_up" and record.ref == "5501001"
    assert record.data_class == "synthetic" and record.model == "claude-haiku-5-5"
    assert (record.input_tokens, record.output_tokens) == (100, 10)
    assert (record.cache_read_tokens, record.cache_write_tokens) == (7, 5)
    assert record.status == "ok"
    assert record.replaced["name"] == 1 and record.replaced["email"] == 1
    assert record.cost_usd > 0


# --- the SDK client and the key ------------------------------------------------------------


def test_real_sdk_client_uses_a_truststore_context(monkeypatch: pytest.MonkeyPatch):
    built: dict[str, dict[str, object]] = {}
    fake = FakeSdk()

    def http_client(**kwargs: object) -> str:
        built["http"] = kwargs
        return "http-client"

    def sdk(**kwargs: object) -> FakeSdk:
        built["sdk"] = kwargs
        return fake

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", http_client)
    monkeypatch.setattr(anthropic, "Anthropic", sdk)
    client = LlmClient(make_settings(timeout_seconds=12), api_key=KEY, clock=Clock())
    assert built == {}  # nothing is built before a call passes the checks
    classify(client)
    verify = built["http"]["verify"]
    assert isinstance(verify, truststore.SSLContext)
    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_REQUIRED and verify.check_hostname is True
    assert built["sdk"]["http_client"] == "http-client"
    assert built["sdk"]["timeout"] == 12
    assert built["sdk"]["max_retries"] == 2
    assert built["sdk"]["api_key"] == KEY
    assert len(fake.messages.calls) == 1


def test_real_sdk_client_builds_offline():
    # Constructing the real client makes no request; only messages.create would.
    client = LlmClient(make_settings(), api_key=KEY, clock=Clock())
    sdk = client._sdk()
    assert isinstance(sdk, anthropic.Anthropic)
    assert sdk.max_retries == 2


def test_set_and_get_api_key(memory_keyring):
    assert get_api_key() is None
    set_api_key(f"  {KEY}\n")
    assert get_api_key() == KEY
    assert memory_keyring.passwords[("ops-suite", "anthropic-api-key")] == KEY


def test_set_api_key_checks_the_prefix_and_never_quotes_the_key():
    with pytest.raises(ValueError) as caught:
        set_api_key("not-a-key-12345")
    assert "not-a-key-12345" not in str(caught.value)
    assert get_api_key() is None


def test_repr_never_shows_the_key():
    client, sdk = make_client()
    assert KEY not in repr(client)
    assert llm_module.KEYRING_API_KEY_USER == "anthropic-api-key"


# --- settings -------------------------------------------------------------------------------


def test_llm_settings_default_to_off():
    llm = Settings().llm
    assert llm.enabled is False and llm.allow_institutional_data is False
    assert (llm.model, llm.effort, llm.max_output_tokens) == ("claude-haiku-5-5", "low", 256)
    assert llm.daily_token_budget == 100_000


def test_llm_settings_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPS_SUITE_LLM__ENABLED", "true")
    monkeypatch.setenv("OPS_SUITE_LLM__DAILY_TOKEN_BUDGET", "5000")
    from ops_suite.common.config import load_settings

    llm = load_settings().llm
    assert llm.enabled is True and llm.daily_token_budget == 5000
    assert llm.allow_institutional_data is False
