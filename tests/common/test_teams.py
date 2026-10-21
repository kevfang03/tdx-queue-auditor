import json
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from ops_suite.common import teams
from ops_suite.common.teams import (
    KEYRING_SERVICE,
    KEYRING_WEBHOOK_USER,
    TeamsPostError,
    get_webhook_url,
    mention_entities,
    mention_name,
    post_card,
    set_webhook_url,
    synthetic_test_card,
    text_card,
    with_mentions,
)

SUFFIXES = [".logic.azure.com", ".api.powerplatform.com"]
SECRET = "SIGSECRET0123456789"
ECHO = "ECHOED-CARD-BODY"
HANG_UP = 0  # a scripted reply that closes the connection without answering
CARD = {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": "x"}]}


@dataclass
class Reply:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


class FakeWorkflows:
    """A local http.server that answers each POST with the next scripted reply."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.bodies: list[dict] = []
        self.content_types: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                fake.bodies.append(json.loads(self.rfile.read(length)))
                fake.content_types.append(self.headers.get("Content-Type", ""))
                reply = fake.replies.pop(0)
                if reply.status == HANG_UP:
                    self.close_connection = True
                    return
                if reply.delay:
                    time.sleep(reply.delay)
                payload = f'{{"error": "{ECHO}"}}'.encode()
                try:
                    self.send_response(reply.status)
                    for name, value in reply.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up (timeout test)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.url = (
            f"http://127.0.0.1:{port}/workflows/abc/triggers/manual/paths/invoke?sig={SECRET}"
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def workflows() -> Iterator[list[FakeWorkflows]]:
    started: list[FakeWorkflows] = []
    yield started
    for server in started:
        server.close()


def serve(workflows: list[FakeWorkflows], *replies: Reply) -> FakeWorkflows:
    server = FakeWorkflows(list(replies))
    workflows.append(server)
    return server


def assert_nothing_leaks(
    server: FakeWorkflows, caplog: pytest.LogCaptureFixture, *errors: BaseException
) -> None:
    texts = [str(error) for error in errors] + [repr(error) for error in errors]
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.append(json.dumps(record.__dict__, default=str))
    for text in texts:
        assert SECRET not in text
        assert server.url not in text
        assert ECHO not in text


def test_202_succeeds_with_the_verified_body(
    workflows: list[FakeWorkflows], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = serve(workflows, Reply(202))

    assert post_card(server.url, CARD, timeout=5) == 202

    assert server.bodies == [
        {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": CARD}
            ],
        }
    ]
    assert server.content_types == ["application/json"]
    assert_nothing_leaks(server, caplog)


def test_the_fallback_text_also_goes_into_the_envelope_summary(
    workflows: list[FakeWorkflows],
) -> None:
    server = serve(workflows, Reply(202))
    card = text_card("Heading", "Body text", fallback="ops-suite: TDX auditor notice")

    post_card(server.url, card, timeout=5)

    assert server.bodies == [
        {
            "type": "message",
            "summary": "ops-suite: TDX auditor notice",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        }
    ]
    assert card["fallbackText"] == "ops-suite: TDX auditor notice"


def test_400_raises_http_400(
    workflows: list[FakeWorkflows], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = serve(workflows, Reply(400))

    with pytest.raises(TeamsPostError) as error_info:
        post_card(server.url, CARD, timeout=5)

    error = error_info.value
    assert (error.status, error.reason) == (400, "http_400")
    assert "http_400" in str(error)
    assert error.__cause__ is None and error.__context__ is None
    assert len(server.bodies) == 1  # no retry
    assert_nothing_leaks(server, caplog, error)


def test_429_then_202_succeeds_after_one_retry(
    workflows: list[FakeWorkflows], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = serve(workflows, Reply(429, {"Retry-After": "120"}), Reply(202))
    slept: list[float] = []

    assert post_card(server.url, CARD, timeout=5, sleep=slept.append) == 202

    assert slept == [30]  # Retry-After capped at 30 s
    assert len(server.bodies) == 2
    assert_nothing_leaks(server, caplog)


def test_500_twice_raises_after_one_retry(workflows: list[FakeWorkflows]) -> None:
    server = serve(workflows, Reply(503), Reply(502))
    slept: list[float] = []

    with pytest.raises(TeamsPostError) as error_info:
        post_card(server.url, CARD, timeout=5, sleep=slept.append)

    assert error_info.value.reason == "http_502"
    assert slept == [5]  # no Retry-After: 5 s
    assert len(server.bodies) == 2


def test_a_redirect_is_not_followed(workflows: list[FakeWorkflows]) -> None:
    server = serve(workflows, Reply(307, {"Location": "http://127.0.0.1:9/elsewhere"}))

    with pytest.raises(TeamsPostError) as error_info:
        post_card(server.url, CARD, timeout=5)

    assert error_info.value.reason == "http_307"


def test_a_timeout_raises_timeout(
    workflows: list[FakeWorkflows], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = serve(workflows, Reply(202, delay=2))

    with pytest.raises(TeamsPostError) as error_info:
        post_card(server.url, CARD, timeout=0.3)

    error = error_info.value
    assert (error.status, error.reason) == (None, "timeout")
    assert error.__cause__ is None and error.__context__ is None
    assert_nothing_leaks(server, caplog, error)


def test_a_dropped_connection_raises_connection(
    workflows: list[FakeWorkflows], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = serve(workflows, Reply(HANG_UP))

    with pytest.raises(TeamsPostError) as error_info:
        post_card(server.url, CARD, timeout=5)

    assert (error_info.value.status, error_info.value.reason) == (None, "connection")
    assert_nothing_leaks(server, caplog, error_info.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://prod-01.westus.logic.azure.com/workflows/abc?sig={SECRET}",
        f"https://example.com/workflows/abc?sig={SECRET}",
        f"https://logic.azure.com.example.com/workflows?sig={SECRET}",
        f"ftp://x.logic.azure.com/?sig={SECRET}",
        f"not a url {SECRET}",
        "",
    ],
)
def test_set_webhook_url_rejects_bad_addresses(url: str, memory_keyring) -> None:
    with pytest.raises(ValueError) as error_info:
        set_webhook_url(url, SUFFIXES)

    assert "not an https Power Automate address" in str(error_info.value)
    assert SECRET not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert memory_keyring.passwords == {}


@pytest.mark.parametrize(
    "url",
    [
        f"https://prod-01.westus.logic.azure.com:443/workflows/abc/invoke?sig={SECRET}",
        f"https://default1234.ab.environment.API.PowerPlatform.com/powerautomate/x?sig={SECRET}",
    ],
)
def test_set_webhook_url_stores_in_the_keyring(url: str, memory_keyring) -> None:
    assert get_webhook_url() is None

    set_webhook_url(f"  {url}\n", SUFFIXES)

    assert memory_keyring.passwords == {(KEYRING_SERVICE, KEYRING_WEBHOOK_USER): url}
    assert get_webhook_url() == url


def test_keyring_names_are_fixed() -> None:
    assert (teams.KEYRING_SERVICE, teams.KEYRING_WEBHOOK_USER) == ("ops-suite", "teams-webhook")
    # The personal entry keeps the name it had before destinations, so nobody saves it again.
    assert teams.KEYRING_USERS == {"personal": "teams-webhook", "desk": "teams-webhook-desk"}


def test_each_destination_has_its_own_keyring_entry(memory_keyring) -> None:
    personal = f"https://prod-01.westus.logic.azure.com/workflows/p?sig={SECRET}"
    desk = f"https://prod-02.westus.logic.azure.com/workflows/d?sig={SECRET}"

    set_webhook_url(desk, SUFFIXES, "desk")
    assert get_webhook_url() is None
    assert get_webhook_url("personal") is None
    assert get_webhook_url("desk") == desk

    set_webhook_url(personal, SUFFIXES)
    assert memory_keyring.passwords == {
        (KEYRING_SERVICE, "teams-webhook"): personal,
        (KEYRING_SERVICE, "teams-webhook-desk"): desk,
    }
    assert get_webhook_url() == personal
    assert get_webhook_url("desk") == desk


def test_a_bad_desk_address_is_not_saved(memory_keyring) -> None:
    with pytest.raises(ValueError):
        set_webhook_url(f"https://example.com/x?sig={SECRET}", SUFFIXES, "desk")
    assert memory_keyring.passwords == {}


def test_set_webhook_command_names_the_destination() -> None:
    assert teams.set_webhook_command() == "uv run ops-suite teams set-webhook"
    assert teams.set_webhook_command("desk") == "uv run ops-suite teams set-webhook --to desk"


def test_synthetic_test_card_is_fixed_and_dated() -> None:
    now = datetime(2026, 10, 16, 14, 5, tzinfo=ZoneInfo("America/New_York"))
    card = synthetic_test_card(now)

    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    texts = [block["text"] for block in card["body"]]
    assert texts[0] == "ops-suite test card (synthetic)"
    assert "Fri 2026-10-16 14:05" in texts[1]
    assert card["fallbackText"] == "ops-suite: test card"


def test_https_posts_verify_against_the_system_trust_store() -> None:
    handlers = [
        h for h in teams._OPENER.handlers if isinstance(h, teams.urllib.request.HTTPSHandler)
    ]

    assert len(handlers) == 1
    context = handlers[0]._context
    assert isinstance(context, teams.truststore.SSLContext)
    assert context.verify_mode == teams.ssl.CERT_REQUIRED
    assert context.check_hostname is True


# --- mentions ----------------------------------------------------------------------------


def test_people_mentions_follow_the_documented_entity_shape() -> None:
    card = with_mentions(
        {"type": "AdaptiveCard", "body": [], "msteams": {"width": "Full"}},
        "people",
        ["mf123@example.edu", "ab45@example.edu"],
    )

    assert card["body"][-1]["text"] == "<at>mf123</at> <at>ab45</at>"
    assert card["msteams"] == {
        "width": "Full",
        "entities": [
            {
                "type": "mention",
                "text": "<at>mf123</at>",
                "mentioned": {"id": "mf123@example.edu", "name": "mf123"},
            },
            {
                "type": "mention",
                "text": "<at>ab45</at>",
                "mentioned": {"id": "ab45@example.edu", "name": "ab45"},
            },
        ],
    }
    # Every entity's text appears in the card body exactly as written.
    body_text = " ".join(block["text"] for block in card["body"])
    assert all(entity["text"] in body_text for entity in card["msteams"]["entities"])


def test_the_everyone_candidate_uses_the_same_shape() -> None:
    text, entities = mention_entities("everyone", ["ignored@example.edu"])
    assert text == "<at>Everyone</at>"
    assert entities == [
        {
            "type": "mention",
            "text": "<at>Everyone</at>",
            "mentioned": {"id": "everyone", "name": "Everyone"},
        }
    ]


def test_no_mention_leaves_the_card_as_it_was() -> None:
    original = synthetic_test_card(datetime(2026, 10, 19, 7, 30, tzinfo=ZoneInfo("UTC")))
    card = with_mentions(original, "none", ["mf123@example.edu"])
    assert card == original and card is not original
    assert "msteams" not in card


def test_with_mentions_does_not_change_the_card_it_was_given() -> None:
    original = {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "x"}]}
    with_mentions(original, "everyone", [])
    assert original == {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "x"}]}


def test_the_mention_name_is_the_address_before_the_at() -> None:
    assert mention_name(" Jo.Q-1@example.edu ") == "Jo.Q-1"
