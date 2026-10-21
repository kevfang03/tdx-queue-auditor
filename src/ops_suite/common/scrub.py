"""The scrubber: the only way to turn raw text into text the LLM client accepts.

Every LLM call takes a ``ScrubbedText``, and only ``scrub()`` can build one, so "scrub before
every call" is enforced by the types, not by convention: a forgotten scrub is a TypeError,
not a leak.

The rules, in order:

1. known names (every word of 2+ letters of each name, "Last, First" included) -> ``[NAME]``;
2. greeting names at the start of a line ("Hi Sam," / "Good morning Sam Sample,") -> ``[NAME]``;
3. email addresses -> ``[EMAIL]``;
4. URLs (``http(s)://...``, ``www....``) -> ``[URL]``;
5. US phone numbers, with or without an extension -> ``[PHONE]``;
6. 8-digit IDs -> ``[ID]``;
7. usernames, 2-4 letters then 2-4 digits (``abc123``) -> ``[USER]``; over-scrubbing is fine;
8. a cap at ``max_chars``, cut on a word boundary;
9. a post-check: if a word of 3+ letters from a known name survived, ``ScrubRefused``.

Emails and URLs are found before rule 1 runs and set aside, so a name inside an address does
not break it up: ``sam.sample@example.edu`` is one ``[EMAIL]``, not ``[NAME].[NAME]@...``.

Personal data rule: nothing here logs, prints or raises with any of the text or the names.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

DataClass = Literal["synthetic", "personal", "institutional"]

# The counted kinds, in the order they are reported.
KINDS = ("name", "email", "phone", "id", "user", "url")

# Only scrub() holds this, so ScrubbedText cannot be built anywhere else.
_TOKEN = object()

# Punctuation that separates the parts of a known name (as in tdx.names, reimplemented here so
# common/ does not depend on tdx/).
_NAME_PUNCTUATION = re.compile(r"[,.'()\-]")
_LETTER = re.compile(r"[^\W\d_]")

_GREETING = re.compile(
    r"^(?P<lead>[ \t]*)(?P<greeting>hi|hello|hey|dear|good (?:morning|afternoon|evening))"
    r"[ \t]+[A-Za-z][\w'\-]*(?:[ \t]+[A-Za-z][\w'\-]*)?[ \t]*,",
    re.IGNORECASE | re.MULTILINE,
)
_EMAIL = re.compile(r"[\w.%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<![\w+])(?:\+?1[ \t.\-]?)?(?:\(\d{3}\)[ \t]?|\d{3}[ \t.\-]?)\d{3}[ \t.\-]?\d{4}"
    r"(?:[ \t]*(?:x|ext\.?|extension)[ \t]*\d{1,6})?(?!\w)",
    re.IGNORECASE,
)
_ID = re.compile(r"\b\d{8}\b")
_USER = re.compile(r"\b[A-Za-z]{2,4}\d{2,4}\b")
# Stand-ins for the emails and URLs set aside before rule 1; no rule matches them.
_HELD = re.compile(r"\x00(\d+)\x00")


class ScrubRefused(Exception):
    """A known name survived scrubbing. The message holds counts only."""


@dataclass(frozen=True, slots=True)
class ScrubbedText:
    """Text that went through ``scrub()``, with what was replaced (counts only)."""

    text: str
    replaced: Mapping[str, int]
    truncated: bool
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise TypeError("ScrubbedText is built only by scrub()")

    def __repr__(self) -> str:
        counts = ", ".join(f"{kind}={count}" for kind, count in self.replaced.items())
        return f"ScrubbedText(chars={len(self.text)}, truncated={self.truncated}, {counts})"


def _name_words(known_names: Sequence[str], min_letters: int) -> list[str]:
    """The distinct words of the known names with at least ``min_letters`` letters, longest
    first, so a longer word is replaced before a shorter one inside it could be."""
    words: set[str] = set()
    for name in known_names:
        for word in _NAME_PUNCTUATION.sub(" ", name).split():
            if len(_LETTER.findall(word)) >= min_letters:
                words.add(word.casefold())
    return sorted(words, key=lambda word: (-len(word), word))


def _whole_word(words: Sequence[str]) -> re.Pattern[str] | None:
    if not words:
        return None
    alternatives = "|".join(re.escape(word) for word in words)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    space = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if space > 0 and not text[max_chars].isspace():
        cut = cut[:space]
    return cut.rstrip(), True


def scrub(text: str, *, known_names: Sequence[str] = (), max_chars: int = 2000) -> ScrubbedText:
    """``text`` with personal details replaced by placeholders; ``ScrubRefused`` if a known
    name survives. See the module docstring for the rules and their order."""
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    counts = dict.fromkeys(KINDS, 0)
    held: list[str] = []

    def hold(kind: str, placeholder: str) -> Callable[[re.Match[str]], str]:
        def _hold(match: re.Match[str]) -> str:
            counts[kind] += 1
            held.append(placeholder)
            return f"\x00{len(held) - 1}\x00"

        return _hold

    def replace(kind: str, placeholder: str) -> Callable[[re.Match[str]], str]:
        def _replace(match: re.Match[str]) -> str:
            counts[kind] += 1
            return placeholder

        return _replace

    # Rules 3 and 4 are found first and held aside (see the module docstring).
    text = _EMAIL.sub(hold("email", "[EMAIL]"), text)
    text = _URL.sub(hold("url", "[URL]"), text)

    text = _replace_known_names(text, known_names, replace("name", "[NAME]"))

    def greeting(match: re.Match[str]) -> str:
        counts["name"] += 1
        return f"{match['lead']}{match['greeting']} [NAME],"

    text = _GREETING.sub(greeting, text)
    text = _HELD.sub(lambda match: held[int(match[1])], text)

    text = _PHONE.sub(replace("phone", "[PHONE]"), text)
    text = _ID.sub(replace("id", "[ID]"), text)
    text = _USER.sub(replace("user", "[USER]"), text)

    text, truncated = _cap(text, max_chars)

    survivor = _whole_word(_name_words(known_names, 3))
    if survivor is not None:
        left = len(survivor.findall(text))
        if left:
            raise ScrubRefused(f"a known name is still present after scrubbing ({left} times)")

    return ScrubbedText(text, MappingProxyType(counts), truncated, _TOKEN)


def _replace_known_names(
    text: str, known_names: Sequence[str], replacement: Callable[[re.Match[str]], str]
) -> str:
    """Rule 1: every word of 2+ letters of each known name, whole word, any case."""
    pattern = _whole_word(_name_words(known_names, 2))
    return text if pattern is None else pattern.sub(replacement, text)
