from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup

from ops_suite.tdx.demo.app import PAGES_DIR
from ops_suite.tdx.rows import (
    StructureError,
    date_shape,
    normalise_header,
    parse_tdx_datetime,
    parse_widget,
    widget_ready,
    widget_title,
)

WIDGETS = Path(__file__).parent.parent / "fixtures" / "tdx_synthetic" / "workmgmt" / "widgets"
TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
PAGE_URL = "https://tdx.example.edu/TDWorkManagement/WorkManagement/Dashboard/Dashboard"
UNCLAIMED_TITLE = "Unclaimed Tickets in My Group(s)"
CLAIMED_TITLE = "Active Tickets Claimed by Others in My Group(s)"
# Invented strings from the fixtures that must never show up in errors or repr().
INVENTED = [
    "Alex Example",
    "Casey Testperson",
    "Morgan Fixture",
    "Pat Helpdesk",
    "Laptop will not connect",
    "Zoom recording",
    "TicketDet",
    "example.edu",
]


def container(title: str, grid: str) -> str:
    """A dashboard widget container around a grid, as in the capture."""
    return (
        '<div class="tdx-dashboard__widget-container"><header class="tdx-header-container">'
        '<div class="tdx-control-bar tdx-control-bar--widget-header">'
        f'<h1 class="tdx-control-bar__title tdx-control-bar__title--no-overflow">{title}</h1>'
        f"</div></header><div>{grid}</div></div>"
    )


def fixture_widget(name: str, title: str) -> str:
    return container(title, (WIDGETS / name).read_text(encoding="utf-8"))


def parse(name: str, kind: str = "claimed", title: str = CLAIMED_TITLE):
    return parse_widget(fixture_widget(name, title), kind, PAGE_URL, TZ, NOW)


# --- widgets -----------------------------------------------------------------------------


def test_unclaimed_page_parses_every_row() -> None:
    page = parse("40101-1.html", "unclaimed", UNCLAIMED_TITLE)

    assert [row.ticket_id for row in page.rows] == [4410231, 4410245, 4410302, 4410317]
    first = page.rows[0]
    assert first.widget == "unclaimed"
    assert first.title == "Laptop will not connect to campus Wi-Fi"
    assert first.requestor == "Alex Example"
    assert first.resp_group == "IT Service Desk"
    assert first.created == datetime(2026, 10, 2, 15, 30, tzinfo=TZ)
    assert first.modified is None and first.prim_resp is None and first.status is None
    assert first.detail_url == (
        "https://tdx.example.edu/TDWorkManagement/Apps/43/Tickets/TicketDet?TicketID=4410231"
    )
    assert page.rows[2].created == datetime(2026, 10, 5, 14, 10, tzinfo=TZ)
    # "Yesterday 4:12 PM" is not a known format yet: no date, only its shape.
    assert page.rows[3].created is None
    assert page.unreadable_dates == ("aaaaaaaaa 9:99 aa",)
    assert page.has_next is False
    assert page.rows_without_id == 0
    assert page.page_marker == "1"


def test_claimed_pages_follow_their_own_column_order() -> None:
    pages = [parse(f"40102-{n}.html") for n in (1, 2, 3)]

    assert [len(page.rows) for page in pages] == [10, 10, 4]
    assert [page.has_next for page in pages] == [True, True, False]
    assert [page.page_marker for page in pages] == ["1", "2", "3"]
    first = pages[0].rows[0]
    assert first.ticket_id == 4409100
    assert first.widget == "claimed"
    assert first.title == "Zoom recording missing from the cloud library"
    assert first.requestor == "Morgan Fixture"
    assert first.resp_group == "CCI Computing Support"
    assert first.status == "New"
    assert first.prim_resp == "Pat Helpdesk"
    assert first.modified == datetime(2026, 10, 1, 11, 47, tzinfo=TZ)
    assert first.created is None  # the claimed widget has no Created column
    blank = pages[0].rows[5]
    assert blank.prim_resp is None
    assert pages[0].rows[2].modified == datetime(2026, 10, 2, 15, 30, tzinfo=TZ)
    assert pages[0].rows[6].modified == datetime(2026, 10, 3, 9, 45, tzinfo=TZ)
    assert all(page.unreadable_dates == () for page in pages)


def test_sort_link_text_in_headers_is_stripped() -> None:
    # The claimed fixture's "Modified" header is a sort link with screen-reader text.
    html = (WIDGETS / "40102-1.html").read_text(encoding="utf-8")
    header = BeautifulSoup(html, "html.parser").select("thead th")[1].get_text(" ")
    assert "Sort by" in header and "Descending" in header

    assert normalise_header(header) == "modified"
    assert normalise_header("ID Sort by ID Ascending") == "id"
    assert normalise_header("  Resp \n Group ") == "resp group"
    assert normalise_header("Prim Resp Sorted descending") == "prim resp"


def test_a_missing_required_column_raises_without_page_text() -> None:
    with pytest.raises(StructureError) as error:
        parse("40102-1-missing.html")

    message = str(error.value)
    assert message.startswith("claimed widget: required column(s) missing: Prim Resp")
    assert "Modified" in message  # found columns are named
    for invented in INVENTED:
        assert invented not in message


def test_unknown_header_labels_are_counted_not_named() -> None:
    grid = (
        '<div class="k-grid"><table role="grid"><thead><tr>'
        "<th>ID</th><th>Alex Example</th></tr></thead><tbody></tbody></table></div>"
    )

    with pytest.raises(StructureError) as error:
        parse_widget(container(UNCLAIMED_TITLE, grid), "unclaimed", PAGE_URL, TZ, NOW)

    assert "found: ID and 1 other column(s)" in str(error.value)
    assert "Alex" not in str(error.value)


def test_an_empty_widget_gives_no_rows() -> None:
    page = parse("40101-1-empty.html", "unclaimed", UNCLAIMED_TITLE)

    assert page.rows == ()
    assert page.has_next is False
    assert page.unreadable_dates == ()


def test_repr_shows_only_the_widget_and_id() -> None:
    row = parse("40102-1.html").rows[0]

    assert repr(row) == "TicketRow(widget='claimed', ticket_id=4409100)"
    assert str(row) == repr(row)
    for invented in INVENTED:
        assert invented not in repr(row)
        assert invented not in repr(parse("40102-1.html"))  # WidgetPage shows rows by repr


def _grid_with_ids(ids: list[str]) -> str:
    rows = "".join(
        f'<tr class="k-table-row k-master-row" role="row"><td>{tid}</td><td>10/2/26</td>'
        "<td>Title</td><td>Requestor</td><td>Group</td></tr>"
        for tid in ids
    )
    head = "".join(f"<th>{c}</th>" for c in ["ID", "Created", "Title", "Requestor", "Resp Group"])
    return (
        f'<div class="k-grid"><table role="grid"><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def test_rows_without_an_id_are_counted() -> None:
    html = container(UNCLAIMED_TITLE, _grid_with_ids(["4410231", "", "4410245", "n/a"]))

    page = parse_widget(html, "unclaimed", PAGE_URL, TZ, NOW)

    assert [row.ticket_id for row in page.rows] == [4410231, 4410245]
    assert page.rows_without_id == 2
    assert page.rows[0].detail_url is None  # no link in the ID or Title cell


def test_mostly_unreadable_ids_are_a_structure_error() -> None:
    html = container(UNCLAIMED_TITLE, _grid_with_ids(["4410231", "Alex Example", "x"]))

    with pytest.raises(StructureError) as error:
        parse_widget(html, "unclaimed", PAGE_URL, TZ, NOW)

    assert str(error.value) == "unclaimed widget: 2 of 3 rows have no readable ticket id"


def test_rows_not_marked_as_data_rows_are_a_structure_error() -> None:
    html = container(UNCLAIMED_TITLE, _grid_with_ids(["4410231"]).replace(" k-master-row", ""))

    with pytest.raises(StructureError, match="none marked k-master-row"):
        parse_widget(html, "unclaimed", PAGE_URL, TZ, NOW)


def test_a_widget_without_a_grid_is_a_structure_error() -> None:
    with pytest.raises(StructureError, match="no Kendo grid"):
        parse_widget(container(UNCLAIMED_TITLE, "<p>x</p>"), "unclaimed", PAGE_URL, TZ, NOW)


def test_script_links_are_not_ticket_links() -> None:
    html = container(
        UNCLAIMED_TITLE,
        _grid_with_ids(['<a href="javascript:openTicket(1)">4410231</a>']).replace(
            "<td>Title</td>", '<td><a href="/TDWorkManagement/T?ID=4410231">Title</a></td>'
        ),
    )

    row = parse_widget(html, "unclaimed", PAGE_URL, TZ, NOW).rows[0]

    assert row.detail_url == "https://tdx.example.edu/TDWorkManagement/T?ID=4410231"


def test_widget_title_and_readiness_on_the_synthetic_dashboard() -> None:
    soup = BeautifulSoup((PAGES_DIR / "dashboard.html").read_text(encoding="utf-8"), "html.parser")
    containers = [str(c) for c in soup.select(".tdx-dashboard__widget-container")]

    assert [widget_title(c) for c in containers] == [
        "unclaimed tickets in my group(s)",
        "active tickets claimed by others in my group(s)",  # its title spans two lines
        "closed tickets in my group(s)",
        "my open tasks",
    ]
    assert not any(widget_ready(c) for c in containers)  # all still show the loading animation
    assert widget_ready(fixture_widget("40101-1.html", UNCLAIMED_TITLE))
    mask = '<div class="k-loading-mask"></div></div>'
    loading = fixture_widget("40102-2.html", CLAIMED_TITLE).replace("</table>", "</table>" + mask)
    assert not widget_ready(loading)
    assert widget_title("<div><p>no title</p></div>") is None


# --- dates -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fri 10/2/26 3:30 PM", datetime(2026, 10, 2, 15, 30)),
        ("10/2/26 3:30 PM", datetime(2026, 10, 2, 15, 30)),
        ("10/2/2026 3:30 PM", datetime(2026, 10, 2, 15, 30)),
        ("10/02/2026 15:30", datetime(2026, 10, 2, 15, 30)),
        ("2026-10-02 15:30", datetime(2026, 10, 2, 15, 30)),
        ("Oct 2, 2026 3:30 PM", datetime(2026, 10, 2, 15, 30)),
        ("10/2/26", datetime(2026, 10, 2, 0, 0)),
        ("2026-10-02", datetime(2026, 10, 2, 0, 0)),
        ("Friday, October 2, 2026", datetime(2026, 10, 2, 0, 0)),
        ("  Fri  10/2/26   12:05 am ", datetime(2026, 10, 2, 0, 5)),
        ("10/2/26 12:00 PM", datetime(2026, 10, 2, 12, 0)),
        ("Sept. 30, 2026 9:15 a.m.", datetime(2026, 9, 30, 9, 15)),
        ("2026-10-02T15:30:45", datetime(2026, 10, 2, 15, 30, 45)),
    ],
)
def test_known_formats_parse_to_aware_new_york_times(text: str, expected: datetime) -> None:
    parsed = parse_tdx_datetime(text, TZ, NOW)

    assert parsed == expected.replace(tzinfo=TZ)
    assert parsed is not None and parsed.tzinfo is TZ


def test_wall_clock_times_follow_daylight_saving() -> None:
    before = parse_tdx_datetime("Fri 10/30/26 9:00 AM", TZ, NOW)
    after = parse_tdx_datetime("Mon 11/2/26 9:00 AM", TZ, NOW)

    assert before is not None and after is not None
    assert before.utcoffset() == timedelta(hours=-4)  # EDT
    assert after.utcoffset() == timedelta(hours=-5)  # EST
    assert after.astimezone(UTC) - before.astimezone(UTC) == timedelta(days=3, hours=1)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Yesterday 4:12 PM",
        "Today",
        "2 days ago",
        "13/2/26 3:30 PM",
        "2/30/26",
        "10/2/26 13:30 PM",
        "10/2/26 25:00",
        "Foo 2, 2026",
        "Alex Example",
        "10/2",
    ],
)
def test_unreadable_cells_give_none(text: str) -> None:
    assert parse_tdx_datetime(text, TZ, NOW) is None


def test_date_shape() -> None:
    # Each digit becomes 9, one 9 per digit, so a two-digit month stays two digits wide.
    assert date_shape("Fri 10/2/26 3:30 PM") == "aaa 99/9/99 9:99 aa"
    assert date_shape("Fri 9/2/26 3:30 PM") == "aaa 9/9/99 9:99 aa"
    assert date_shape("Yesterday\n 4:12 PM") == "aaaaaaaaa 9:99 aa"
    assert date_shape("") == "(blank)"
    assert date_shape("x" * 100) == "a" * 40
