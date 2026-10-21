"""Where the suite keeps its files. Nothing here creates a folder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

if TYPE_CHECKING:
    from ops_suite.common.config import Settings

APP_NAME = "ops-suite"


def default_config_path() -> Path:
    """Config file used when neither ``--config`` nor ``OPS_SUITE_CONFIG`` is given."""
    return platformdirs.user_config_path(APP_NAME, appauthor=False) / "config.toml"


def data_dir(settings: Settings) -> Path:
    return settings.paths.data_dir or platformdirs.user_data_path(APP_NAME, appauthor=False)


def log_dir(settings: Settings) -> Path:
    return settings.paths.log_dir or platformdirs.user_log_path(APP_NAME, appauthor=False)


def profile_dir(settings: Settings, profile: str) -> Path:
    """Folder for one mail profile. Profiles never share files."""
    return data_dir(settings) / profile


def tdx_dir(settings: Settings) -> Path:
    return data_dir(settings) / "tdx"


def tdx_profile_dir(settings: Settings) -> Path:
    """The TDX browser profile. It holds live session cookies: print its path, nothing else."""
    return settings.tdx.profile_dir or tdx_dir(settings) / "browser-profile"


def tdx_captures_dir(settings: Settings) -> Path:
    return tdx_dir(settings) / "captures"


def tdx_dashboard_file(settings: Settings) -> Path:
    """The dashboard address saved by ``tdx set-dashboard``; account-specific, never logged."""
    return tdx_dir(settings) / "dashboard.json"


def llm_dir(settings: Settings) -> Path:
    """The LLM client's files (``llm-usage.json``: daily token counts only, never text)."""
    return data_dir(settings) / "llm"
