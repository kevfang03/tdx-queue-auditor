"""profile_in_use without a browser: lock files only."""

import os
import socket
import sys
from pathlib import Path

import pytest

from ops_suite.tdx.browser import profile_in_use

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SingletonLock is macOS/Linux")


def test_a_profile_without_locks_is_free(tmp_path: Path) -> None:
    assert not profile_in_use(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="lockfile is Windows only")
def test_a_lockfile_nobody_holds_is_free(tmp_path: Path) -> None:
    (tmp_path / "lockfile").write_text("", encoding="utf-8")

    assert not profile_in_use(tmp_path)


@posix_only
def test_a_singleton_lock_of_a_live_process_is_in_use(tmp_path: Path) -> None:
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", tmp_path / "SingletonLock")

    assert profile_in_use(tmp_path)


@posix_only
def test_a_stale_singleton_lock_is_free(tmp_path: Path) -> None:
    os.symlink(f"{socket.gethostname()}-999999999", tmp_path / "SingletonLock")

    assert not profile_in_use(tmp_path)


@posix_only
def test_a_singleton_lock_from_another_host_is_in_use(tmp_path: Path) -> None:
    os.symlink("other-host.example.edu-1234", tmp_path / "SingletonLock")

    assert profile_in_use(tmp_path)
