import pytest

from ops_suite.common.runtime import is_store_python

STORE_PACKAGE = (
    r"C:\Program Files\WindowsApps"
    r"\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0"
)
UV_PYTHON = r"C:\Users\someone\AppData\Roaming\uv\python\cpython-3.12.11-windows-x86_64-none"
VENV_PYTHON = r"C:\Users\someone\Documents\tdx-queue-auditor\.venv\Scripts\python.exe"


@pytest.mark.parametrize(
    ("executable", "base_prefix"),
    [
        # A venv built on the Store Python, as found on a real PC.
        (VENV_PYTHON, STORE_PACKAGE),
        # The Store Python itself, started through its app execution alias.
        (r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\python3.12.exe", UV_PYTHON),
        (STORE_PACKAGE + r"\python.exe", STORE_PACKAGE),
    ],
)
def test_the_store_python_is_recognised_on_windows(executable: str, base_prefix: str) -> None:
    assert is_store_python(executable=executable, base_prefix=base_prefix, platform="win32")


def test_a_uv_managed_python_is_not_the_store_python() -> None:
    assert not is_store_python(executable=VENV_PYTHON, base_prefix=UV_PYTHON, platform="win32")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_only_windows_has_a_store_python(platform: str) -> None:
    assert not is_store_python(
        executable=STORE_PACKAGE + r"\python.exe", base_prefix=STORE_PACKAGE, platform=platform
    )
    assert not is_store_python(
        executable="/Users/someone/WindowsApps/.venv/bin/python",
        base_prefix="/opt/homebrew/opt/python@3.12/Frameworks/Python.framework/Versions/3.12",
        platform=platform,
    )


def test_it_reads_the_running_interpreter_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sys.executable", VENV_PYTHON)
    monkeypatch.setattr("sys.base_prefix", STORE_PACKAGE)

    assert is_store_python()
