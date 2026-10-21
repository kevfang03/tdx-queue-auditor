from pathlib import Path

import platformdirs
import pytest
from pydantic import ValidationError

from ops_suite.common import paths
from ops_suite.common.config import load_settings


def test_defaults_load_with_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPS_SUITE_PATHS__DATA_DIR")
    monkeypatch.delenv("OPS_SUITE_PATHS__LOG_DIR")

    settings = load_settings()

    assert settings.paths.data_dir is None
    assert settings.paths.log_dir is None
    assert settings.mail.work.mailbox_address == ""
    assert settings.mail.work.drop_dir is None
    assert settings.mail.work.retention_days == 7
    assert settings.mail.work.automated_senders == []
    assert "no-reply" in settings.mail.work.automated_local_parts
    assert "postmaster" in settings.mail.work.automated_local_parts


def test_default_paths_come_from_platformdirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPS_SUITE_PATHS__DATA_DIR")
    monkeypatch.delenv("OPS_SUITE_PATHS__LOG_DIR")

    settings = load_settings()

    assert paths.data_dir(settings) == platformdirs.user_data_path("ops-suite", appauthor=False)
    assert paths.log_dir(settings) == platformdirs.user_log_path("ops-suite", appauthor=False)


def test_toml_values_load(test_config: Path) -> None:
    settings = load_settings(test_config)

    assert settings.mail.work.mailbox_address == "shared-mailbox@example.edu"
    assert settings.mail.work.automated_senders == ["notifications@tdx.example.edu"]
    assert settings.mail.work.retention_days == 7


def test_config_file_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "from-env.toml"
    config.write_text("[mail.work]\nretention_days = 3\n", encoding="utf-8")
    monkeypatch.setenv("OPS_SUITE_CONFIG", str(config))

    assert load_settings().mail.work.retention_days == 3


def test_environment_overrides_toml(test_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SUITE_MAIL__WORK__MAILBOX_ADDRESS", "other-desk@example.edu")

    settings = load_settings(test_config)

    assert settings.mail.work.mailbox_address == "other-desk@example.edu"
    # Keys the environment does not set still come from the file.
    assert settings.mail.work.automated_senders == ["notifications@tdx.example.edu"]


def test_dotenv_sits_between_environment_and_toml(
    tmp_path: Path, test_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "OPS_SUITE_MAIL__WORK__MAILBOX_ADDRESS=dotenv-desk@example.edu\n"
        "OPS_SUITE_MAIL__WORK__RETENTION_DAYS=5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPS_SUITE_MAIL__WORK__RETENTION_DAYS", "9")

    settings = load_settings(test_config)

    assert settings.mail.work.mailbox_address == "dotenv-desk@example.edu"
    assert settings.mail.work.retention_days == 9


def test_retention_days_zero_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[mail.work]\nretention_days = 0\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_settings(config)


def test_empty_path_strings_mean_not_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPS_SUITE_PATHS__DATA_DIR")
    monkeypatch.delenv("OPS_SUITE_PATHS__LOG_DIR")
    config = tmp_path / "blank.toml"
    config.write_text(
        '[paths]\ndata_dir = ""\nlog_dir = ""\n\n[mail.work]\ndrop_dir = ""\n', encoding="utf-8"
    )

    settings = load_settings(config)

    assert settings.paths.data_dir is None
    assert settings.paths.log_dir is None
    assert settings.mail.work.drop_dir is None


def test_path_overrides_and_profile_dir(tmp_path: Path) -> None:
    settings = load_settings()

    assert paths.data_dir(settings) == tmp_path / "appdata"
    assert paths.log_dir(settings) == tmp_path / "applogs"
    assert paths.profile_dir(settings, "work") == tmp_path / "appdata" / "work"
    assert not (tmp_path / "appdata").exists()


def test_tdx_settings_and_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()

    assert settings.tdx.base_url == ""
    assert settings.tdx.profile_dir is None
    assert paths.tdx_dir(settings) == tmp_path / "appdata" / "tdx"
    assert paths.tdx_profile_dir(settings) == tmp_path / "appdata" / "tdx" / "browser-profile"
    assert paths.tdx_captures_dir(settings) == tmp_path / "appdata" / "tdx" / "captures"

    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", "https://tdx.example.edu/TDNext/")
    monkeypatch.setenv("OPS_SUITE_TDX__PROFILE_DIR", str(tmp_path / "profile"))
    settings = load_settings()

    assert settings.tdx.base_url == "https://tdx.example.edu/TDNext/"
    assert paths.tdx_profile_dir(settings) == tmp_path / "profile"


def test_example_config_loads() -> None:
    example = Path(__file__).parent.parent.parent / "config.example.toml"

    settings = load_settings(example)

    assert settings.tdx.base_url == "https://tdx.example.edu/TDWorkManagement/"
    assert settings.tdx.profile_dir is None
    assert settings.tdx.dashboard_url == ""
    assert settings.tdx.headless is True
    assert settings.tdx.allowed_requests == []
    assert settings.tdx.widgets.claimed_title == "Active Tickets Claimed by Others in My Group(s)"


def test_tdx_rows_settings_defaults() -> None:
    tdx = load_settings().tdx

    assert tdx.display_timezone == "America/New_York"
    assert tdx.headless is True
    assert tdx.max_pages_per_widget == 20
    assert tdx.page_timeout_seconds == 30
    assert tdx.allowed_requests == []
    assert tdx.widgets.unclaimed_title == "Unclaimed Tickets in My Group(s)"


def test_tdx_rows_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SUITE_TDX__HEADLESS", "false")
    monkeypatch.setenv("OPS_SUITE_TDX__ALLOWED_REQUESTS", '["POST /TDWorkManagement/x/#"]')
    monkeypatch.setenv("OPS_SUITE_TDX__WIDGETS__CLAIMED_TITLE", "Claimed Elsewhere")

    tdx = load_settings().tdx

    assert tdx.headless is False
    assert tdx.allowed_requests == ["POST /TDWorkManagement/x/#"]
    assert tdx.widgets.claimed_title == "Claimed Elsewhere"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("display_timezone", '"Mars/Olympus_Mons"'),
        ("max_pages_per_widget", "0"),
        ("max_pages_per_widget", "101"),
        ("page_timeout_seconds", "4"),
        ("page_timeout_seconds", "121"),
        ("allowed_requests", '["GET /TDWorkManagement/x"]'),
        ("allowed_requests", '["POST"]'),
        ("allowed_requests", '["POST TDWorkManagement/no-slash"]'),
    ],
)
def test_invalid_tdx_rows_settings_are_refused(tmp_path: Path, key: str, value: str) -> None:
    config = tmp_path / "bad.toml"
    config.write_text(f"[tdx]\n{key} = {value}\n", encoding="utf-8")

    with pytest.raises(ValidationError) as error:
        load_settings(config)

    assert f"tdx.{key}" in str(error.value)


def test_tdx_audit_settings_defaults_and_example() -> None:
    example = Path(__file__).parent.parent.parent / "config.example.toml"

    for tdx in (load_settings().tdx, load_settings(example).tdx):
        assert tdx.feed_delay_seconds == 2
        assert tdx.max_feeds_per_run == 40
        assert tdx.ticket_url_template == ""
        assert (tdx.rules.stale_hours, tdx.rules.event_over_hours) == (48, 24)
        assert tdx.rules.event_tbd_days == 3
        assert tdx.rules.extra_staff_names == []
        assert tdx.rules.desk_memory is True
        assert tdx.rules.desk_memory_forget_days == 180


def test_tdx_rules_from_the_environment_and_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SUITE_TDX__RULES__STALE_HOURS", "72")
    monkeypatch.setenv("OPS_SUITE_TDX__RULES__EXTRA_STAFF_NAMES", '["Alex Example"]')

    tdx = load_settings().tdx

    assert tdx.rules.stale_hours == 72
    assert tdx.rules.extra_staff_names == ["Alex Example"]
    # Only the number of names is ever shown.
    for shown in (repr(tdx), str(tdx), repr(tdx.rules)):
        assert "Alex Example" not in shown
    assert "extra_staff_names=1" in repr(tdx.rules)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("tdx", "feed_delay_seconds", "-1"),
        ("tdx", "feed_delay_seconds", "31"),
        ("tdx", "max_feeds_per_run", "0"),
        ("tdx", "max_feeds_per_run", "201"),
        ("tdx.rules", "stale_hours", "0"),
        ("tdx.rules", "stale_hours", "721"),
        ("tdx.rules", "desk_memory_forget_days", "29"),
        ("tdx.rules", "desk_memory_forget_days", "731"),
        ("tdx.rules", "event_over_hours", "0"),
        ("tdx.rules", "event_tbd_days", "-1"),
        ("tdx.rules", "event_tbd_days", "61"),
    ],
)
def test_invalid_tdx_audit_settings_are_refused(
    tmp_path: Path, section: str, key: str, value: str
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text(f"[{section}]\n{key} = {value}\n", encoding="utf-8")

    with pytest.raises(ValidationError) as error:
        load_settings(config)

    assert f"{section}.{key}" in str(error.value)


def test_tdx_event_defaults_and_example_match_both_college_names() -> None:
    """The college is now the SCIS (formerly CCI): each default naming it has both forms."""
    example = Path(__file__).parent.parent.parent / "config.example.toml"

    for events in (load_settings().tdx.events, load_settings(example).tdx.events):
        assert events.form_titles == ["CCI Event Request Form", "SCIS Event Request Form"]
        assert "CCI Operational Assistance" in events.known_option_prefixes
        assert "SCIS Operational Assistance" in events.known_option_prefixes
        for prefix in (*events.marketing_only_prefixes, *events.av_service_prefixes):
            assert "CCI" not in prefix.split() and "SCIS" not in prefix.split()


def test_teams_destination_settings_defaults_and_example() -> None:
    example = Path(__file__).parent.parent.parent / "config.example.toml"

    for teams in (load_settings().teams, load_settings(example).teams):
        assert teams.personal.show_titles is True
        assert (teams.desk.approved, teams.desk.show_titles) == (False, False)
        assert (teams.show_titles("personal"), teams.show_titles("desk")) == (True, False)


def test_teams_desk_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SUITE_TEAMS__DESK__SHOW_TITLES", "true")

    teams = load_settings().teams

    assert teams.show_titles("desk") is True
    assert teams.desk.approved is False
