"""Every user-facing hint prints the command as it runs when typed: ``uv run ops-suite ...``."""

import re
from pathlib import Path

import pytest

from ops_suite import cli
from ops_suite.common import teams
from ops_suite.common.hints import RUN
from ops_suite.tdx import watch
from ops_suite.tdx.browser import PROFILE_IN_USE_MESSAGE

SRC = Path(__file__).resolve().parents[1] / "src" / "ops_suite"
# A command in quotes (or single backticks) that does not start with "uv run".
BARE_COMMAND = re.compile(r"""(?:["']|(?<!`)`)ops-suite (?:tdx|teams)\b""")

HINTS = {
    "cli.SESSION_EXPIRED": cli.SESSION_EXPIRED,
    "cli.SET_DASHBOARD_HINT": cli.SET_DASHBOARD_HINT,
    "cli.SET_DASHBOARD_OVERRIDDEN": cli.SET_DASHBOARD_OVERRIDDEN,
    "cli.NO_WEBHOOK": cli.NO_WEBHOOK,
    "teams.SET_WEBHOOK_HINT": teams.SET_WEBHOOK_HINT,
    "watch.SET_WEBHOOK_HINT": watch.SET_WEBHOOK_HINT,
    "watch.NO_PERSONAL_FOR_NOTICES": watch.NO_PERSONAL_FOR_NOTICES,
    "teams.set_webhook_command(desk)": teams.set_webhook_command("desk"),
    "browser.PROFILE_IN_USE_MESSAGE": PROFILE_IN_USE_MESSAGE,
    "watch.sign_in_notice": watch.sign_in_notice(10),
}


def test_the_prefix_is_uv_run() -> None:
    assert RUN == "uv run ops-suite"


@pytest.mark.parametrize("name", sorted(HINTS))
def test_every_hint_constant_starts_its_command_with_uv_run(name: str) -> None:
    hint = HINTS[name]
    commands = re.findall(r"ops-suite (?:tdx|teams)", hint)
    assert commands, f"{name} names no command"
    assert hint.count(f"{RUN} tdx") + hint.count(f"{RUN} teams") == len(commands), hint


def test_no_bare_command_in_the_source() -> None:
    found = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if BARE_COMMAND.search(line)
    ]
    assert found == []
