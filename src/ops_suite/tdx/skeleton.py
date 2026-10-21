"""Turn a page's HTML into a structure-only skeleton that is safe to hand to a Claude session.

Pure standard library; no Playwright. The skeleton keeps tag names, a few structural
attributes and short column-header labels. Every other text node becomes a shape placeholder
such as ``[datetime]`` or ``[text:2]``, and every other attribute is dropped. When in doubt,
text is replaced rather than kept.

Kept attribute values can carry personal data too (TDX builds the ids of its notify-list
checkboxes from people's names and addresses), so they are cleaned as well: long digit runs
become ``#``, a value with ``@`` becomes ``[email]``, the ``id`` and ``name`` of form controls
are always replaced, and any word in an ``id``, ``class`` or ``name`` that also appears in the
page's own text or removed attributes (a person's name, for example) becomes ``*``, unless it
is a column header or a common TDX interface word.
"""

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Removed together with everything inside them. <head> holds the page title, which can carry a
# ticket title; a stray <title> elsewhere is dropped for the same reason.
_DROPPED_ELEMENTS = frozenset({"head", "title", "script", "style", "noscript", "template"})
_KEPT_ATTRIBUTES = frozenset({"id", "class", "role", "type", "colspan", "rowspan", "name"})
# Kept attributes whose values are free-form and so are checked against the page's own words.
_WORD_CHECKED_ATTRIBUTES = frozenset({"id", "class", "name"})
# The tool never fills forms, so the ids and names of form controls are not needed for
# selectors; they are where TDX puts people's names (one notify checkbox per person).
_FORM_CONTROLS = frozenset({"input", "select", "option", "textarea", "button"})
_URL_ATTRIBUTES = frozenset({"href", "src", "action"})
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_CELL_TAGS = frozenset({"td", "th"})
_CELL_ROLES = frozenset({"cell", "gridcell", "rowheader", "columnheader"})
# A new cell or row implicitly closes the open cell (and row) up to these boundaries.
_ROW_BOUNDARIES = frozenset({"tr", "table", "thead", "tbody", "tfoot"})
_SECTION_BOUNDARIES = frozenset({"table", "thead", "tbody", "tfoot"})

MAX_HEADER_LENGTH = 40
# Attribute values longer than this (view state, encoded blobs) are not mined for words.
_MAX_WORD_SOURCE_LENGTH = 200

# Interface words that may stay in an id/class/name even when the page's text uses them too.
# Column headers found on the page are added to these per page.
_INTERFACE_WORDS = frozenset(
    """
    id ids ticket tickets title requestor requester created modified status priority due
    service services resp responsible primary prim group groups acct dept account department
    affiliation responded date time by feed feeds comment comments notify notified update
    updates save cancel reply private public attachment attachments description detail details
    type source desktop dashboard report reports home search filter sort sorted page pages next
    previous prev first last edit actions action menu tab tabs new open closed close active
    inactive unclaimed claimed others all my me people person contact email phone location
    room impact urgency classification form task tasks asset assets knowledge work management
    app apps view views list grid table row rows column columns header footer body content
    main nav left right top bottom show hide more less loading load refresh help settings
    profile logout login sign in out of to for and the it ok yes no none name names user users
    """.split()
)

_VALID_TAG = re.compile(r"[a-z][a-z0-9-]*")
_ALPHA_RUN = re.compile(r"[A-Za-z]+")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
_EMAIL_LOCAL_PART = re.compile(r"([\w.+-]+)@")

_DIGIT_RUN = re.compile(r"\d{3,}")
_NUMBER_ONLY = re.compile(r"[\d\s\-().+/#]*\d[\d\s\-().+/#]*")

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_WEEKDAY = (
    r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\.?"
)
# Each piece a date/time text can be made of. A text is a date/time when it contains at least
# one of the first group and nothing is left once all of them are removed.
_DATE_TIME_TOKENS = re.compile(
    r"""
    (?<!\d)\d{4}-\d{2}-\d{2}(?:[t\s]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?(?!\d)
    | (?<!\d)\d{1,4}([/.-])\d{1,2}\1\d{1,4}(?!\d)
    | (?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?(?![\d\w])
    | \b<month>\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?\b
    | \b\d{1,2}(?:st|nd|rd|th)?\s+<month>(?:,?\s+\d{2,4})?\b
    """.replace("<month>", _MONTH),
    re.IGNORECASE | re.VERBOSE,
)
_DATE_TIME_FILLERS = re.compile(
    r"\b(?:" + _WEEKDAY + r"|at|on|[ap]\.?m\.?|utc|gmt|e[sd]?t|c[sd]?t|m[sd]?t|p[sd]?t)(?!\w)"
    r"|[\s,.:;|\-–/()]+",
    re.IGNORECASE,
)


def mask_digits(value: str) -> str:
    """Replace every run of 3 or more digits with ``#``; shorter runs stay."""
    return _DIGIT_RUN.sub("#", value)


def _looks_like_datetime(text: str) -> bool:
    if not _DATE_TIME_TOKENS.search(text):
        return False
    rest = _DATE_TIME_TOKENS.sub(" ", text)
    return not _DATE_TIME_FILLERS.sub("", rest)


def placeholder_for(text: str) -> str:
    """The shape placeholder for a text node: email, date/time, number or word count."""
    if "@" in text:
        return "[email]"
    if _looks_like_datetime(text):
        return "[datetime]"
    if _NUMBER_ONLY.fullmatch(text.strip()):
        return "[number]"
    return f"[text:{len(text.split())}]"


@dataclass(frozen=True, slots=True)
class Skeleton:
    html: str  # the structure-only HTML
    kept_text: tuple[str, ...]  # every real text string that was kept (header labels), in order


@dataclass(slots=True)
class _Open:
    tag: str
    is_cell: bool
    is_header: bool


def _cell_kind(tag: str, attrs: dict[str, str | None]) -> tuple[bool, bool]:
    """(is_cell, is_header). An explicit cell role wins over the tag; row headers hold data."""
    role = (attrs.get("role") or "").strip().lower()
    if role in _CELL_ROLES:
        return True, role == "columnheader"
    if tag in _CELL_TAGS:
        scope = (attrs.get("scope") or "").strip().lower()
        return True, tag == "th" and scope not in {"row", "rowgroup"}
    return False, False


def _word_forms(run: str) -> set[str]:
    """``run`` and its camel-case parts of 2+ letters, lower-cased."""
    forms = {run.lower()}
    forms.update(part.lower() for part in _CAMEL_PART.findall(run) if len(part) >= 2)
    return forms


def _page_words(text: str) -> set[str]:
    """Words of ``text`` that could be part of a name: capitalised words, email local parts."""
    words: set[str] = set()
    for run in _ALPHA_RUN.findall(text):
        if len(run) >= 2 and run[0].isupper():
            words |= _word_forms(run)
    for local_part in _EMAIL_LOCAL_PART.findall(text):
        for run in _ALPHA_RUN.findall(local_part):
            if len(run) >= 2:
                words |= _word_forms(run)
    return words


class _SkeletonParser(HTMLParser):
    def __init__(self, page_words: frozenset[str] = frozenset()) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.kept: list[str] = []
        # Words seen in text and in removed attribute values (gathered by the first pass).
        self.words: set[str] = set()
        # Words to mask in id/class/name values (handed to the second pass).
        self._page_words = page_words
        self._stack: list[_Open] = []
        self._text: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    # --- events -------------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush_text()
        if not _VALID_TAG.fullmatch(tag):
            # Not a real element: text such as "Name <alex@example.edu>" read as a tag.
            return
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
                return
            if not (self._skip_tag == "head" and tag == "body"):
                return
            # An unclosed <head> ends where the body starts.
            self._skip_tag = None
        if tag in _DROPPED_ELEMENTS:
            self._start_skip(tag)
            return
        if tag == "svg":
            self.out.append(f"{self._render_start(tag, attrs)}</svg>")
            self._start_skip(tag)
            return
        self._close_implied(tag)
        self.out.append(self._render_start(tag, attrs))
        if tag not in _VOID_ELEMENTS:
            is_cell, is_header = _cell_kind(tag, dict(attrs))
            self._stack.append(_Open(tag, is_cell, is_header))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush_text()
        if self._skip_tag is not None or not _VALID_TAG.fullmatch(tag):
            return
        if tag in _DROPPED_ELEMENTS:
            return
        if tag == "svg":
            self.out.append(f"{self._render_start(tag, attrs)}</svg>")
            return
        self._close_implied(tag)
        self.out.append(self._render_start(tag, attrs))
        if tag not in _VOID_ELEMENTS:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        self._flush_text()
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _VOID_ELEMENTS or not _VALID_TAG.fullmatch(tag):
            return
        self.out.append(f"</{tag}>")
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._skip_tag is None:
            self._text.append(data)

    def handle_comment(self, data: str) -> None:
        self._flush_text()

    def handle_decl(self, decl: str) -> None:
        self._flush_text()
        if self._skip_tag is None and decl.lower().startswith("doctype"):
            self.out.append("<!DOCTYPE html>")

    def handle_pi(self, data: str) -> None:
        self._flush_text()

    def unknown_decl(self, data: str) -> None:
        self._flush_text()

    def close(self) -> None:
        super().close()
        self._flush_text()

    # --- helpers ------------------------------------------------------------------------

    def _start_skip(self, tag: str) -> None:
        self._skip_tag = tag
        self._skip_depth = 1

    def _close_implied(self, tag: str) -> None:
        """Pop cells (and rows) that a new cell or row implicitly closes, as HTML does."""
        if tag in _CELL_TAGS:
            closes, boundaries = _CELL_TAGS, _ROW_BOUNDARIES
        elif tag == "tr":
            closes, boundaries = _CELL_TAGS | {"tr"}, _SECTION_BOUNDARIES
        else:
            return
        for index in range(len(self._stack) - 1, -1, -1):
            open_tag = self._stack[index].tag
            if open_tag in closes:
                del self._stack[index:]
                return
            if open_tag in boundaries:
                return

    def _in_header(self) -> bool:
        for element in reversed(self._stack):
            if element.is_cell:
                return element.is_header
        return False

    def _flush_text(self) -> None:
        if not self._text:
            return
        text = "".join(self._text)
        self._text.clear()
        if not text.strip():
            self.out.append("\n" if "\n" in text else " ")
            return
        collapsed = " ".join(text.split())
        self.words |= _page_words(collapsed)
        placeholder = placeholder_for(collapsed)
        if (
            self._in_header()
            and len(collapsed) <= MAX_HEADER_LENGTH
            and placeholder.startswith("[text:")
        ):
            self.kept.append(collapsed)
            self.out.append(html.escape(collapsed, quote=False))
        else:
            self.out.append(placeholder)

    def _render_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts = [tag]
        for name, value in attrs:
            if name in _KEPT_ATTRIBUTES:
                if value is None:
                    parts.append(name)
                else:
                    cleaned = self._clean_value(tag, name, value)
                    parts.append(f'{name}="{html.escape(cleaned, quote=True)}"')
                continue
            if value and len(value) <= _MAX_WORD_SOURCE_LENGTH:
                self.words |= _page_words(value)
            if name in _URL_ATTRIBUTES:
                parts.append(f'{name}="[url]"')
        return f"<{' '.join(parts)}>"

    def _clean_value(self, tag: str, name: str, value: str) -> str:
        if "@" in value:
            return "[email]"
        if tag in _FORM_CONTROLS and name in {"id", "name"}:
            return f"[{name}]"
        if name in _WORD_CHECKED_ATTRIBUTES:
            value = _ALPHA_RUN.sub(self._mask_word, value)
        return mask_digits(value)

    def _mask_word(self, match: re.Match[str]) -> str:
        run = match.group(0)
        if run.lower() in self._page_words:
            return "*"
        parts = _CAMEL_PART.findall(run)
        return "".join("*" if part.lower() in self._page_words else part for part in parts)


def _parse(html_text: str, page_words: frozenset[str]) -> _SkeletonParser:
    parser = _SkeletonParser(page_words)
    parser.feed(html_text)
    parser.close()
    return parser


def skeletonize(html_text: str) -> Skeleton:
    """The structure-only version of ``html_text``; see the module docstring for the rules.

    Two passes: the first gathers the page's own words and its column headers, the second
    writes the skeleton with those words masked in id, class and name values.
    """
    first = _parse(html_text, frozenset())
    allowed = set(_INTERFACE_WORDS)
    for label in first.kept:
        for run in _ALPHA_RUN.findall(label):
            allowed |= _word_forms(run)
    second = _parse(html_text, frozenset(first.words - allowed))
    return Skeleton(html="".join(second.out), kept_text=tuple(second.kept))
