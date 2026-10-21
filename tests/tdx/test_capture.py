from datetime import datetime
from pathlib import Path

import pytest

from ops_suite.cli import main
from ops_suite.tdx.browser import FrameHtml, RequestRecord
from ops_suite.tdx.capture import write_capture

SYNTHETIC = Path(__file__).parent.parent / "fixtures" / "tdx_synthetic"
BASE = "https://tdx.example.edu"
NOW = datetime(2026, 10, 3, 14, 5, 9)


def _frames() -> list[FrameHtml]:
    def read(name: str) -> str:
        return (SYNTHETIC / name).read_text(encoding="utf-8")

    return [
        FrameHtml(
            0, 0, f"{BASE}/TDNext/Home/Desktop/Default.aspx?user=alex", read("dashboard.html")
        ),
        FrameHtml(0, 1, f"{BASE}/TDNext/widget.html?ReportID=88125", read("widget.html")),
        FrameHtml(
            1, 0, f"{BASE}/TDNext/Apps/43/Tickets/TicketDet?TicketID=3119232", read("ticket.html")
        ),
    ]


def _requests() -> list[RequestRecord]:
    return [
        RequestRecord("GET", "document", f"{BASE}/TDNext/Home/Desktop/Default.aspx?user=alex"),
        RequestRecord("POST", "fetch", f"{BASE}/TDNext/api/tickets.json?page=2&ticket=3119232"),
        RequestRecord("POST", "fetch", f"{BASE}/TDNext/api/tickets.json?page=3&ticket=3119232"),
        RequestRecord("GET", "document", f"{BASE}/TDNext/widget.html?ReportID=88125"),
        RequestRecord("GET", "image", f"{BASE}/TDNext/img/3119232.png"),
    ]


def test_capture_folder_and_files(tmp_path: Path) -> None:
    folder = write_capture(tmp_path / "captures", "dashboard", _frames(), _requests(), NOW)

    assert folder == tmp_path / "captures" / "dashboard-20261003-140509"
    assert sorted(path.name for path in folder.iterdir()) == [
        "README.txt",
        "frame-0-0.html",
        "frame-0-1.html",
        "frame-1-0.html",
        "frames.txt",
        "kept-text.txt",
        "requests.txt",
    ]
    assert "<th>REQUESTOR</th>" in (folder / "frame-0-1.html").read_text(encoding="utf-8")

    frames = (folder / "frames.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[:2] for line in frames] == [
        ["0-0", "tdx.example.edu/TDNext/Home/Desktop/Default.aspx"],
        ["0-1", "tdx.example.edu/TDNext/widget.html"],
        ["1-0", "tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet"],
    ]
    assert all(line.endswith(" KB") for line in frames)

    kept = (folder / "kept-text.txt").read_text(encoding="utf-8").splitlines()
    # De-duplicated across frames, in first-seen order.
    assert kept[:5] == ["Modified", "ID", "Title", "Prim Resp", "Status"]
    assert kept.count("ID") == 1
    assert "REQUESTOR" in kept and "Created" in kept

    readme = (folder / "README.txt").read_text(encoding="utf-8")
    assert "dashboard" in readme and "kept-text.txt" in readme


def test_requests_file_has_counts_and_no_queries(tmp_path: Path) -> None:
    folder = write_capture(tmp_path, "dashboard", _frames(), _requests(), NOW)

    lines = (folder / "requests.txt").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "GET  document  tdx.example.edu/TDNext/Home/Desktop/Default.aspx  x1",
        "GET  document  tdx.example.edu/TDNext/widget.html  x1",
        "GET  image  tdx.example.edu/TDNext/img/#.png  x1",
        "POST  fetch  tdx.example.edu/TDNext/api/tickets.json  x2",
    ]


def test_no_private_text_reaches_any_file(tmp_path: Path) -> None:
    folder = write_capture(tmp_path, "ticket", _frames(), _requests(), NOW)

    written = "\n".join(path.read_text(encoding="utf-8") for path in folder.iterdir())
    for private in ["Alex Example", "Jordan Sample", "3119232", "88125", "user=alex", "?", "@"]:
        assert private not in written


@pytest.mark.parametrize("name", ["", "Dashboard", "my capture", "a/b", "../up", "x" * 41, "é"])
def test_bad_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        write_capture(tmp_path, name, [], [], NOW)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["Dashboard", "../up", "x" * 41])
def test_bad_name_on_the_command_line_exits_2(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", "https://tdx.example.edu/TDNext/")

    code = main(["tdx", "capture", "--name", name])

    err = capsys.readouterr().err
    assert code == 2
    assert "--name" in err
    assert len(err.strip().splitlines()) == 1
