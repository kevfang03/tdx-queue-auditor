"""The event request form parser: only the listed answers are read, and nothing
from the Recipient Data block or the personal answers ever leaves it.

Every description here is invented. The Recipient Data block uses a documentation address
(203.0.113.7, TEST-NET-3), a fake response id and a fake report link.
"""

import json
import logging
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx import event_form
from ops_suite.tdx.alerts import AlertPolicy, AlertState, build_card, plan_alerts
from ops_suite.tdx.event_form import (
    EventForm,
    EventFormOptions,
    description_text,
    parse_event_form,
)
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import RuleSettings, evaluate, format_audit_summary

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)
OPTIONS = EventFormOptions()

HOST = "To host an event at CCI, including reserving space, marketing and other logistical support."
MARKETING = (
    "Marketing/advertising for an occasion or event that does not require CCI space or other "
    "logistical support"
)
LOBBY = "Lobby Area (10th and/or 11th floor lobby area)"
CLASSROOM = "Classroom Space (available during non-class time)"
OTHER_SPACE = "Other (please explain)"
OPS = "CCI Operational Assistance"
CATERING = "Event Catering"
AV = "A/V & Technical Support"
MARKETING_SUPPORT = "Marketing Support"
OTHER_SERVICE = 'Other (please explain your "other" needs)'

FAKE_IP = "203.0.113.7"
FAKE_RESPONSE_ID = "R_3FakeResponse0001"
FAKE_REPORT = "https://survey.example.edu/reports/fake-report-0001"
RECIPIENT_DATA = f"""Recipient Data:
Time Finished: 2026-10-01 10:12:33 EDT
IP: {FAKE_IP}
ResponseID: {FAKE_RESPONSE_ID}
Link to View Results: {FAKE_REPORT}
URL to View Results: {FAKE_REPORT}
"""
# The organisation sub-block and the other personal answers.
FAKE_NAMES = ("Sam Synthetic", "Jo Fixture")
FAKE_EMAILS = ("sam.synthetic@example.edu", "jo.fixture@example.edu")
FAKE_STUDENT_ID = "19990001"
SECRETS = (FAKE_IP, FAKE_RESPONSE_ID, FAKE_REPORT, *FAKE_NAMES, *FAKE_EMAILS, FAKE_STUDENT_ID)


def form_text(
    *,
    dates: str = "10/28/2026",
    times: str = "2:00 PM - 4:00 PM",
    recurring: str = "No",
    looking: str = HOST,
    space: str = CLASSROOM,
    services: tuple[str, ...] = (AV,),
    inline: bool = False,
    organisation: bool = True,
) -> str:
    """A synthetic description in the form's shape. ``inline`` puts each answer on its
    label's line ("Label: answer"); otherwise the answer is on the lines below."""
    fields: list[tuple[str, str]] = [("Are you:", "A student organization")]
    if organisation:
        fields += [
            ("Organization Name:", "Fake Robotics Society"),
            ("Your Name:", FAKE_NAMES[0]),
            ("Your Email:", FAKE_EMAILS[0]),
            ("Student ID Number:", FAKE_STUDENT_ID),
        ]
    fields += [
        ("Event Type:", "Workshop"),
        ("Event Title:", "Intro to soldering"),
        ("Event Description:", "A hands-on session. I am looking: forward to it!"),
        ("On-Site Contact:", f"{FAKE_NAMES[1]}\n{FAKE_EMAILS[1]}"),
        ("Is this a recurring event:", recurring),
        ("What is your proposed event date(s):", dates),
        ("What is your event proposed start/end times:", times),
        ("Please confirm that you have reviewed the event date(s) ...", "Yes"),
        ("I am looking:", looking),
        ("What type of space are you looking for:", space),
        ("Who is your anticipated audience ...", "Students"),
        ("What is your estimated event headcount?", "40"),
        ("Will this event include minors? ...", "No"),
        ("Please select all that apply:", "\n".join(services)),
        ("Please describe your A/V needs in detail ...", "A projector and two microphones."),
        ("By submitting this request ...", "I agree"),
    ]
    joiner = " " if inline else "\n"
    body = "\n".join(f"{label}{joiner}{answer}" for label, answer in fields)
    return f"{RECIPIENT_DATA}\nResponse Summary:\n{body}\n"


def parse(text: str, options: EventFormOptions = OPTIONS) -> EventForm | None:
    return parse_event_form(text, TZ, NOW, options)


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


# --- the cut -------------------------------------------------------------------------------


def test_no_response_summary_gives_none() -> None:
    assert parse(RECIPIENT_DATA + "\nAre you:\nA student\n") is None
    assert parse("") is None


def test_nothing_before_the_response_summary_is_read() -> None:
    # A date in the Recipient Data block would be read if the cut did not come first.
    text = form_text(dates="")
    assert "2026-10-01" in text

    form = parse(text)

    assert form is not None and form.dates == ()


def test_a_second_recipient_data_block_ends_the_summary() -> None:
    text = form_text() + "\n" + RECIPIENT_DATA.replace("Time Finished", "Event Title")

    form = parse(text)

    assert form is not None and form.dates == (at(10, 28, 14),)


# --- dates ---------------------------------------------------------------------------------


@pytest.mark.parametrize("inline", [False, True])
def test_a_single_date_takes_the_start_time(inline: bool) -> None:
    form = parse(form_text(inline=inline))

    assert form is not None
    assert form.dates == (at(10, 28, 14),)
    assert form.recurring is False


def test_several_dates_are_all_read() -> None:
    form = parse(form_text(dates="10/28/2026, 11/4/2026", times="6:00 PM - 8:00 PM"))

    assert form is not None and form.dates == (at(10, 28, 18), at(11, 4, 18))


def test_a_month_name_range_gives_both_days() -> None:
    form = parse(form_text(dates="October 28 - 30, 2026", times="10am-12pm"))

    assert form is not None and form.dates == (at(10, 28, 10), at(10, 30, 10))


def test_an_iso_date_is_read() -> None:
    form = parse(form_text(dates="2026-10-28"))

    assert form is not None and form.dates == (at(10, 28, 14),)


def test_no_time_means_the_end_of_the_day() -> None:
    form = parse(form_text(times="TBD"))

    assert form is not None and form.dates == (at(10, 28, 23, 59),)


def test_an_unreadable_date_gives_no_dates() -> None:
    form = parse(form_text(dates="Sometime in the spring"))

    assert form is not None and form.dates == ()


def test_a_recurring_yes() -> None:
    form = parse(form_text(recurring="Yes, every Tuesday"))

    assert form is not None and form.recurring is True


def test_a_missing_recurring_answer_is_none() -> None:
    text = form_text().replace("Is this a recurring event:", "Something else:")

    form = parse(text)

    assert form is not None and form.recurring is None


# --- options -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("looking", "space", "services", "expected"),
    [
        # (wants_av, wants_room, marketing_only)
        pytest.param(HOST, LOBBY, (AV,), (True, False, False), id="av-only"),
        pytest.param(HOST, CLASSROOM, (CATERING,), (False, True, False), id="classroom-only"),
        pytest.param(HOST, CLASSROOM, (OPS, AV, CATERING), (True, True, False), id="both"),
        pytest.param(MARKETING, OTHER_SPACE, (MARKETING_SUPPORT,), (False, False, True), id="mkt"),
        pytest.param(HOST, LOBBY, (CATERING, OPS), (False, False, False), id="lobby-no-av"),
        pytest.param(HOST, OTHER_SPACE, (OTHER_SERVICE,), (False, False, False), id="other-only"),
    ],
)
def test_the_option_combinations(
    looking: str, space: str, services: tuple[str, ...], expected: tuple[bool, bool, bool]
) -> None:
    form = parse(form_text(looking=looking, space=space, services=services))

    assert form is not None
    assert (form.wants_av, form.wants_room, form.marketing_only) == expected
    assert form.unknown_options == 0


@pytest.mark.parametrize("college", ["CCI", "SCIS"])
def test_the_options_match_under_either_college_name(college: str) -> None:
    """The college is now the SCIS (formerly CCI); the defaults know both option wordings."""
    host = HOST.replace("CCI", college)
    ops = OPS.replace("CCI", college)

    form = parse(form_text(looking=host, space=CLASSROOM, services=(ops, AV)))

    assert form is not None and form.wants_av and form.wants_room
    assert not form.marketing_only and form.unknown_options == 0


def test_an_unknown_option_is_counted() -> None:
    form = parse(form_text(services=(AV, "Balloon Animals")))

    assert form is not None and form.wants_av and form.unknown_options == 1


def test_options_run_together_on_one_line_still_count() -> None:
    form = parse(form_text(services=(f"{OPS} {AV}",)))

    assert form is not None and form.wants_av


def test_matching_ignores_case_and_spacing_in_the_option_tails() -> None:
    form = parse(
        form_text(space="classroom   space (any time)", services=("a/v &  technical support",))
    )

    assert form is not None and form.wants_av and form.wants_room and form.unknown_options == 0


def test_lobby_counts_as_a_room_once_it_is_in_the_config() -> None:
    options = EventFormOptions(room_space_prefixes=("Classroom Space", "Lobby Area"))

    form = parse(form_text(space=LOBBY, services=(CATERING,)), options)

    assert form is not None and form.wants_room


def test_no_organisation_block() -> None:
    form = parse(form_text(organisation=False))

    assert form is not None and form.dates == (at(10, 28, 14),) and form.wants_av


# --- the description in HTML ---------------------------------------------------------------


def _description_html(text: str) -> str:
    lines = "".join(
        f"<div>{escape(line)}</div>" if line else "<div><br></div>" for line in text.splitlines()
    )
    return (
        '<div id="divTicketDescription"><div id="divDescription" class="gutter-bottom-lg">'
        '<h2>Description</h2><div class="gutter-top-sm"></div>'
        f'<div class="wrap-text ticket-desc">{lines}</div></div></div>'
    )


def test_description_text_reads_the_container_line_by_line() -> None:
    html = f"<html><body><p>Other text</p>{_description_html(form_text())}</body></html>"

    text = description_text(html)

    assert text is not None and "Other text" not in text
    assert text.lstrip().startswith("Recipient Data:")  # not the heading above the container
    form = parse(text)
    assert form is not None and form.dates == (at(10, 28, 14),) and form.wants_room


def test_description_text_is_none_without_the_container() -> None:
    assert description_text("<html><body><div class='feed'></div></body></html>") is None


def test_description_text_keeps_the_parts_the_page_hides() -> None:
    """The live miss: a long description collapsed behind a toggle. Its HTML still holds the
    space and the services, and the reader takes the HTML, not the rendered text."""
    lines = form_text().splitlines()
    cut = next(i for i, line in enumerate(lines) if line.startswith("I am looking:"))
    shown = "".join(f"<div>{escape(line)}</div>" for line in lines[:cut])
    hidden = "".join(f"<div>{escape(line)}</div>" for line in lines[cut:])
    html = (
        '<div id="divDescription"><div class="wrap-text ticket-desc" style="max-height: 4em; '
        f'overflow: hidden">{shown}<div class="js-desc-more" style="display: none">{hidden}'
        '</div></div><a href="#" class="js-desc-toggle">Show more</a></div>'
    )

    text = description_text(html)

    assert text is not None and "Show more" not in text
    form = parse(text)
    assert form is not None and (form.wants_av, form.wants_room) == (True, True)


def test_description_to_text_is_the_same_for_entities_and_line_ends() -> None:
    html = (
        "<div>What type of space are you&nbsp;looking for:</div>\r\n"
        "<div>Classroom&nbsp;Space</div><ul><li>A/V&nbsp;&amp;&nbsp;Technical Support</li>"
        "<li>Event Catering</li></ul><table><tr><td>Label:</td><td>Answer</td></tr></table>"
    )

    text = event_form.description_to_text(html)

    assert "\xa0" not in text and "\r" not in text
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines == [
        "What type of space are you looking for:",
        "Classroom Space",
        "A/V & Technical Support",
        "Event Catering",
        "Label:",
        "Answer",
    ]


def test_description_to_text_drops_what_is_never_description() -> None:
    """Code, styles, buttons and text boxes (read as copies on their own) are dropped; a
    template or noscript block may hold a copy of the description, so it is kept."""
    html = (
        "<div>Kept</div><script>var x = 1;</script><style>.a{}</style>"
        "<button>Show more</button><textarea>&lt;div&gt;Source&lt;/div&gt;</textarea>"
        "<template><div>T</div></template><noscript>N</noscript>"
    )

    lines = [line for line in event_form.description_to_text(html).splitlines() if line]
    assert lines == ["Kept", "T", "N"]


# --- copies of the form on one page --------------------------------------------------------


def _page(inner: str, *, around: str = "", attribute: str = "") -> str:
    return (
        f'<div id="divTicketDescription"><div id="divDescription"><h2>Description</h2>'
        f'<div class="wrap-text ticket-desc"{attribute}>{inner}</div>{around}</div></div>'
    )


def _preview_and_full() -> tuple[str, str]:
    """A preview that stops after the event's times, and the full form, as HTML lines."""
    lines = [f"<div>{escape(line)}</div>" for line in form_text(space=CLASSROOM).splitlines()]
    cut = next(i for i, line in enumerate(lines) if "I am looking:" in line)
    return "".join(lines[:cut]), "".join(lines)


def _read(html: str) -> event_form.FormRead:
    parts = event_form.description_parts(html)
    assert parts is not None
    return event_form.read_event_form(parts, TZ, NOW, OPTIONS)


@pytest.mark.parametrize(
    ("shape", "source"),
    [
        ("hidden copy", "container"),
        ("text box", "text box"),
        ("attribute", "attribute"),
        ("sibling", "outside"),
        ("template", "template"),
        ("script", "script"),
    ],
)
def test_a_preview_with_the_full_form_elsewhere_on_the_page_is_read_in_full(
    shape: str, source: str
) -> None:
    """The full copy starts with its own Recipient Data block. Cutting the preview's
    Response Summary at the next Recipient Data, as the reader once did, kept only the
    preview: the dates, but no space and no services."""
    preview, full = _preview_and_full()
    hidden = '<span style="display: none">...</span>'
    pages = {
        "hidden copy": _page(preview + f'<div style="display: none">{full}</div>'),
        "text box": _page(preview + f'<textarea style="display: none">{escape(full)}</textarea>'),
        "attribute": _page(preview + hidden, attribute=f' data-full="{escape(full)}"'),
        "sibling": _page(preview, around=f'<div style="display: none">{full}</div>'),
        "template": _page(preview + hidden) + f"<template><div>{full}</div></template>",
        "script": _page(
            preview + hidden, around=f"<script>var description = {json.dumps(full)};</script>"
        ),
    }

    read = _read(pages[shape])

    assert read.form is not None and read.source == source
    assert (read.form.wants_av, read.form.wants_room, read.form.space) == (True, True, "classroom")
    assert read.form.dates == (at(10, 28, 14),)
    assert read.blocks == 2 and read.answers == len(event_form.READ_FIELDS)
    assert FAKE_IP not in repr(read) and FAKE_NAMES[0] not in repr(read)


def test_a_full_copy_kept_as_escaped_html_text_is_read_as_html() -> None:
    """Escaped once more than the page renders it, the copy's DOM text shows its own tags;
    read as text, its space answer would start with a tag and A/V would be "&amp;"."""
    preview, full = _preview_and_full()
    hidden_source = f'<div style="display: none">{escape(full)}</div>'

    read = _read(_page(preview + hidden_source))

    assert read.source == "container" and read.form is not None
    assert (read.form.wants_av, read.form.wants_room, read.form.space) == (True, True, "classroom")


def test_entity_spaces_in_a_source_copy_still_mark_it_as_the_form() -> None:
    preview, full = _preview_and_full()
    source = escape(full.replace("Response Summary", "Response&nbsp;Summary"))

    read = _read(_page(preview + f"<textarea>{source}</textarea>"))

    assert read.source == "text box" and read.form is not None and read.form.wants_av


def test_only_copies_that_hold_the_form_are_read() -> None:
    preview, _ = _preview_and_full()
    html = _page(preview, attribute=' data-note="no form here"') + (
        '<textarea>Other text</textarea><script>var x = "Response time";</script>'
    )

    parts = event_form.description_parts(html)

    assert parts is not None and [part.source for part in parts] == ["container"]
    assert "text=" not in repr(parts[0])  # the text never shows in a repr


def test_the_most_complete_block_wins_and_ties_keep_the_first() -> None:
    preview, full = _preview_and_full()
    one = form_text(services=(AV,))
    other = form_text(services=(CATERING,))

    # A preview cut inside its last answer has every label but is shorter: the full copy wins.
    cut_short = full[: full.index("A/V &amp;")]
    assert _read(_page(cut_short + f'<div style="display:none">{full}</div>')).form.wants_av
    # Two whole copies: the first one is used.
    both = _page(
        "".join(f"<div>{escape(line)}</div>" for line in (one + "\n" + other).splitlines())
    )
    assert _read(both).form.wants_av


def test_a_block_never_runs_into_the_next_recipient_data() -> None:
    """Each block stops at the next Recipient Data, so metadata is never read as an answer:
    a services answer cut short by a metadata block stays without A/V."""
    text = (
        "Response Summary:\nWhat is your proposed event date(s):\n10/28/2026\n"
        "Please select all that apply:\nEvent Catering\n"
        f"{RECIPIENT_DATA}A/V & Technical Support\n"
    )

    form = parse(text)

    assert form is not None and not form.wants_av


def test_description_to_text_keeps_plain_text_as_it_is() -> None:
    plain = "Response Summary:\r\nI am looking:\xa0To host <an> event & more"

    assert event_form.description_to_text(plain, rich_html=False) == (
        "Response Summary:\nI am looking: To host <an> event & more"
    )


# --- nothing leaks -------------------------------------------------------------------------


def test_the_form_holds_no_text_and_its_repr_only_flags_and_counts() -> None:
    form = parse(form_text(services=(AV, "Balloon Animals")))

    assert form is not None
    shown = repr(form)
    assert (
        str(form)
        == shown
        == (
            "EventForm(dates=1, recurring=False, wants_av=True, wants_room=True, "
            "marketing_only=False, unknown_options=1, has_time=True, end_time=16:00:00, "
            "space=classroom)"
        )
    )
    for value in (form.recurring, form.wants_av, form.wants_room, form.marketing_only):
        assert isinstance(value, bool)
    for secret in SECRETS:
        assert secret not in shown


def test_parsing_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    parse(form_text())
    parse("garbage " + RECIPIENT_DATA)

    assert caplog.records == []


def test_a_failure_inside_the_parser_gives_none_and_never_raises_with_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(text: str, tz: ZoneInfo, now: datetime):
        raise ValueError(text)  # an exception carrying the form's text

    monkeypatch.setattr(event_form, "read_event_times", boom)

    assert parse(form_text()) is None


def test_nothing_from_the_form_reaches_the_summary_state_file_or_card(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole path after the parser: rules, summary lines, alert state file, card."""
    caplog.set_level(logging.DEBUG)
    description = form_text(dates="10/24/2026", services=(AV, "Balloon Animals"))
    forms = {7001: parse(description), 7002: parse(form_text(dates="10/22/2026"))}
    row = TicketRow(
        widget="unclaimed",
        ticket_id=7001,
        title="CCI Event Request Form",
        requestor=FAKE_NAMES[0],
        resp_group="Example Group",
        created=NOW,
        modified=None,
        prim_resp=None,
        status=None,
        detail_url="https://tdx.example.edu/TDNext/Apps/1/Tickets/TicketDet?TicketID=7001",
    )
    other = TicketRow(
        **{**{name: getattr(row, name) for name in TicketRow.__slots__}, "ticket_id": 7002}
    )
    feeds = {
        7001: TicketFeed(7001, ()),
        7002: TicketFeed(7002, (FeedEntry("Pat Helpdesk", NOW - timedelta(hours=72), "reply"),)),
    }
    rules = RuleSettings(extra_staff_names=("Pat Helpdesk",))

    result = evaluate([row, other], feeds, NOW, rules, TZ, forms=forms)
    assert {(f.ticket_id, f.kind) for f in result.findings} == {
        (7001, "event_outreach_due"),
        (7002, "event_details_pending"),
    }
    state = AlertState(tmp_path / "alerts-state.json")
    policy = AlertPolicy(3, 30, 7, max_rows_per_card=25)
    alert_plan = plan_alerts(
        result.findings,
        state,
        NOW,
        policy,
        seen_ticket_ids={7001, 7002},
        unsettled_ticket_ids=set(),
        queue_complete=True,
    )
    alert_plan.record(state, NOW)
    state.save()
    rows = {7001: row, 7002: other}
    for show_titles in (True, False):
        card = build_card(
            alert_plan,
            rows,
            {7001: row.detail_url, 7002: None},
            NOW,
            TZ,
            {"unclaimed": 2, "feeds_read": 2, "feeds_failed": 0},
            title_chars=80,
            show_titles=show_titles,
        )
        assert "email the requester for the room number" in json.dumps(card)
        shown = json.dumps(card)
        for secret in SECRETS:
            assert secret not in shown

    outputs = [
        *format_audit_summary(result, show_ids=True),
        (tmp_path / "alerts-state.json").read_text(encoding="utf-8"),
        repr(result),
        repr(forms),
        "".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records),
    ]
    for secret in SECRETS:
        assert all(secret not in text for text in outputs)


# --- what "Events this week" reads ----------------------------------------------------------


@pytest.mark.parametrize(
    ("space", "expected"),
    [(LOBBY, "lobby"), (CLASSROOM, "classroom"), (OTHER_SPACE, "other"), ("", None)],
)
def test_the_space_is_read_as_a_category(space: str, expected: str | None) -> None:
    form = parse(form_text(space=space))

    assert form is not None and form.space == expected


def test_an_unknown_space_answer_is_no_category() -> None:
    form = parse(form_text(space="Rooftop garden, please"))

    assert form is not None and form.space is None


def test_the_start_and_end_times_are_read() -> None:
    timed = parse(form_text(times="2:00 PM - 4:00 PM"))
    start_only = parse(form_text(times="2:00 PM"))
    untimed = parse(form_text(times=""))

    assert timed is not None and timed.has_time and timed.end_time == time(16, 0)
    assert start_only is not None and start_only.has_time and start_only.end_time is None
    assert untimed is not None and not untimed.has_time and untimed.end_time is None
