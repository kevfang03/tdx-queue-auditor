"""Start ``tdx watch`` at login: a launchd user agent on macOS, a Startup-folder script on
Windows.

Both only start the same ``tdx watch`` process a person runs in a terminal; the desk-hours
schedule stays inside the program (no Task Scheduler or cron, so it works the same on every
system). On macOS ``KeepAlive``
is false, so launchd never restarts it after "auditor stopped", and ``caffeinate -i`` keeps
the Mac awake. On Windows ``WindowsStartup`` writes a ``.cmd`` file into the user's Startup
folder, and ``tdx watch`` keeps the PC awake itself with ``keep_awake()``. Linux has no
implementation: it runs ``tdx watch`` in a terminal, and ``for_platform`` returns None there.

The launchctl and tasklist calls go through an injectable runner, and the Windows API call
through an injectable function, so the tests run on any OS without touching launchd, the
Startup folder or Windows. The Startup script is built from ``PurePath`` values and reads the
uv path as a Windows path, so tests check it with ``PureWindowsPath`` paths on any OS.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Protocol

from ops_suite.common.hints import RUN
from ops_suite.common.paths import APP_NAME

log = logging.getLogger(__name__)

LABEL = "com.ops-suite.tdx-watch"
CAFFEINATE = "/usr/bin/caffeinate"
OUT_LOG = "watch.out.log"
ERR_LOG = "watch.err.log"
# launchctl bootout returns before the old job is fully gone; a bootstrap right after it can
# fail with "Input/output error", so it is tried a few times.
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_RETRY_SECONDS = 1.0

# Windows: the Startup-folder script, and the title of the minimised window it opens, which
# ``status`` looks for with tasklist (cmd adds " - <command>" to it while a command runs).
WINDOW_TITLE = f"{APP_NAME} tdx watch"
STARTUP_SCRIPT = f"{WINDOW_TITLE}.cmd"
MINIMISED_FLAG = "--minimised"
# SetThreadExecutionState flags (winbase.h).
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_LAST_EXIT = re.compile(r"last exit code = (-?\d+)")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class UvNotFoundError(Exception):
    """``uv`` is not on PATH, so the agent would have nothing to start."""


class StartupFolderError(Exception):
    """The Windows Startup folder is unknown (``APPDATA`` is not set)."""


class LaunchctlError(Exception):
    """A launchctl call failed. ``reason`` is launchctl's own short message."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"launchctl {action} failed: {reason}")
        self.action = action
        self.reason = reason


@dataclass(frozen=True)
class AgentStatus:
    """``plist_path`` is the agent's file (the ``.cmd`` script on Windows). On Windows
    ``loaded`` means its ``tdx watch`` window was found, and ``running`` says the same, or None
    when tasklist could not be asked; macOS leaves ``running`` None."""

    installed: bool
    loaded: bool
    last_exit_code: int | None
    plist_path: Path
    out_log: Path
    err_log: Path
    running: bool | None = None


class AutoStart(Protocol):
    def install(self, *, destination: str, extra_args: Sequence[str]) -> Path:
        """Write and load the agent (replacing one that is loaded); return its file."""
        ...

    def uninstall(self) -> bool:
        """Unload and remove the agent; True if something was removed."""
        ...

    def status(self) -> AgentStatus: ...


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def for_platform(*, repo: Path, log_dir: Path, platform: str = sys.platform) -> AutoStart | None:
    """The start-at-login implementation for this OS; None on Linux."""
    if platform == "darwin":
        return MacLaunchAgent(repo=repo, log_dir=log_dir)
    if platform == "win32":
        return WindowsStartup(repo=repo, log_dir=log_dir)
    return None


class MacLaunchAgent:
    """A launchd user agent in ``~/Library/LaunchAgents`` that runs ``tdx watch`` at login."""

    def __init__(
        self,
        *,
        repo: Path,
        log_dir: Path,
        agents_dir: Path | None = None,
        uid: int | None = None,
        runner: Runner = _run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo = repo
        self.log_dir = log_dir
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"
        self._uid = uid
        self._runner = runner
        self._which = which
        self._sleep = sleep

    @property
    def plist_path(self) -> Path:
        return self.agents_dir / f"{LABEL}.plist"

    @property
    def out_log(self) -> Path:
        return self.log_dir / OUT_LOG

    @property
    def err_log(self) -> Path:
        return self.log_dir / ERR_LOG

    def _domain(self) -> str:
        # os.getuid exists only on POSIX; it is read here, never on import.
        uid = self._uid if self._uid is not None else os.getuid()  # type: ignore[attr-defined]
        return f"gui/{uid}"

    def program_arguments(self, destination: str, extra_args: Sequence[str]) -> list[str]:
        """caffeinate -i, then the absolute uv path and ``run --project <repo> ... tdx watch``."""
        uv = self._which("uv")
        if uv is None:
            raise UvNotFoundError("uv was not found on PATH")
        arguments = [
            CAFFEINATE,
            "-i",
            # Not resolved: a Homebrew uv is a symlink whose target moves on each upgrade.
            uv,
            "run",
            "--project",
            str(self.repo),
            "ops-suite",
            "tdx",
            "watch",
        ]
        if destination != "personal":
            arguments += ["--to", destination]
        return arguments + list(extra_args)

    def plist(self, destination: str, extra_args: Sequence[str]) -> dict[str, object]:
        return {
            "Label": LABEL,
            "ProgramArguments": self.program_arguments(destination, extra_args),
            "WorkingDirectory": str(self.repo),
            "RunAtLoad": True,
            "KeepAlive": False,
            # No ProcessType: launchd's default (Standard). "Background" throttles CPU and I/O,
            # which can make Chromium start too slowly for its launch timeout.
            "StandardOutPath": str(self.out_log),
            "StandardErrorPath": str(self.err_log),
        }

    def install(self, *, destination: str, extra_args: Sequence[str]) -> Path:
        content = self.plist(destination, extra_args)  # UvNotFoundError before any write
        if self._loaded():
            self._bootout()
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self.plist_path.open("wb") as file:
            plistlib.dump(content, file)
        self._bootstrap()
        return self.plist_path

    def uninstall(self) -> bool:
        removed = False
        if self._loaded():
            self._bootout()
            removed = True
        if self.plist_path.exists():
            self.plist_path.unlink()
            removed = True
        return removed

    def status(self) -> AgentStatus:
        printed = self._print()
        loaded = printed.returncode == 0
        match = _LAST_EXIT.search(printed.stdout) if loaded else None
        return AgentStatus(
            installed=self.plist_path.exists(),
            loaded=loaded,
            last_exit_code=int(match.group(1)) if match else None,
            plist_path=self.plist_path,
            out_log=self.out_log,
            err_log=self.err_log,
        )

    def _print(self) -> subprocess.CompletedProcess[str]:
        return self._runner(["launchctl", "print", f"{self._domain()}/{LABEL}"])

    def _loaded(self) -> bool:
        return self._print().returncode == 0

    def _bootout(self) -> None:
        done = self._runner(["launchctl", "bootout", f"{self._domain()}/{LABEL}"])
        if done.returncode != 0:
            raise LaunchctlError("bootout", _reason(done))

    def _bootstrap(self) -> None:
        for attempt in range(1, BOOTSTRAP_ATTEMPTS + 1):
            done = self._runner(["launchctl", "bootstrap", self._domain(), str(self.plist_path)])
            if done.returncode == 0:
                return
            if attempt < BOOTSTRAP_ATTEMPTS:
                self._sleep(BOOTSTRAP_RETRY_SECONDS)
        raise LaunchctlError("bootstrap", _reason(done))


def _reason(done: subprocess.CompletedProcess[str]) -> str:
    """launchctl's last stderr line (it names no ticket or person), or the exit code."""
    lines = [line.strip() for line in (done.stderr or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else f"exit code {done.returncode}"


def startup_folder(environ: Mapping[str, str] = os.environ) -> Path:
    r"""The user's Startup folder: ``%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup``."""
    appdata = environ.get("APPDATA", "").strip()
    if not appdata:
        raise StartupFolderError("APPDATA is not set, so the Startup folder is unknown")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _cmd_quote(value: str) -> str:
    """``"value"`` for a batch file, with ``%`` doubled so cmd does not expand it."""
    if any(character in value for character in '"\r\n'):
        raise ValueError("a path or argument for the Startup script holds a quote or newline")
    return '"' + value.replace("%", "%%") + '"'


def _absolute_windows_path(path: str) -> str:
    """``path`` made absolute; an absolute Windows path stays as it is on any OS (on Windows
    this is ``str(Path(path).absolute())``)."""
    windows = PureWindowsPath(path)
    return str(windows if windows.is_absolute() else Path(path).absolute())


class WindowsStartup:
    """``ops-suite tdx watch.cmd`` in the user's Startup folder: ``tdx watch`` at each sign-in.

    Windows opens the script at sign-in; it starts itself again in a minimised window titled
    ``ops-suite tdx watch`` and closes the first one. The minimised copy ``cd``s to the repo and
    runs the absolute ``uv`` path with ``run ops-suite tdx watch``, its output appended to
    ``watch.out.log`` / ``watch.err.log`` in the log folder, which it creates first when it is
    missing; the window closes when ``tdx watch`` exits. ``install`` does not start it: the
    next sign-in does.

    ``repo`` and ``log_dir`` may be pure paths for ``script``; ``install`` and ``status`` read
    them as real paths.
    """

    def __init__(
        self,
        *,
        repo: PurePath,
        log_dir: PurePath,
        startup_dir: Path | None = None,
        runner: Runner = _run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.repo = repo
        self.log_dir = log_dir
        self._startup_dir = startup_dir
        self._runner = runner
        self._which = which

    @property
    def startup_dir(self) -> Path:
        return self._startup_dir if self._startup_dir is not None else startup_folder()

    @property
    def plist_path(self) -> Path:
        """The Startup script (named for the shared ``AgentStatus`` field)."""
        return self.startup_dir / STARTUP_SCRIPT

    @property
    def out_log(self) -> PurePath:
        return self.log_dir / OUT_LOG

    @property
    def err_log(self) -> PurePath:
        return self.log_dir / ERR_LOG

    def watch_arguments(self, destination: str, extra_args: Sequence[str]) -> list[str]:
        """The absolute uv path, then ``run ops-suite tdx watch`` and its options."""
        uv = self._which("uv")
        if uv is None:
            raise UvNotFoundError("uv was not found on PATH")
        arguments = [_absolute_windows_path(uv), "run", "ops-suite", "tdx", "watch"]
        if destination != "personal":
            arguments += ["--to", destination]
        return arguments + list(extra_args)

    def script(self, destination: str, extra_args: Sequence[str]) -> str:
        """The batch file's text, with CRLF line ends."""
        command = " ".join(_cmd_quote(arg) for arg in self.watch_arguments(destination, extra_args))
        log_dir = _cmd_quote(str(self.log_dir))
        lines = [
            "@echo off",
            f'rem Written by "{RUN} tdx agent install": starts "tdx watch" at every sign-in.',
            f'rem Remove it with "{RUN} tdx agent uninstall".',
            f'if not "%~1"=="{MINIMISED_FLAG}" (',
            f'  start "{WINDOW_TITLE}" /min "%~f0" {MINIMISED_FLAG}',
            "  exit /b",
            ")",
            # The log files are not a console, so Python would print in the ANSI code page.
            'set "PYTHONIOENCODING=utf-8"',
            f"cd /d {_cmd_quote(str(self.repo))}",
            # cmd cannot append to a file in a missing folder: without this line the redirect
            # fails and uv never starts.
            f"if not exist {log_dir} mkdir {log_dir}",
            f"{command} >>{_cmd_quote(str(self.out_log))} 2>>{_cmd_quote(str(self.err_log))}",
            "exit",
        ]
        if not all(line.isascii() for line in lines):
            # cmd reads a batch file in the console code page: switch it to UTF-8 first.
            lines.insert(1, "chcp 65001 >nul")
        return "\r\n".join(lines) + "\r\n"

    def install(self, *, destination: str, extra_args: Sequence[str]) -> Path:
        content = self.script(destination, extra_args)  # UvNotFoundError before any write
        path = self.plist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def uninstall(self) -> bool:
        """Remove the script; a ``tdx watch`` it already started keeps running."""
        if self.plist_path.exists():
            self.plist_path.unlink()
            return True
        return False

    def status(self) -> AgentStatus:
        running = self._running()
        return AgentStatus(
            installed=self.plist_path.exists(),
            loaded=bool(running),
            last_exit_code=None,
            plist_path=self.plist_path,
            out_log=Path(self.out_log),
            err_log=Path(self.err_log),
            running=running,
        )

    def _running(self) -> bool | None:
        """Best effort: whether a window titled ``ops-suite tdx watch`` exists, or None when
        tasklist failed. A Windows Terminal host can hide the title, so "no" is not proof."""
        title_filter = f"WINDOWTITLE eq {WINDOW_TITLE}*"
        try:
            done = self._runner(["tasklist", "/FI", title_filter, "/FO", "CSV", "/NH"])
        except OSError as error:
            log.info("tasklist failed", extra={"error": type(error).__name__})
            return None
        if done.returncode != 0:
            log.info("tasklist failed", extra={"exit_code": done.returncode})
            return None
        # A match is a CSV row; no match is one "INFO: No tasks are running ..." line.
        return any(line.startswith('"') for line in done.stdout.splitlines())


SetExecutionState = Callable[[int], int]


def _set_thread_execution_state() -> SetExecutionState:
    import ctypes  # ctypes.windll exists on Windows only, so this is read on Windows only.

    function = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return function


@contextmanager
def keep_awake(
    *, platform: str = sys.platform, set_state: SetExecutionState | None = None
) -> Iterator[bool]:
    """Ask Windows not to sleep while the block runs; the display may still turn off.

    ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`` on entry and
    ``ES_CONTINUOUS`` alone on exit: the Windows counterpart of ``caffeinate -i``. Yields
    whether the request was made. Off Windows it does nothing and yields False (the Mac agent
    has ``caffeinate``). A failed call is logged, never raised.
    """
    if platform != "win32":
        yield False
        return
    if set_state is None:
        set_state = _set_thread_execution_state()
    if not set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        log.warning("keep awake request failed")
        yield False
        return
    log.info("keep awake requested")
    try:
        yield True
    finally:
        set_state(ES_CONTINUOUS)
