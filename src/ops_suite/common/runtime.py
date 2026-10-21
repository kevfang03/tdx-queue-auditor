r"""Checks on the Python interpreter the tool runs on.

The Microsoft Store build of Python gets a private copy of ``%LOCALAPPDATA%`` from Windows: a
folder that Python creates there lands in
``...\Packages\PythonSoftwareFoundation.Python...\LocalCache\Local``, where Python still sees it
but other programs (the Startup script's cmd, Explorer, folder access rules) do not.
For this tool that hidden copy holds the browser profile with the TDX session cookies, so live
commands refuse to run on it; ``uv sync`` builds the venv on a uv-managed Python.
"""

import sys

STORE_PACKAGE = "pythonsoftwarefoundation.python."
WINDOWS_APPS = "\\windowsapps\\"


def is_store_python(
    *,
    executable: str | None = None,
    base_prefix: str | None = None,
    platform: str | None = None,
) -> bool:
    """Whether this is the Microsoft Store Python: on Windows, ``sys.executable`` or
    ``sys.base_prefix`` is under a ``WindowsApps`` folder or names the Store package.

    A venv's ``sys.executable`` is the venv's own copy, so a venv built on the Store Python is
    recognised by its ``sys.base_prefix``. Each argument defaults to the running interpreter's.
    """
    if (sys.platform if platform is None else platform) != "win32":
        return False
    candidates = (
        sys.executable if executable is None else executable,
        sys.base_prefix if base_prefix is None else base_prefix,
    )
    for path in candidates:
        folded = path.replace("/", "\\").lower()
        if STORE_PACKAGE in folded or WINDOWS_APPS in folded:
            return True
    return False
