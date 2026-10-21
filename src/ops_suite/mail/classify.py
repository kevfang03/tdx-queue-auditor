"""Fixed rules that decide whether a person sent a message. No LLM, no keyword test."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops_suite.mail.models import MailMessage

if TYPE_CHECKING:
    from ops_suite.common.config import WorkMailSettings

_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})


def _normalised(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    # A blank entry would match every sender, so blanks are dropped.
    return tuple(value.strip().lower() for value in values if value.strip())


@dataclass(frozen=True, slots=True)
class MailRules:
    mailbox_address: str
    automated_senders: tuple[str, ...]
    automated_local_parts: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: WorkMailSettings) -> MailRules:
        return cls(
            mailbox_address=settings.mailbox_address,
            automated_senders=tuple(settings.automated_senders),
            automated_local_parts=tuple(settings.automated_local_parts),
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    flagged: bool
    reason: str


def classify(message: MailMessage, rules: MailRules) -> Verdict:
    """Apply the rules in order; the first match wins. Comparisons ignore case."""
    sender = message.sender.strip().lower()
    local_part, _, domain = sender.partition("@")
    headers = {name.lower(): value.strip().lower() for name, value in message.headers.items()}

    if sender == rules.mailbox_address.strip().lower():
        return Verdict(False, "own_mailbox")

    for entry in _normalised(rules.automated_senders):
        if sender == entry or (entry.startswith("@") and domain == entry[1:]):
            return Verdict(False, "automated_sender")

    if local_part.startswith(_normalised(rules.automated_local_parts)):
        return Verdict(False, "automated_local_part")

    if headers.get("auto-submitted", "no") != "no":
        return Verdict(False, "auto_submitted")

    if headers.get("precedence") in _BULK_PRECEDENCE:
        return Verdict(False, "bulk_precedence")

    if "list-unsubscribe" in headers:
        return Verdict(False, "list_mail")

    return Verdict(True, "person")
