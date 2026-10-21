"""Post Adaptive Cards to a Teams Workflows webhook. Shared by every module; knows no TDX.

The webhook URL is a secret (anyone holding it can post to the chat): it lives only in
``keyring`` and never appears in config, logs, output or exception messages. Error messages
carry an HTTP status and a short reason code only, never the URL and never the response body,
which can echo the card. The card itself is never logged.

The request body is the shape a Workflows webhook accepted in a live test (as of 2026-10-16):
``{"type": "message", "attachments": [{"contentType": <adaptive>, "content": card}]}``, plus a
``"summary"`` that repeats the card's counts-only ``fallbackText``, for previews and banners
where Teams cannot render the card.

Two named destinations: ``personal``, the
person's own chat, and ``desk``, the desk's shared chat. Each has its own keyring entry; the
personal one keeps the name it always had, so nobody saves their webhook again.

Mentions follow Microsoft's documented Adaptive Card format for Teams ("Format
cards in Microsoft Teams", "User mention in Incoming Webhook with Adaptive Cards", checked
2026-10-19): an ``<at>Name</at>`` run in a TextBlock and, under the card's ``msteams``, an
``entities`` list of ``{"type": "mention", "text": "<at>Name</at>", "mentioned": {"id": <UPN>,
"name": Name}}``, where ``text`` matches the run exactly. Microsoft documents no "everyone"
mention for cards (only for a Graph message posted as a user), so ``everyone`` is a candidate
to test live in your own chat first (docs/USAGE.md, "Shift summaries").
"""

import copy
import http.client
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

import keyring
import truststore

from ops_suite.common.hints import RUN

log = logging.getLogger(__name__)

KEYRING_SERVICE = "ops-suite"
KEYRING_WEBHOOK_USER = "teams-webhook"
Destination = Literal["personal", "desk"]
MentionMode = Literal["none", "everyone", "people"]
DESTINATIONS: tuple[Destination, ...] = ("personal", "desk")
KEYRING_USERS: dict[Destination, str] = {
    "personal": KEYRING_WEBHOOK_USER,
    "desk": "teams-webhook-desk",
}
ADAPTIVE_CARD = "application/vnd.microsoft.card.adaptive"
CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
CARD_VERSION = "1.4"
RETRY_AFTER_CAP_SECONDS = 30
RETRY_DEFAULT_SECONDS = 5
TEST_CARD_FALLBACK = "ops-suite: test card"
# The undocumented "everyone" candidate: the documented entity shape with a fixed id. Kept only
# if a synthetic test in your own chat shows that it notifies the group chat.
EVERYONE_NAME = "Everyone"
EVERYONE_ID = "everyone"
SET_WEBHOOK_HINT = f'Teams: not sent; run "{RUN} teams set-webhook" first.'


class TeamsPostError(Exception):
    """The post failed. The message holds the status and a reason code only."""

    def __init__(self, status: int | None, reason: str) -> None:
        super().__init__(f"Teams post failed ({reason})")
        self.status = status
        self.reason = reason  # "http_400", "http_429", "timeout", "connection", ...


def check_webhook_url(url: str, host_suffixes: Sequence[str]) -> None:
    """ValueError unless ``url`` is https on an allowed host. The message never quotes it."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ValueError("not an https Power Automate address") from None
    if parts.scheme.lower() != "https" or not host:
        raise ValueError("not an https Power Automate address")
    suffixes = [suffix.lower() for suffix in host_suffixes]
    if not any(host.endswith(suffix) for suffix in suffixes):
        raise ValueError(
            f"not an https Power Automate address (the host must end with {' or '.join(suffixes)})"
        )


def set_webhook_command(destination: Destination = "personal") -> str:
    """The runnable command that saves ``destination``'s webhook."""
    command = f"{RUN} teams set-webhook"
    return command if destination == "personal" else f"{command} --to {destination}"


def get_webhook_url(destination: Destination = "personal") -> str | None:
    """The stored webhook URL of ``destination``, or None when nothing is stored."""
    return keyring.get_password(KEYRING_SERVICE, KEYRING_USERS[destination]) or None


def set_webhook_url(
    url: str, host_suffixes: Sequence[str], destination: Destination = "personal"
) -> None:
    """Validate, then store in the system keychain under ``destination``'s entry."""
    check_webhook_url(url, host_suffixes)
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERS[destination], url.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the card goes to the stored address or nowhere."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _tls_context() -> ssl.SSLContext:
    """Verify certificates against the operating system's trust store.

    Python builds without a CA bundle of their own (uv's managed interpreters on macOS, for
    one) otherwise fail every https post with CERTIFICATE_VERIFY_FAILED. The system store also
    holds any certificate authority the university adds. Verification stays fully on.
    """
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


_OPENER = urllib.request.build_opener(
    _NoRedirect, urllib.request.HTTPSHandler(context=_tls_context())
)


def _retry_delay(retry_after: str | None) -> float:
    try:
        seconds = float(retry_after) if retry_after is not None else RETRY_DEFAULT_SECONDS
    except ValueError:
        seconds = RETRY_DEFAULT_SECONDS  # the HTTP-date form
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_SECONDS)


def _post_once(body: bytes, url: str, timeout: float, attempt: int) -> tuple[int, str | None]:
    """(status, Retry-After) of one POST; TeamsPostError on a timeout or connection error."""
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            response.read()  # drained, never kept
            status, retry_after = response.status, None
    except urllib.error.HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After") if error.headers else None
        error.close()
    except urllib.error.URLError as error:
        reason = "timeout" if isinstance(error.reason, TimeoutError) else "connection"
        status = None
    except TimeoutError:
        reason, status = "timeout", None
    except (OSError, http.client.HTTPException):
        reason, status = "connection", None
    if status is None:
        # Raised outside the except blocks, so no chained exception carries the URL.
        log.info("teams post failed", extra={"reason": reason, "attempt": attempt})
        raise TeamsPostError(None, reason)
    log.info("teams post answered", extra={"status": status, "attempt": attempt})
    return status, retry_after


def post_card(
    url: str,
    card: Mapping[str, object],
    *,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """POST ``card`` in the verified body; the 2xx status, or TeamsPostError.

    The card's ``fallbackText``, if any, also goes into the envelope as ``summary``.
    429 and 5xx are retried once, after Retry-After (at most 30 s) or 5 s.
    """
    envelope: dict[str, object] = {"type": "message"}
    fallback = card.get("fallbackText")
    if isinstance(fallback, str) and fallback:
        envelope["summary"] = fallback
    envelope["attachments"] = [{"contentType": ADAPTIVE_CARD, "content": card}]
    body = json.dumps(envelope).encode("utf-8")
    status, retry_after = _post_once(body, url, timeout, 1)
    if status == 429 or status >= 500:
        sleep(_retry_delay(retry_after))
        status, _ = _post_once(body, url, timeout, 2)
    if not 200 <= status < 300:
        raise TeamsPostError(status, f"http_{status}")
    return status


def text_card(heading: str, text: str, *, fallback: str) -> dict[str, object]:
    """A plain card: a heading and one paragraph. For notices that carry no ticket data.

    ``fallback`` is the short line Teams shows where it cannot render the card (notification
    banners, the chat-list preview); keep it free of ticket data.
    """
    return {
        "type": "AdaptiveCard",
        "$schema": CARD_SCHEMA,
        "version": CARD_VERSION,
        "fallbackText": fallback,
        "body": [
            {"type": "TextBlock", "text": heading, "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": text, "wrap": True},
        ],
    }


def synthetic_test_card(now: datetime) -> dict[str, object]:
    """A fixed card with the send time, for ``ops-suite teams test``."""
    return text_card(
        "ops-suite test card (synthetic)",
        f"Sent {now:%a %Y-%m-%d %H:%M %Z}. No ticket or mail data.",
        fallback=TEST_CARD_FALLBACK,
    )


def mention_name(address: str) -> str:
    """The name a mention shows: the part of the sign-in address before the ``@``. Never a
    name read from TDX."""
    return address.strip().split("@", 1)[0]


def mention_entities(
    mode: MentionMode, people: Sequence[str]
) -> tuple[str, list[dict[str, object]]]:
    """(the TextBlock text with the ``<at>`` runs, the ``msteams.entities`` list) for ``mode``;
    ``("", [])`` for ``none``."""
    if mode == "none":
        return "", []
    if mode == "everyone":
        pairs = [(EVERYONE_ID, EVERYONE_NAME)]
    else:
        pairs = [(address.strip(), mention_name(address)) for address in people]
    entities: list[dict[str, object]] = [
        {"type": "mention", "text": f"<at>{name}</at>", "mentioned": {"id": ident, "name": name}}
        for ident, name in pairs
    ]
    return " ".join(str(entity["text"]) for entity in entities), entities


def with_mentions(
    card: Mapping[str, object], mode: MentionMode, people: Sequence[str]
) -> dict[str, object]:
    """A copy of ``card`` with one more TextBlock holding the mentions, and their entities in
    ``msteams.entities`` (other ``msteams`` keys kept). ``none`` returns a plain copy."""
    result = copy.deepcopy(dict(card))
    text, entities = mention_entities(mode, people)
    if not entities:
        return result
    body = list(result.get("body", []))  # type: ignore[call-overload]
    body.append({"type": "TextBlock", "text": text, "wrap": True, "spacing": "Medium"})
    result["body"] = body
    msteams = dict(result.get("msteams", {}))  # type: ignore[call-overload]
    msteams["entities"] = entities
    result["msteams"] = msteams
    return result
