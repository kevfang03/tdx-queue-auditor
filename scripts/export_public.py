"""Build the public snapshot of this repository in a separate folder.

    uv run python scripts/export_public.py --to <folder>
    uv run python scripts/export_public.py --check

The snapshot is an allowlist of tracked files (``git ls-files``, so untracked and ignored files
such as captures, ``.env`` or data folders can never slip in) plus the ``public/`` overlay, which
wins over an allowlisted file of the same name. Before anything is written, every relative
Markdown link must resolve inside the snapshot and every text file must pass the privacy checks.
A privacy hit is reported as file, line and pattern name only, never the matched text.

The target folder must be outside this repository, and either empty or a previous export: one
holding the ``.export-manifest`` this script writes. A re-export deletes only the files listed
in that manifest, so the target's own ``.git`` and ``.venv`` stay. ``--check`` runs the checks
without writing anything.

Exit codes: 0 done, 1 a check failed, 2 the target or the repository is unusable.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

MANIFEST = ".export-manifest"
MANIFEST_HEADER = "# Written by scripts/export_public.py: the files it wrote, one per line."

ALLOWED_PREFIXES = ("src/", "tests/", "scripts/", ".github/workflows/", "docs/img/")
ALLOWED_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".gitignore",
        "LICENSE",
        "config.example.toml",
        ".env.example",
        "docs/USAGE.md",
        "docs/tdx-rules.md",
        "docs/tdx-page-structure.md",
    }
)
# Never exported, even if an allowlist entry above is widened by mistake.
NEVER_PREFIXES = (
    "docs/payloads/",
    "docs/guides/",
    ".claude/",
    "public/",
    "tests/fixtures/tdx/captures/",
)
NEVER_FILES = frozenset(
    {
        "docs/ARCHITECTURE.md",
        "docs/OPEN_QUESTIONS.md",
        "docs/GOVERNANCE.md",
        "docs/PUBLISHING.md",
        "CLAUDE.md",
    }
)
OVERLAY = {
    "public/README.md": "README.md",
    "public/docs/DESIGN.md": "docs/DESIGN.md",
    "public/docs/PRIVACY.md": "docs/PRIVACY.md",
}


class ExportError(Exception):
    """The repository or the target folder cannot be used; the message says why."""


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    pattern: str


@dataclass(frozen=True)
class BrokenLink:
    path: str
    line: int
    target: str


# --- Privacy checks ---------------------------------------------------------------------------
# The identifying strings are spelled so that this file does not match its own patterns: it is
# exported too.

_OWNER_HANDLE = "kevfang" + "03"
_PUBLIC_REPO = f"github.com/{_OWNER_HANDLE}/tdx-queue-auditor"
_ALLOWED_ADDRESSES = frozenset({"noreply@anthropic.com"})
_TEST_TLDS = frozenset({"example", "test", "invalid", "localhost"})
# Placeholder user-profile folder names in the tests and docs. "jos" is a test's "José" written
# with a é escape, which ends the name at the backslash.
_PLACEHOLDER_USERS = frozenset(
    {"you", "someone", "owner", "desk owner", "josé", "jos", "public", "default"}
)
_PLACEHOLDER_POSIX_USERS = frozenset({"you", "someone", "owner", "jordan.sampleton", "runner"})


def _address_allowed(path: str, line: str, match: re.Match[str]) -> bool:
    address = match.group(0).lower()
    if address in _ALLOWED_ADDRESSES:
        return True
    labels = address.rsplit("@", 1)[1].split(".")
    return labels[-1] in _TEST_TLDS or (len(labels) >= 2 and labels[-2] == "example")


def _placeholder_user(name: str, allowed: frozenset[str]) -> bool:
    name = name.strip().lower()
    if not name or name[0] in "%{$<":
        return True
    return any(name == user or name.startswith(user + " ") for user in allowed)


def _windows_user_allowed(path: str, line: str, match: re.Match[str]) -> bool:
    return _placeholder_user(match.group(1), _PLACEHOLDER_USERS)


def _posix_user_allowed(path: str, line: str, match: re.Match[str]) -> bool:
    return _placeholder_user(match.group(1), _PLACEHOLDER_POSIX_USERS)


def _handle_allowed(path: str, line: str, match: re.Match[str]) -> bool:
    if path == "LICENSE" and line.lower().startswith("copyright"):
        return True
    if path != "README.md":
        return False
    start = match.start() - len("github.com/")
    return start >= 0 and line[start : start + len(_PUBLIC_REPO)].lower() == _PUBLIC_REPO


Allow = Callable[[str, str, re.Match[str]], bool]


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern[str]
    allow: Allow | None = None


PATTERNS = (
    Pattern("shared-mailbox", re.compile(r"icommons[c]rc", re.IGNORECASE)),
    Pattern("university-domain", re.compile(r"drexel\.edu", re.IGNORECASE)),
    Pattern("tdx-host", re.compile(r"teamdynamix\.com", re.IGNORECASE)),
    Pattern(
        "email-address",
        re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", re.IGNORECASE),
        _address_allowed,
    ),
    Pattern(
        "windows-profile-path",
        re.compile(r"[a-z]:(?:\\\\|\\|/)users(?:\\\\|\\|/)([^\\/\"'`\r\n<>|:*?]*)", re.IGNORECASE),
        _windows_user_allowed,
    ),
    # Case-sensitive: macOS and Linux spell these folders exactly so, and "/users/" in a web
    # path is not a home folder.
    Pattern(
        "posix-home-path",
        re.compile(r"(?<![\w.~/-])/(?:Users|home)/([^/\\\"'`\s<>|:*?]*)"),
        _posix_user_allowed,
    ),
    Pattern("owner-handle", re.compile(r"kevfang0[3]", re.IGNORECASE), _handle_allowed),
    Pattern("anthropic-api-key", re.compile(r"sk-ant-[a-z]+\d*-[A-Za-z0-9_-]{20,}")),
    Pattern("webhook-signature", re.compile(r"sig=[A-Za-z0-9_%-]{20,}")),
)


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def privacy_hits(files: dict[str, bytes]) -> list[Hit]:
    """Every identifying string in the snapshot's paths and text files, as file, line, name."""
    hits: list[Hit] = []
    for path in sorted(files):
        lines = [(0, path)]
        if is_text(files[path]):
            lines += enumerate(files[path].decode("utf-8").splitlines(), start=1)
        for number, line in lines:
            for pattern in PATTERNS:
                for match in pattern.regex.finditer(line):
                    if pattern.allow is None or not pattern.allow(path, line, match):
                        hits.append(Hit(path, number, pattern.name))
    return hits


# --- Link check -------------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`]*`")
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_HTML_ID = re.compile(r"<a\s+[^>]*(?:id|name)=\"([^\"]+)\"", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """The lines outside fenced code blocks, numbered from 1."""
    lines, fenced = [], False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append((number, line))
    return lines


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a Markdown heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in _prose_lines(text):
        found.update(match.lower() for match in _HTML_ID.findall(line))
        heading = _HEADING.match(line)
        if heading:
            slug = github_slug(heading.group(1))
            seen = counts.get(slug, 0)
            counts[slug] = seen + 1
            found.add(slug if seen == 0 else f"{slug}-{seen}")
    return found


def broken_links(files: dict[str, bytes]) -> tuple[list[BrokenLink], int]:
    """Relative links in the snapshot's Markdown files that do not resolve inside it."""
    markdown = {
        path: files[path].decode("utf-8")
        for path in files
        if path.lower().endswith(".md") and is_text(files[path])
    }
    anchor_cache: dict[str, set[str]] = {}
    broken: list[BrokenLink] = []
    checked = 0
    for path in sorted(markdown):
        for number, line in _prose_lines(markdown[path]):
            line = _INLINE_CODE.sub("", line)
            targets = [m.group(1) for m in _LINK.finditer(line)]
            targets += [m.group(1) for m in _REFERENCE.finditer(line)]
            for target in targets:
                if _SCHEME.match(target) or target.startswith("/"):
                    continue
                checked += 1
                if not _resolves(path, unquote(target), files, markdown, anchor_cache):
                    broken.append(BrokenLink(path, number, target))
    return broken, checked


def _resolves(
    source: str,
    target: str,
    files: dict[str, bytes],
    markdown: dict[str, str],
    anchor_cache: dict[str, set[str]],
) -> bool:
    link_path, _, fragment = target.partition("#")
    if link_path:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), link_path))
        if resolved == ".." or resolved.startswith("../"):
            return False
    else:
        resolved = source
    if resolved not in files:
        prefix = resolved.rstrip("/") + "/"
        return not fragment and any(path.startswith(prefix) for path in files)
    if fragment and resolved in markdown:
        if resolved not in anchor_cache:
            anchor_cache[resolved] = anchors(markdown[resolved])
        return fragment.lower() in anchor_cache[resolved]
    return True


# --- Selecting the files ----------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise ExportError(f"git {args[0]} failed in {repo}: is it a git repository with a commit?")
    return result.stdout


def tracked_files(repo: Path) -> list[str]:
    return [path for path in _git(repo, "ls-files", "-z").split("\0") if path]


def changed_files(repo: Path) -> set[str]:
    """Tracked files whose working copy differs from HEAD (staged or not, deleted included)."""
    return {path for path in _git(repo, "diff", "--name-only", "-z", "HEAD").split("\0") if path}


def is_allowed(path: str) -> bool:
    if path in NEVER_FILES or path.startswith(NEVER_PREFIXES):
        return False
    return path in ALLOWED_FILES or path.startswith(ALLOWED_PREFIXES)


def select_files(tracked: list[str]) -> dict[str, str]:
    """Snapshot path -> repository path: the allowlist, then the overlay on top."""
    tracked_set = set(tracked)
    missing = sorted(source for source in OVERLAY if source not in tracked_set)
    if missing:
        raise ExportError(f"overlay file(s) not tracked by git: {', '.join(missing)}")
    selected = {path: path for path in tracked if is_allowed(path)}
    selected.update({dest: source for source, dest in OVERLAY.items()})
    return selected


def build_snapshot(repo: Path, allow_dirty: bool = False) -> dict[str, bytes]:
    """The snapshot's files and their contents, read from the repository's working copy."""
    selected = select_files(tracked_files(repo))
    if not allow_dirty:
        dirty = sorted(set(selected.values()) & changed_files(repo))
        if dirty:
            raise ExportError(
                f"{len(dirty)} exported file(s) differ from the last commit, first "
                f"{dirty[0]}: commit them, or pass --allow-dirty"
            )
    return {dest: (repo / source).read_bytes() for dest, source in sorted(selected.items())}


# --- Writing ----------------------------------------------------------------------------------


def _norm(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _inside(child: Path, parent: Path) -> bool:
    child_s, parent_s = _norm(child), _norm(parent)
    return child_s == parent_s or child_s.startswith(parent_s.rstrip(os.sep) + os.sep)


def _safe_entry(entry: str) -> bool:
    pure = PurePosixPath(entry)
    return (
        bool(entry)
        and "\\" not in entry
        and ":" not in entry
        and not pure.is_absolute()
        and ".." not in pure.parts
        and pure.parts[0] != ".git"
        and entry != MANIFEST
    )


def previous_export(repo: Path, target: Path) -> list[str] | None:
    """The previous manifest's entries, None for a new or empty folder; refuse anything else."""
    if _inside(target, repo) or _inside(repo, target):
        raise ExportError("the target must be a folder outside this repository")
    if not target.exists():
        return None
    if not target.is_dir():
        raise ExportError("the target exists and is not a folder")
    manifest = target / MANIFEST
    if not manifest.is_file():
        if any(target.iterdir()):
            raise ExportError(
                f"the target is not empty and holds no {MANIFEST}, so it is not a previous export"
            )
        return None
    entries = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    unsafe = [entry for entry in entries if not _safe_entry(entry)]
    if unsafe:
        raise ExportError(f"{MANIFEST} lists {len(unsafe)} path(s) outside the export; refusing")
    return entries


def write_snapshot(target: Path, files: dict[str, bytes], previous: list[str] | None) -> None:
    previous_set = set(previous or [])
    clashes = sorted(p for p in files if p not in previous_set and (target / p).exists())
    if clashes:
        raise ExportError(
            f"{len(clashes)} file(s) in the target were not written by an export, first "
            f"{clashes[0]}: move them away first"
        )
    for entry in sorted(previous_set):
        old = target / entry
        if old.is_file() or old.is_symlink():
            old.unlink()
        parent = old.parent
        while parent != target and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for path, data in files.items():
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    (target / MANIFEST).write_text(
        "\n".join([MANIFEST_HEADER, *sorted(files)]) + "\n", encoding="utf-8"
    )


# --- Command line -----------------------------------------------------------------------------


def run(repo: Path, target: Path | None, allow_dirty: bool = False) -> int:
    try:
        previous = previous_export(repo, target) if target is not None else None
        files = build_snapshot(repo, allow_dirty=allow_dirty)
    except ExportError as error:
        print(f"Export refused: {error}", file=sys.stderr)
        return 2

    hits = privacy_hits(files)
    broken, checked = broken_links(files)
    for hit in hits:
        print(f"Privacy check failed: {hit.path}:{hit.line}: {hit.pattern}", file=sys.stderr)
    for link in broken:
        print(f"Broken link: {link.path}:{link.line}: {link.target}", file=sys.stderr)
    if hits or broken:
        print(
            f"Export failed: {len(hits)} privacy hit(s), {len(broken)} broken link(s). "
            "Nothing was written.",
            file=sys.stderr,
        )
        return 1

    texts = sum(1 for data in files.values() if is_text(data))
    overlay = sum(1 for dest in OVERLAY.values() if dest in files)
    checks = (
        f"Privacy checks passed: {len(PATTERNS)} patterns over {texts} text file(s). "
        f"Links: {checked} checked, 0 broken."
    )
    if target is None:
        print(f"Check only: {len(files)} file(s) would be exported ({overlay} from public/).")
        print(checks)
        return 0
    try:
        write_snapshot(target, files, previous)
    except ExportError as error:
        print(f"Export refused: {error}", file=sys.stderr)
        return 2
    removed = len(set(previous or []) - set(files))
    print(f"Exported {len(files)} file(s) ({overlay} from public/) to {target}.")
    if previous is not None:
        print(f"Re-export: replaced {len(previous)} file(s); {removed} no longer exported.")
    print(checks)
    print(f"Wrote {MANIFEST}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the public snapshot of this repository.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--to", type=Path, metavar="FOLDER", help="the snapshot folder")
    action.add_argument("--check", action="store_true", help="run the checks, write nothing")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="export tracked files even when they differ from the last commit",
    )
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    target = None if args.check else args.to.expanduser().absolute()
    return run(args.repo.resolve(), target, allow_dirty=args.allow_dirty)


if __name__ == "__main__":
    sys.exit(main())
