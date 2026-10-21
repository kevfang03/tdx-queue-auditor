import plistlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

import pytest

from ops_suite.common import autostart
from ops_suite.common.autostart import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    LABEL,
    STARTUP_SCRIPT,
    LaunchctlError,
    MacLaunchAgent,
    StartupFolderError,
    UvNotFoundError,
    WindowsStartup,
    for_platform,
    keep_awake,
    startup_folder,
)

UV = "/Users/owner/.local/bin/uv"
REPO = Path("/Users/owner/Documents/GitHub/tdx-queue-auditor")
PRINTED = (
    "gui/501/com.ops-suite.tdx-watch = {\n"
    "\tactive count = 1\n"
    "\tstate = running\n"
    "\tlast exit code = 6\n"
    "}\n"
)


class FakeLaunchctl:
    """Stands in for launchctl: one job that is loaded or not, and every call recorded."""

    def __init__(self, *, loaded: bool = False, bootstrap_failures: int = 0) -> None:
        self.loaded = loaded
        self.bootstrap_failures = bootstrap_failures
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        verb = args[1]
        if verb == "print":
            if self.loaded:
                return subprocess.CompletedProcess(args, 0, PRINTED, "")
            return subprocess.CompletedProcess(args, 113, "", "Could not find service\n")
        if verb == "bootout":
            self.loaded = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "bootstrap":
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return subprocess.CompletedProcess(
                    args, 5, "", "Bootstrap failed: 5: Input/output error\n"
                )
            self.loaded = True
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected launchctl call: {args}")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


def _agent(
    tmp_path: Path, launchctl: FakeLaunchctl, *, uv: str | None = UV
) -> tuple[MacLaunchAgent, list[float]]:
    sleeps: list[float] = []
    agent = MacLaunchAgent(
        repo=REPO,
        log_dir=tmp_path / "logs",
        agents_dir=tmp_path / "LaunchAgents",
        uid=501,
        runner=launchctl,
        which=lambda name: uv if name == "uv" else None,
        sleep=sleeps.append,
    )
    return agent, sleeps


def _written(agent: MacLaunchAgent) -> dict:
    with agent.plist_path.open("rb") as file:
        return plistlib.load(file)


def test_the_plist_starts_tdx_watch_under_caffeinate_and_never_restarts_it(
    tmp_path: Path,
) -> None:
    agent, _ = _agent(tmp_path, FakeLaunchctl())

    path = agent.install(destination="personal", extra_args=[])

    assert path == tmp_path / "LaunchAgents" / "com.ops-suite.tdx-watch.plist"
    assert _written(agent) == {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-i",
            UV,
            "run",
            "--project",
            str(REPO),
            "ops-suite",
            "tdx",
            "watch",
        ],
        "WorkingDirectory": str(REPO),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(tmp_path / "logs" / "watch.out.log"),
        "StandardErrorPath": str(tmp_path / "logs" / "watch.err.log"),
    }
    assert (tmp_path / "logs").is_dir()


def test_the_plist_leaves_the_process_type_at_launchds_default(tmp_path: Path) -> None:
    # "Background" throttles CPU and I/O, and Chromium may then miss its launch timeout.
    agent, _ = _agent(tmp_path, FakeLaunchctl())

    agent.install(destination="desk", extra_args=["--headed"])

    assert "ProcessType" not in _written(agent)


def test_the_plist_adds_to_for_the_desk_and_the_extra_args(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, FakeLaunchctl())

    agent.install(destination="desk", extra_args=["--headed"])

    assert _written(agent)["ProgramArguments"][-4:] == ["watch", "--to", "desk", "--headed"]


def test_install_bootstraps_the_agent_in_the_gui_domain(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    agent, _ = _agent(tmp_path, launchctl)

    agent.install(destination="personal", extra_args=[])

    assert launchctl.calls == [
        ["launchctl", "print", f"gui/501/{LABEL}"],
        ["launchctl", "bootstrap", "gui/501", str(agent.plist_path)],
    ]
    assert launchctl.loaded


def test_install_unloads_a_loaded_agent_before_replacing_it(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl(loaded=True)
    agent, _ = _agent(tmp_path, launchctl)

    agent.install(destination="personal", extra_args=[])

    assert launchctl.verbs() == ["print", "bootout", "bootstrap"]
    assert launchctl.calls[1] == ["launchctl", "bootout", f"gui/501/{LABEL}"]


def test_install_tries_the_bootstrap_again_after_an_io_error(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl(loaded=True, bootstrap_failures=1)
    agent, sleeps = _agent(tmp_path, launchctl)

    agent.install(destination="personal", extra_args=[])

    assert launchctl.verbs() == ["print", "bootout", "bootstrap", "bootstrap"]
    assert sleeps == [autostart.BOOTSTRAP_RETRY_SECONDS]


def test_install_reports_launchctls_reason_when_the_bootstrap_keeps_failing(
    tmp_path: Path,
) -> None:
    launchctl = FakeLaunchctl(bootstrap_failures=autostart.BOOTSTRAP_ATTEMPTS)
    agent, _ = _agent(tmp_path, launchctl)

    with pytest.raises(LaunchctlError) as caught:
        agent.install(destination="personal", extra_args=[])

    assert caught.value.action == "bootstrap"
    assert caught.value.reason == "Bootstrap failed: 5: Input/output error"
    assert launchctl.verbs().count("bootstrap") == autostart.BOOTSTRAP_ATTEMPTS


def test_install_without_uv_writes_and_loads_nothing(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    agent, _ = _agent(tmp_path, launchctl, uv=None)

    with pytest.raises(UvNotFoundError):
        agent.install(destination="personal", extra_args=[])

    assert not agent.plist_path.exists()
    assert launchctl.calls == []


def test_uninstall_unloads_and_removes_the_agent(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    agent, _ = _agent(tmp_path, launchctl)
    agent.install(destination="personal", extra_args=[])
    launchctl.calls.clear()

    assert agent.uninstall() is True

    assert launchctl.verbs() == ["print", "bootout"]
    assert not agent.plist_path.exists()
    assert not launchctl.loaded


def test_uninstall_removes_a_plist_that_is_not_loaded(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    agent, _ = _agent(tmp_path, launchctl)
    agent.install(destination="personal", extra_args=[])
    launchctl.loaded = False
    launchctl.calls.clear()

    assert agent.uninstall() is True

    assert launchctl.verbs() == ["print"]
    assert not agent.plist_path.exists()


def test_uninstall_with_nothing_installed_returns_false(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, FakeLaunchctl())

    assert agent.uninstall() is False


def test_status_reads_loaded_and_the_last_exit_code_from_launchctl_print(
    tmp_path: Path,
) -> None:
    launchctl = FakeLaunchctl()
    agent, _ = _agent(tmp_path, launchctl)
    agent.install(destination="personal", extra_args=[])

    status = agent.status()

    assert status.installed and status.loaded
    assert status.last_exit_code == 6
    assert status.plist_path == agent.plist_path
    assert status.out_log == tmp_path / "logs" / "watch.out.log"
    assert status.err_log == tmp_path / "logs" / "watch.err.log"


def test_status_of_a_job_that_never_exited_has_no_exit_code(tmp_path: Path) -> None:
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        printed = "\tlast exit code = (never exited)\n"
        return subprocess.CompletedProcess(list(args), 0, printed, "")

    agent = MacLaunchAgent(repo=REPO, log_dir=tmp_path, agents_dir=tmp_path, uid=501, runner=runner)

    status = agent.status()

    assert status.loaded and not status.installed
    assert status.last_exit_code is None


def test_status_with_nothing_installed(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, FakeLaunchctl())

    status = agent.status()

    assert not status.installed and not status.loaded
    assert status.last_exit_code is None


def test_for_platform_has_nothing_on_linux(tmp_path: Path) -> None:
    assert for_platform(repo=REPO, log_dir=tmp_path, platform="linux") is None


def test_for_platform_on_macos_is_the_launch_agent(tmp_path: Path) -> None:
    agent = for_platform(repo=REPO, log_dir=tmp_path, platform="darwin")

    assert isinstance(agent, MacLaunchAgent)
    assert agent.plist_path.name == f"{LABEL}.plist"


# --- Windows: the Startup-folder script --------------------------------------------------

# Every path the script embeds is a Windows path on any OS, so the expected text is the same on
# the Linux and macOS runners. Writing the script uses real tmp_path folders.
WIN_UV = r"C:\Users\Desk Owner\.local\bin\uv.exe"
WIN_REPO = PureWindowsPath(r"C:\Users\Desk Owner\Documents\tdx-queue-auditor")
WIN_LOGS = PureWindowsPath(r"C:\Users\Desk Owner\AppData\Local\ops-suite\Logs")
NO_MATCH = "INFO: No tasks are running which match the specified criteria.\r\n"
MATCH = '"cmd.exe","4242","Console","1","4,000 K"\r\n'


class FakeTasklist:
    """Stands in for tasklist: the window is there or not, or tasklist fails."""

    def __init__(self, *, running: bool = False, fails: bool = False) -> None:
        self.running = running
        self.fails = fails
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if self.fails:
            return subprocess.CompletedProcess(args, 1, "", "ERROR: Invalid argument.\r\n")
        return subprocess.CompletedProcess(args, 0, MATCH if self.running else NO_MATCH, "")


def _startup(
    tmp_path: Path, tasklist: FakeTasklist | None = None, *, uv: str | None = WIN_UV
) -> WindowsStartup:
    return WindowsStartup(
        repo=WIN_REPO,
        log_dir=tmp_path / "logs",
        startup_dir=tmp_path / "Startup",
        runner=tasklist or FakeTasklist(),
        which=lambda name: uv if name == "uv" else None,
    )


def _lines(script: str) -> list[str]:
    assert script.endswith("\r\n")
    assert "\n" not in script.replace("\r\n", "")  # CRLF line ends only
    return script.split("\r\n")[:-1]


def _script_lines(startup: WindowsStartup) -> list[str]:
    return _lines(startup.plist_path.read_bytes().decode("utf-8"))


def _windows_script(
    *,
    repo: PureWindowsPath = WIN_REPO,
    log_dir: PureWindowsPath = WIN_LOGS,
    uv: str = WIN_UV,
    destination: str = "personal",
    extra_args: Sequence[str] = (),
) -> list[str]:
    """The script's lines for Windows paths, built on any OS; nothing is written."""
    startup = WindowsStartup(repo=repo, log_dir=log_dir, which=lambda name: uv)
    return _lines(startup.script(destination, extra_args))


def test_the_script_runs_absolute_uv_in_the_repo_minimised_with_logs() -> None:
    assert _windows_script() == [
        "@echo off",
        'rem Written by "uv run ops-suite tdx agent install": starts "tdx watch" at every sign-in.',
        'rem Remove it with "uv run ops-suite tdx agent uninstall".',
        'if not "%~1"=="--minimised" (',
        '  start "ops-suite tdx watch" /min "%~f0" --minimised',
        "  exit /b",
        ")",
        'set "PYTHONIOENCODING=utf-8"',
        r'cd /d "C:\Users\Desk Owner\Documents\tdx-queue-auditor"',
        f'if not exist "{WIN_LOGS}" mkdir "{WIN_LOGS}"',
        f'"{WIN_UV}" "run" "ops-suite" "tdx" "watch" '
        f'>>"{WIN_LOGS / "watch.out.log"}" 2>>"{WIN_LOGS / "watch.err.log"}"',
        "exit",
    ]


def test_install_writes_the_script_to_the_startup_folder_and_makes_the_log_folder(
    tmp_path: Path,
) -> None:
    startup = _startup(tmp_path)

    path = startup.install(destination="personal", extra_args=[])

    assert path == tmp_path / "Startup" / "ops-suite tdx watch.cmd"
    assert _script_lines(startup) == _lines(startup.script("personal", []))
    assert (tmp_path / "logs").is_dir()  # cmd cannot append to a log in a missing folder


def test_a_relative_uv_path_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    startup = WindowsStartup(repo=WIN_REPO, log_dir=WIN_LOGS, which=lambda name: "uv.exe")

    assert startup.watch_arguments("personal", [])[0] == str(Path.cwd() / "uv.exe")


def test_the_minimised_copy_makes_the_log_folder_before_the_redirect() -> None:
    # A case seen on a real PC: the folder is missing when the script runs at sign-in,
    # so cmd's ">>" fails and uv never starts, unless the script makes the folder first.
    lines = _windows_script(destination="desk")
    logs = WIN_LOGS
    relaunch = lines.index('  start "ops-suite tdx watch" /min "%~f0" --minimised')
    branch_end = lines.index(")")
    guard = lines.index(f'if not exist "{logs}" mkdir "{logs}"')
    redirect = next(i for i, line in enumerate(lines) if f'>>"{logs / "watch.out.log"}"' in line)
    # Only the minimised copy runs it: the first copy re-launches and exits before it.
    assert relaunch < branch_end < guard == redirect - 1


def test_the_log_folder_guard_doubles_a_percent_sign() -> None:
    lines = _windows_script(log_dir=PureWindowsPath(r"C:\Users\Desk Owner\ops 100%\Logs"))

    folder = r'"C:\Users\Desk Owner\ops 100%%\Logs"'
    assert f"if not exist {folder} mkdir {folder}" in lines


@pytest.mark.skipif(sys.platform != "win32", reason="runs the script in cmd")
def test_cmd_starts_uv_when_the_log_folder_is_missing(tmp_path: Path) -> None:
    fake_uv = tmp_path / "bin" / "uv.cmd"
    fake_uv.parent.mkdir()
    fake_uv.write_bytes(b"@echo off\r\necho fake uv %*\r\necho fake error 1>&2\r\n")
    logs = tmp_path / "Local" / "ops 100%" / "Logs"
    startup = WindowsStartup(
        repo=tmp_path,
        log_dir=logs,
        startup_dir=tmp_path / "Startup",
        runner=FakeTasklist(),
        which=lambda name: str(fake_uv),
    )
    startup.install(destination="desk", extra_args=[])
    shutil.rmtree(tmp_path / "Local")

    # Run as the minimised copy: the first copy only re-launches the script with this flag.
    done = subprocess.run(
        ["cmd", "/c", str(startup.plist_path), "--minimised"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert (logs / "watch.out.log").read_text().strip() == (
        'fake uv "run" "ops-suite" "tdx" "watch" "--to" "desk"'
    )
    assert (logs / "watch.err.log").read_text().strip() == "fake error"


def test_the_script_adds_to_and_the_extra_args() -> None:
    config = r"C:\Users\Desk Owner\ops 100%\config.toml"

    lines = _windows_script(destination="desk", extra_args=["--headed", "--config", config])

    assert lines[10] == (
        r'"C:\Users\Desk Owner\.local\bin\uv.exe" "run" "ops-suite" "tdx" "watch" "--to" "desk" '
        r'"--headed" "--config" "C:\Users\Desk Owner\ops 100%%\config.toml" '
        r'>>"C:\Users\Desk Owner\AppData\Local\ops-suite\Logs\watch.out.log" '
        r'2>>"C:\Users\Desk Owner\AppData\Local\ops-suite\Logs\watch.err.log"'
    )


def test_the_script_switches_cmd_to_utf8_for_a_non_ascii_path() -> None:
    lines = _windows_script(repo=PureWindowsPath("C:\\Users\\Jos\u00e9\\tdx-queue-auditor"))

    assert lines[:2] == ["@echo off", "chcp 65001 >nul"]
    assert 'cd /d "C:\\Users\\Jos\u00e9\\tdx-queue-auditor"' in lines


def test_an_ascii_script_leaves_the_code_page_alone() -> None:
    assert not any("chcp" in line for line in _windows_script())


def test_install_without_uv_writes_nothing_on_windows(tmp_path: Path) -> None:
    startup = _startup(tmp_path, uv=None)

    with pytest.raises(UvNotFoundError):
        startup.install(destination="personal", extra_args=[])
    assert not (tmp_path / "Startup").exists()


def test_a_quote_in_an_argument_is_refused_before_writing(tmp_path: Path) -> None:
    startup = _startup(tmp_path)

    with pytest.raises(ValueError):
        startup.install(destination="personal", extra_args=["--config", 'a"b'])
    assert not startup.plist_path.exists()


def test_windows_install_status_and_uninstall(tmp_path: Path) -> None:
    tasklist = FakeTasklist()
    startup = _startup(tmp_path, tasklist)

    status = startup.status()
    assert not status.installed and not status.loaded and status.running is False

    startup.install(destination="personal", extra_args=[])
    tasklist.running = True
    status = startup.status()
    assert status.installed and status.loaded and status.running is True
    assert status.last_exit_code is None
    assert status.plist_path == startup.plist_path
    assert status.out_log == tmp_path / "logs" / "watch.out.log"
    assert status.err_log == tmp_path / "logs" / "watch.err.log"
    # tasklist gets an argument list, never a shell string.
    assert tasklist.calls[-1] == [
        "tasklist",
        "/FI",
        "WINDOWTITLE eq ops-suite tdx watch*",
        "/FO",
        "CSV",
        "/NH",
    ]

    assert startup.uninstall() is True
    assert not startup.plist_path.exists()
    assert startup.uninstall() is False


def test_windows_status_is_unknown_when_tasklist_fails(tmp_path: Path) -> None:
    assert _startup(tmp_path, FakeTasklist(fails=True)).status().running is None

    def missing(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tasklist")

    startup = WindowsStartup(repo=WIN_REPO, log_dir=tmp_path, startup_dir=tmp_path, runner=missing)
    status = startup.status()
    assert status.running is None and not status.loaded


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_install_creates_the_log_folder(tmp_path: Path, platform: str) -> None:
    logs = tmp_path / "Local" / "ops-suite" / "Logs"
    agent: MacLaunchAgent | WindowsStartup
    if platform == "darwin":
        agent, _ = _agent(tmp_path, FakeLaunchctl())
    else:
        agent = _startup(tmp_path)
    agent.log_dir = logs

    agent.install(destination="desk", extra_args=[])

    assert logs.is_dir()


def test_startup_folder_comes_from_appdata() -> None:
    folder = startup_folder({"APPDATA": r"C:\Users\someone\AppData\Roaming"})

    assert folder == (
        Path(r"C:\Users\someone\AppData\Roaming")
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    with pytest.raises(StartupFolderError):
        startup_folder({})


def test_for_platform_on_windows_is_the_startup_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    agent = for_platform(repo=REPO, log_dir=tmp_path, platform="win32")

    assert isinstance(agent, WindowsStartup)
    startup = tmp_path / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    assert agent.plist_path == startup / "Startup" / STARTUP_SCRIPT


# --- keep_awake --------------------------------------------------------------------------


def test_keep_awake_asks_windows_and_clears_the_request_on_exit() -> None:
    calls: list[int] = []

    def set_state(flags: int) -> int:
        calls.append(flags)
        return ES_CONTINUOUS  # the previous state: non-zero means success

    with keep_awake(platform="win32", set_state=set_state) as requested:
        assert requested is True
        assert calls == [0x80000000 | 0x00000001]
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, 0x80000000]


def test_keep_awake_clears_the_request_when_the_block_raises() -> None:
    calls: list[int] = []

    def set_state(flags: int) -> int:
        calls.append(flags)
        return 1

    with pytest.raises(KeyboardInterrupt), keep_awake(platform="win32", set_state=set_state):
        raise KeyboardInterrupt
    assert calls[-1] == ES_CONTINUOUS


def test_keep_awake_carries_on_when_the_call_fails() -> None:
    calls: list[int] = []

    def set_state(flags: int) -> int:
        calls.append(flags)
        return 0

    with keep_awake(platform="win32", set_state=set_state) as requested:
        assert requested is False
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]  # nothing to clear


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_keep_awake_does_nothing_off_windows(platform: str) -> None:
    def set_state(flags: int) -> int:
        raise AssertionError("the Windows API may not be called")

    with keep_awake(platform=platform, set_state=set_state) as requested:
        assert requested is False
