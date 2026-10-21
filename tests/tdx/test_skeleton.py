import re
from pathlib import Path

import pytest

from ops_suite.tdx.skeleton import mask_digits, placeholder_for, skeletonize

SYNTHETIC = Path(__file__).parent.parent / "fixtures" / "tdx_synthetic"

# Every invented personal or ticket detail in the synthetic pages, including the page titles.
PRIVATE = [
    "Alex Example",
    "Jordan Sample",
    "Casey Placeholder",
    "Riley Testperson",
    "Morgan Fictional",
    "alex.example",
    "jordan.sample@example.edu",
    "morgan.fictional@example.edu",
    "desk-team@example.edu",
    "3119232",
    "3120045",
    "3118877",
    "55123401",
    "(215) 555-0100",
    "215-555-0199",
    "555-0142",
    "14567890",
    "Projector in Room 204 shows no picture",
    "Cannot connect laptop to campus Wi-Fi",
    "Robotics Club Demo Night",
    "Room 204",
    "HDMI",
    "IT Tickets - Alex Example - TDNext",
    "Unclaimed Tickets - Jordan Sample",
    "College of Examples",
    "Department of Samples",
    "Fri 10/2/26 3:30 PM",
    "10/3/26",
    "Oct 2, 2026",
    "8f1c2e44",
    "dXNlcj1",
]
# Single name parts, which TDX also puts into ids, names and classes (the notify list).
NAME_PARTS = ["alex", "jordan", "sample", "casey", "riley", "testperson", "morgan", "fictional"]
# Script, style and attribute content that must not survive.
DROPPED = [
    "currentUser",
    "ticketRequestor",
    "secret-style-marker",
    "c0ffee",
    "fetch(",
    "<script",
    "<style",
    "<title",
    "<head>",
    "<meta",
    "<noscript",
    "<!--",
    "aria-",
    "data-",
    "title=",
    "value=",
    "placeholder=",
    "onclick",
    "style=",
    "summary=",
    "scope=",
    "viewbox",
    "<path",
    "ReportID",
    "TicketID=",
    "mailto:",
]


def _skeleton(name: str):
    return skeletonize((SYNTHETIC / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["dashboard.html", "widget.html", "ticket.html"])
def test_no_private_text_survives(name: str) -> None:
    out = _skeleton(name).html.lower()

    leaked = [value for value in PRIVATE + DROPPED if value.lower() in out]
    assert leaked == []
    assert [part for part in NAME_PARTS if re.search(rf"(?<![a-z]){part}(?![a-z])", out)] == []
    assert "@" not in out
    assert re.search(r"\d{3,}", out) is None


@pytest.mark.parametrize("name", ["dashboard.html", "widget.html", "ticket.html"])
def test_private_values_really_are_in_the_fixture(name: str) -> None:
    raw = (SYNTHETIC / name).read_text(encoding="utf-8")

    assert sum(value in raw for value in PRIVATE) >= 8


def test_table_headers_are_kept_and_listed() -> None:
    skeleton = _skeleton("widget.html")

    assert skeleton.kept_text == (
        "ID",
        "Created",
        "Title",
        "REQUESTOR",
        "Affiliation",
        "Resp Group",
        "Acct/Dept",
        "Priority",
        "MODIFIED",
    )
    assert "<th>REQUESTOR</th>" in skeleton.html
    # A label wrapped in a sort link counts as header text too.
    assert '<th><a href="[url]">Created</a></th>' in skeleton.html


def test_columnheader_role_text_is_kept() -> None:
    skeleton = _skeleton("dashboard.html")

    assert skeleton.kept_text == ("Modified", "ID", "Title", "Prim Resp", "Status")


def test_long_header_becomes_a_placeholder() -> None:
    skeleton = _skeleton("widget.html")

    assert "Days Since" not in skeleton.html
    assert "<th>[text:10]</th>" in skeleton.html
    assert not any(text.startswith("Days") for text in skeleton.kept_text)


def test_ticket_page_keeps_no_text() -> None:
    assert _skeleton("ticket.html").kept_text == ()


def test_structure_attributes_and_urls() -> None:
    widget = _skeleton("widget.html").html
    dashboard = _skeleton("dashboard.html").html

    assert '<tr id="ticket-#" class="row-odd">' in widget
    assert '<table id="ctl00_col2_grid" class="table table-striped report-table">' in widget
    assert (
        '<iframe id="ctl00_col2_frame" name="ifrUnclaimed" class="widget-frame" src="[url]">'
        in (dashboard)
    )
    assert '<form id="aspnetForm" action="[url]">' in dashboard
    assert '<svg class="icon icon-bell"></svg>' in dashboard
    assert '<a class="brand" href="[url]">[text:2]</a>' in dashboard
    assert "[datetime]" in widget
    assert "[number]" in widget
    assert "[email]" in dashboard


def test_structural_ids_survive_the_word_check() -> None:
    ticket = _skeleton("ticket.html").html
    widget = _skeleton("widget.html").html

    # "Ticket", "Title" and "ID" appear in the page text but are interface words.
    assert '<h1 id="thTicket_spnTitle" class="ticket-title">' in ticket
    assert '<span id="lblTicketID" class="ticket-id">' in ticket
    assert '<div id="divFeed" class="feed" role="feed">' in ticket
    assert '<article id="feedEntry-#" class="feed-item feed-item-client">' in ticket
    assert '<ul id="notifyList" class="notify-list">' in ticket
    # A column header is allowed in a class even though rows repeat the word.
    assert '<span class="requestor">' in widget


def test_names_in_kept_attributes_are_masked() -> None:
    ticket = _skeleton("ticket.html").html

    # The notify list: ids and names built from people's names and addresses.
    assert '<input type="checkbox" id="[id]" name="[email]">' in ticket
    assert '<input type="checkbox" id="[id]" name="[name]">' in ticket
    assert '<textarea id="[id]" name="[name]">' in ticket
    # Outside form controls, only the words that also appear in the page text are masked.
    assert '<div id="avatar-**" class="avatar avatar-*-* feed-item-client">' in ticket


def test_a_word_from_the_page_text_is_masked_in_any_kept_attribute() -> None:
    out = skeletonize(
        '<div id="row_Barron" class="by-barron kuhn-note" name="McNamara">'
        "Kyle Barron, Jon Kuhn, John McNamara</div>"
    ).html

    assert out.startswith('<div id="row_*" class="by-* *-note" name="*">')


def test_an_email_local_part_is_masked_in_any_kept_attribute() -> None:
    out = skeletonize('<a href="mailto:khb36@example.edu">x</a><div class="khb36-note"></div>').html

    assert '<div class="*36-note"></div>' in out


def test_a_value_with_an_at_sign_becomes_email() -> None:
    out = skeletonize('<div id="khb36@example.edu" class="x@y"></div>').html

    assert out == '<div id="[email]" class="[email]"></div>'


def test_an_address_in_angle_brackets_is_not_kept_as_a_tag() -> None:
    out = skeletonize(
        "<p>Notified: Kyle Barron <khb36@example.edu>, Jo Ou <jo633@example.edu></p>"
    ).html

    assert out == "<p>[text:3][text:3]</p>"


def test_textarea_and_option_text_become_placeholders() -> None:
    out = skeletonize(
        "<textarea name=c>Reply to Alex Example</textarea>"
        "<select><option value=x>Alex Example</option></select>"
    ).html

    assert (
        out
        == '<textarea name="[name]">[text:4]</textarea><select><option>[text:2]</option></select>'
    )


def test_whitespace_is_collapsed() -> None:
    out = skeletonize("<div>\n    \n  <span>   </span>\t</div>").html

    assert out == "<div>\n<span> </span> </div>"


def test_text_outside_a_header_cell_is_not_kept() -> None:
    skeleton = skeletonize("<table><tr><th>Status</th><td>Status</td></tr></table><p>ID</p>")

    assert skeleton.kept_text == ("Status",)
    assert skeleton.html.count("[text:1]") == 2


def test_an_unclosed_header_cell_ends_at_the_next_cell() -> None:
    skeleton = skeletonize("<table><tr><th>Requestor<td>Alex Example<tr><td>Jordan Sample</table>")

    assert skeleton.kept_text == ("Requestor",)
    assert "Alex Example" not in skeleton.html
    assert "Jordan Sample" not in skeleton.html


def test_row_headers_are_data_not_labels() -> None:
    skeleton = skeletonize(
        '<table><tr><th scope="row">Alex Example</th><th role="rowheader">Jordan Sample</th>'
        '<td role="columnheader">Due</td></tr></table>'
    )

    assert skeleton.kept_text == ("Due",)


def test_header_text_that_looks_like_data_is_not_kept() -> None:
    skeleton = skeletonize(
        "<table><tr><th>3119232</th><th>Fri 10/2/26 3:30 PM</th>"
        "<th>alex@example.edu</th><th>Due</th></tr></table>"
    )

    assert skeleton.kept_text == ("Due",)
    assert "<th>[number]</th><th>[datetime]</th><th>[email]</th>" in skeleton.html


def test_header_labels_are_escaped() -> None:
    skeleton = skeletonize("<table><tr><th>Acct &amp; Dept</th></tr></table>")

    assert skeleton.kept_text == ("Acct & Dept",)
    assert "<th>Acct &amp; Dept</th>" in skeleton.html


def test_an_unclosed_head_ends_at_the_body() -> None:
    out = skeletonize("<html><head><title>Alex Example</title><body><p>x</p></body>").html

    assert out == "<html><body><p>[text:1]</p></body>"


def test_nested_templates_and_comments_are_removed() -> None:
    out = skeletonize(
        "<div><template><template>Alex</template>Example</template><!-- Jordan --></div>"
    ).html

    assert out == "<div></div>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("alex.example@example.edu", "[email]"),
        ("Write to desk@example.edu today", "[email]"),
        ("Fri 10/2/26 3:30 PM", "[datetime]"),
        ("10/2/26", "[datetime]"),
        ("2026-10-02", "[datetime]"),
        ("3:30 PM", "[datetime]"),
        ("Oct 2, 2026", "[datetime]"),
        ("2026-10-02T15:30:00Z", "[datetime]"),
        ("Mon, Oct 5 at 9:15 AM EDT", "[datetime]"),
        ("(215) 555-0100", "[number]"),
        ("3119232", "[number]"),
        ("#3119232", "[number]"),
        ("+1 215.555.0100", "[number]"),
        ("Alex Example", "[text:2]"),
        ("Projector in Room 204 shows no picture", "[text:7]"),
        ("March 3 students", "[text:3]"),
        ("Ticket 3119232", "[text:2]"),
        ("-", "[text:1]"),
    ],
)
def test_placeholder_for(text: str, expected: str) -> None:
    assert placeholder_for(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ticket-3119232", "ticket-#"),
        ("ctl00_col2", "ctl00_col2"),
        ("feedEntry-55123401 row-12", "feedEntry-# row-12"),
        ("a123b4567", "a#b#"),
        ("", ""),
    ],
)
def test_mask_digits(value: str, expected: str) -> None:
    assert mask_digits(value) == expected
