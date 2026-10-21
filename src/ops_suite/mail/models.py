"""The message shape every mail adapter produces."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MailMessage:
    id: str  # internet_message_id when present, else the adapter's own id
    conversation_id: str | None
    received_at: datetime  # timezone-aware, UTC
    sender: str  # lower-cased address only
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    is_html: bool
    has_attachments: bool
    headers: Mapping[str, str]  # keys lower-cased
    source_ref: str  # adapter-private handle; for the drop folder, the file name
