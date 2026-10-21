from collections.abc import Iterator
from pathlib import Path

import pytest

from ops_suite.tdx.demo.app import FolderSource, WorkMgmtServer, serve

# The tests' widget grids and ticket feeds; the app's own pages are in the package.
WORKMGMT = Path(__file__).parent.parent / "fixtures" / "tdx_synthetic" / "workmgmt"


@pytest.fixture
def workmgmt_server() -> Iterator[WorkMgmtServer]:
    """Serve the synthetic TDWorkManagement app on a free port of 127.0.0.1."""
    with serve(FolderSource(WORKMGMT)) as server:
        yield server
