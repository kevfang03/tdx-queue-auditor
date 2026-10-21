"""The interface a mail adapter implements."""

from collections.abc import Iterator
from typing import Protocol

from ops_suite.mail.models import MailMessage


class MailSource(Protocol):
    rejected: int  # inputs the adapter could not turn into a message

    def fetch(self) -> Iterator[MailMessage]: ...

    def acknowledge(self, message: MailMessage) -> None:
        """Mark a message as handled so it is not fetched again."""
        ...
