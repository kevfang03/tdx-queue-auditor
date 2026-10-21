"""scripts/export_public.py: the allowlist, the overlay, safe targets, re-export and the checks.

Each test builds a small git repository in a temporary folder. Identifying strings are put
together at run time, so this file does not trip the export's own privacy checks.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_public.py"
_spec = importlib.util.spec_from_file_location("export_public", SCRIPT)
export = importlib.util.module_from_spec(_spec)
sys.modules["export_public"] = export
_spec.loader.exec_module(export)

BASE_FILES = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": "VALUE = 1\n",
    "tests/test_core.py": "def test_value():\n    assert True\n",
    "scripts/export_public.py": "# the script\n",
    "pyproject.toml": "[project]\nname = 'pkg'\n",
    "LICENSE": "MIT License\n\nCopyright (c) 2026 Sample Owner\n",
    ".gitignore": "secret.env\n",
    "docs/USAGE.md": "# Usage\n\n## Demo\n\nSee the [design](DESIGN.md#principles).\n",
    "docs/ARCHITECTURE.md": "# Working notes\n",
    "docs/payloads/01-first.md": "# Payload\n",
    "CLAUDE.md": "# Instructions\n",
    ".claude/settings.json": "{}\n",
    "README.md": "# Working README\n",
    "public/README.md": "# Public README\n\n[Usage](docs/USAGE.md#demo) · [Licence](LICENSE)\n",
    "public/docs/DESIGN.md": "# Design\n\n## Principles\n\nBack to [usage](USAGE.md).\n",
    "public/docs/PRIVACY.md": "# Privacy\n\nContact: someone@example.edu\n",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _write(repo: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        out = repo / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, BASE_FILES)
    _commit(root, "initial")
    return root


def test_exports_the_allowlist_and_the_overlay_only(repo: Path, tmp_path: Path) -> None:
    _write(repo, {"src/pkg/untracked.py": "X = 1\n", "secret.env": "TOKEN=x\n"})
    target = tmp_path / "snapshot"

    assert export.run(repo, target) == 0

    written = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert written == sorted(
        [
            ".export-manifest",
            ".gitignore",
            "LICENSE",
            "README.md",
            "docs/DESIGN.md",
            "docs/PRIVACY.md",
            "docs/USAGE.md",
            "pyproject.toml",
            "scripts/export_public.py",
            "src/pkg/__init__.py",
            "src/pkg/core.py",
            "tests/test_core.py",
        ]
    )


def test_the_overlay_wins_over_an_allowlisted_file(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "snapshot"

    export.run(repo, target)

    assert (target / "README.md").read_text(encoding="utf-8").startswith("# Public README")
    assert not (target / "public").exists()


def test_never_exports_working_docs_even_if_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export, "ALLOWED_PREFIXES", ("docs/", ".claude/", ""))

    for path in ["docs/ARCHITECTURE.md", "docs/payloads/01-first.md", "CLAUDE.md", ".claude/x"]:
        assert not export.is_allowed(path)


def test_refuses_a_non_empty_folder_that_is_not_an_export(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "other"
    target.mkdir()
    (target / "notes.txt").write_text("mine\n", encoding="utf-8")

    assert export.run(repo, target) == 2

    assert "not a previous export" in capsys.readouterr().err
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]


def test_refuses_a_target_inside_the_repository(repo: Path) -> None:
    assert export.run(repo, repo / "snapshot") == 2
    assert not (repo / "snapshot").exists()


def test_refuses_uncommitted_changes_to_exported_files(repo: Path, tmp_path: Path) -> None:
    (repo / "src/pkg/core.py").write_text("VALUE = 2\n", encoding="utf-8")
    target = tmp_path / "snapshot"

    assert export.run(repo, target) == 2
    assert export.run(repo, target, allow_dirty=True) == 0
    assert (target / "src/pkg/core.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_reexport_replaces_only_the_files_in_the_manifest(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "snapshot"
    assert export.run(repo, target) == 0
    (target / ".git").mkdir()
    (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (target / ".venv").mkdir()
    (target / ".venv" / "marker").write_text("x\n", encoding="utf-8")
    (repo / "src/pkg/core.py").unlink()
    _write(repo, {"src/pkg/new.py": "NEW = 1\n"})
    _commit(repo)

    assert export.run(repo, target) == 0

    assert not (target / "src/pkg/core.py").exists()
    assert (target / "src/pkg/new.py").read_text(encoding="utf-8") == "NEW = 1\n"
    assert (target / ".git" / "HEAD").exists()
    assert (target / ".venv" / "marker").exists()
    manifest = (target / ".export-manifest").read_text(encoding="utf-8").splitlines()
    assert "src/pkg/new.py" in manifest
    assert "src/pkg/core.py" not in manifest


def test_reexport_removes_folders_it_emptied(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "snapshot"
    export.run(repo, target)
    (repo / "tests/test_core.py").unlink()
    _commit(repo)

    assert export.run(repo, target) == 0

    assert not (target / "tests").exists()


def test_reexport_refuses_to_overwrite_a_file_it_did_not_write(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "snapshot"
    export.run(repo, target)
    _write(repo, {"src/pkg/later.py": "LATER = 1\n"})
    _commit(repo)
    (target / "src/pkg/later.py").write_text("my own\n", encoding="utf-8")

    assert export.run(repo, target) == 2

    assert (target / "src/pkg/later.py").read_text(encoding="utf-8") == "my own\n"
    assert (target / "src/pkg/core.py").exists()


def test_reexport_refuses_a_manifest_pointing_outside(repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "snapshot"
    export.run(repo, target)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    with (target / ".export-manifest").open("a", encoding="utf-8") as manifest:
        manifest.write("../victim.txt\n")

    assert export.run(repo, target) == 2

    assert victim.exists()


HITS = [
    ("Mail " + "icommons" + "crc" + " for help.", "shared-mailbox"),
    ("Host: tdx." + "drexel" + ".edu", "university-domain"),
    ("https://sample." + "teamdynamix" + ".com/TDNext", "tdx-host"),
    ("Contact jordan.real" + "@" + "gmail.com", "email-address"),
    ("Logs in C:" + "\\Users\\" + "Admin\\AppData", "windows-profile-path"),
    ('HOME = "/' + "Users/" + 'realname/project"', "posix-home-path"),
    ("Built by " + "kevfang" + "03", "owner-handle"),
    ("key = sk-ant-" + "api03-" + "A" * 40, "anthropic-api-key"),
    ("https://x.logic.azure.com/workflows/1?sig=" + "Ab1_" * 10, "webhook-signature"),
]


# The ids name the pattern, so test output never shows the seeded text either.
@pytest.mark.parametrize(("text", "pattern"), HITS, ids=[pattern for _, pattern in HITS])
def test_a_privacy_hit_fails_and_prints_no_matched_text(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    pattern: str,
) -> None:
    _write(repo, {"src/pkg/core.py": f"VALUE = 1\n# {text}\n"})
    _commit(repo)
    target = tmp_path / "snapshot"

    assert export.run(repo, target) == 1

    err = capsys.readouterr().err
    assert f"Privacy check failed: src/pkg/core.py:2: {pattern}" in err
    assert text not in err
    assert not target.exists()


@pytest.mark.parametrize(
    "line",
    [
        "Mail jordan" + "@" + "example.edu or help" + "@" + "mail.example.com.",
        "Co-Authored-By: Claude <noreply" + "@" + "anthropic.com>",
        "Startup file: C:" + "\\Users\\" + "you\\AppData\\Roaming",
        'WIN = r"C:' + "\\Users\\" + 'Desk Owner\\.local\\bin\\uv.exe"',
        "cd /" + "Users/" + "you/project",
        "uses: astral-sh/setup-uv@v10.3.0",
        "key = sk-ant-test-0000",
        "url = https://x.logic.azure.com/w?sig=SIGSECRET42",
        "page: /TDWorkManagement/" + "Home/Desktop",
    ],
)
def test_placeholders_pass_the_privacy_checks(line: str) -> None:
    assert export.privacy_hits({"src/pkg/core.py": line.encode()}) == []


def test_the_owner_handle_is_allowed_only_in_the_public_repo_links() -> None:
    handle = "kevfang" + "03"
    files = {
        "README.md": f"[![CI](https://github.com/{handle}/tdx-queue-auditor/badge.svg)]\n"
        f"git clone https://github.com/{handle}/tdx-queue-auditor.git\n"
        f"Old repo: https://github.com/{handle}/enterprise-ai-ops-suite\n".encode(),
        "LICENSE": f"Copyright (c) 2026 {handle}\n".encode(),
        "docs/USAGE.md": f"https://github.com/{handle}/tdx-queue-auditor\n".encode(),
    }

    hits = export.privacy_hits(files)

    assert [(h.path, h.line) for h in hits] == [("README.md", 3), ("docs/USAGE.md", 1)]


def test_binary_files_are_not_scanned() -> None:
    data = b"\x89PNG\0" + ("icommons" + "crc").encode()

    assert export.privacy_hits({"docs/img/card.png": data}) == []


def test_a_broken_relative_link_fails(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            "docs/USAGE.md": "# Usage\n\n## Demo\n\nDecisions: [notes](ARCHITECTURE.md).\n"
            "Steps: [payload](payloads/01-first.md).\n"
        },
    )
    _commit(repo)
    target = tmp_path / "snapshot"

    assert export.run(repo, target) == 1

    err = capsys.readouterr().err
    assert "Broken link: docs/USAGE.md:5: ARCHITECTURE.md" in err
    assert "Broken link: docs/USAGE.md:6: payloads/01-first.md" in err
    assert not target.exists()


def test_link_check_resolves_paths_folders_and_anchors() -> None:
    files = {
        "README.md": (
            b"# Title\n\n"
            b"[ok](docs/USAGE.md#watching-the-queue-tdx-watch) [dir](src/) [web](https://x.org)\n"
            b"[up](../outside.md) [anchor](docs/USAGE.md#missing) [self](#title)\n"
            b"`[code](nowhere.md)`\n"
            b"```\n[fenced](nowhere.md)\n```\n"
            b"![img](docs/img/a.png)\n"
        ),
        "docs/USAGE.md": b"# Usage\n\n## Watching the queue (tdx watch)\n",
        "docs/img/a.png": b"\x89PNG\0",
        "src/pkg/core.py": b"",
    }

    broken, checked = export.broken_links(files)

    assert [(b.line, b.target) for b in broken] == [
        (4, "../outside.md"),
        (4, "docs/USAGE.md#missing"),
    ]
    assert checked == 6


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Teams alerts (tdx audit --send, tdx watch)", "teams-alerts-tdx-audit---send-tdx-watch"),
        ("Windows: use uv's Python", "windows-use-uvs-python"),
        ("The `ScrubbedText` type", "the-scrubbedtext-type"),
        ("TLS via truststore", "tls-via-truststore"),
    ],
)
def test_github_slugs(heading: str, slug: str) -> None:
    assert export.github_slug(heading) == slug


def test_check_mode_writes_nothing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert export.main(["--check", "--repo", str(repo)]) == 0

    assert "Check only: 11 file(s)" in capsys.readouterr().out
    assert not list(tmp_path.rglob(".export-manifest"))
