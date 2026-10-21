"""The llm commands: set-key, status and test. No test here makes a network call."""

from types import SimpleNamespace

import keyring.errors
import pytest

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common import llm as llm_module
from ops_suite.common.llm import get_api_key, set_api_key

KEY = "sk-ant-test-0000"


class FakeMessages:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def reply(text: str = '{"ok": true}', stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=42,
            output_tokens=7,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch):
    """Replace the SDK client the LLM client would build; returns its messages fake."""
    holder: dict[str, FakeMessages] = {}

    def install(result: object) -> FakeMessages:
        messages = FakeMessages(result)
        holder["messages"] = messages
        monkeypatch.setattr(
            llm_module.LlmClient, "_sdk", lambda self: SimpleNamespace(messages=messages)
        )
        return messages

    return install


# --- set-key --------------------------------------------------------------------------------


def test_set_key_saves_a_hidden_key(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: KEY)
    assert main(["llm", "set-key"]) == 0
    assert get_api_key() == KEY
    out, err = capsys.readouterr()
    assert KEY not in out + err
    assert "API key saved" in out


def test_set_key_refuses_a_bad_key(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "not-a-key-9999")
    assert main(["llm", "set-key"]) == 2
    assert get_api_key() is None
    out, err = capsys.readouterr()
    assert "not-a-key-9999" not in out + err
    assert "sk-ant-" in err


def test_set_key_asks_before_replacing(monkeypatch: pytest.MonkeyPatch, capsys):
    set_api_key(KEY)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: pytest.fail("asked for a key"))
    assert main(["llm", "set-key"]) == 0
    assert get_api_key() == KEY
    assert "Kept the saved API key." in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "sk-ant-test-1111")
    assert main(["llm", "set-key"]) == 0
    assert get_api_key() == "sk-ant-test-1111"


def test_set_key_keychain_failure_exits_2(monkeypatch: pytest.MonkeyPatch, capsys):
    def broken(*args: object) -> None:
        raise keyring.errors.PasswordSetError("locked")

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: KEY)
    monkeypatch.setattr(llm_module.keyring, "set_password", broken)
    assert main(["llm", "set-key"]) == 2
    assert "PasswordSetError" in capsys.readouterr().err


# --- status ---------------------------------------------------------------------------------


def test_status_without_a_key(fake_sdk, capsys):
    messages = fake_sdk(reply())
    assert main(["llm", "status"]) == 0
    out = capsys.readouterr().out
    assert out == (
        "LLM: enabled no; Drexel data allowed no; model claude-haiku-5-5; key saved no.\n"
        "Today: 0 calls, 0 of 100,000 tokens, ~$0.0000.\n"
    )
    assert messages.calls == []


def test_status_with_a_key_and_a_call_today(fake_sdk, monkeypatch: pytest.MonkeyPatch, capsys):
    set_api_key(KEY)
    fake_sdk(reply())
    assert main(["llm", "test"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("OPS_SUITE_LLM__ENABLED", "true")
    assert main(["llm", "status"]) == 0
    out = capsys.readouterr().out
    assert "LLM: enabled yes; Drexel data allowed no;" in out
    assert "key saved yes." in out
    assert "Today: 1 call, 49 of 100,000 tokens, ~$0.0000." in out
    assert KEY not in out


def test_status_keychain_failure_exits_2(monkeypatch: pytest.MonkeyPatch, capsys):
    def broken(*args: object) -> None:
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(llm_module.keyring, "get_password", broken)
    assert main(["llm", "status"]) == 2
    assert "keychain could not be read" in capsys.readouterr().err


# --- test -----------------------------------------------------------------------------------


def test_test_without_a_key_prints_the_hint_and_exits_2(fake_sdk, capsys):
    messages = fake_sdk(reply())
    assert main(["llm", "test"]) == 2
    err = capsys.readouterr().err
    assert "uv run ops-suite llm set-key" in err
    assert messages.calls == []


def test_test_ok_with_llm_disabled(fake_sdk, capsys):
    set_api_key(KEY)
    messages = fake_sdk(reply())
    assert main(["llm", "test"]) == 0
    assert capsys.readouterr().out == "LLM test: ok (in 42, out 7 tokens, ~$0.0000).\n"
    (call,) = messages.calls
    assert call["messages"] == [{"role": "user", "content": cli.LLM_TEST_SENTENCE}]
    assert call["output_config"]["format"]["schema"]["properties"] == {
        "ok": {"type": "boolean", "title": "Ok"}
    }


def test_test_refusal_exits_7(fake_sdk, capsys):
    set_api_key(KEY)
    fake_sdk(reply("", stop_reason="refusal"))
    assert main(["llm", "test"]) == 7
    assert capsys.readouterr().err == "LLM test: failed (refused).\n"


def test_test_bad_output_exits_7(fake_sdk, capsys):
    set_api_key(KEY)
    fake_sdk(reply("not json"))
    assert main(["llm", "test"]) == 7
    assert capsys.readouterr().err == "LLM test: failed (bad_output).\n"


def test_test_connection_failure_exits_7(fake_sdk, capsys):
    import anthropic
    import httpx2

    set_api_key(KEY)
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_sdk(anthropic.APIConnectionError(request=request))
    assert main(["llm", "test"]) == 7
    assert capsys.readouterr().err == "LLM test: failed (connection).\n"


def test_test_over_budget_exits_7(fake_sdk, monkeypatch: pytest.MonkeyPatch, capsys):
    set_api_key(KEY)
    monkeypatch.setenv("OPS_SUITE_LLM__DAILY_TOKEN_BUDGET", "1000")
    big = reply()
    big.usage.input_tokens = 1000
    fake_sdk(big)
    assert main(["llm", "test"]) == 0
    capsys.readouterr()
    assert main(["llm", "test"]) == 7
    assert capsys.readouterr().err == "LLM test: failed (budget).\n"


def test_llm_help_lists_the_commands(capsys):
    with pytest.raises(SystemExit):
        main(["llm", "--help"])
    out = capsys.readouterr().out
    for name in ("set-key", "status", "test"):
        assert name in out
