"""parse_feed and feed_ready on the synthetic ticket pages. Pure: no browser, no network."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx import feed as feed_module
from ops_suite.tdx.demo.app import PAGES_DIR
from ops_suite.tdx.feed import feed_ready, feed_state, parse_feed, read_feed_frames

WORKMGMT = Path(__file__).parent.parent / "fixtures" / "tdx_synthetic" / "workmgmt"
TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
FEED_END = '<div class="text-center NoPrint js-feed-end">'
INDICATOR = '<div class="refresh-indicator-wrapper NoPrint">'
INVENTED = [
    "Alex Example",
    "Riley Testperson",
    "Pat Helpdesk",
    "Jordan Thirdparty",
    "Sam Synthetic",
    "Room 204",
    "example.edu",
]


def _ticket_page(feed: str | None, *, indicator_hidden: bool = True) -> str:
    """The ticket frame as the browser shows it once the feed script has run."""
    html = (PAGES_DIR / "ticket.html").read_text(encoding="utf-8")
    if feed is not None:
        fragment = (WORKMGMT / "feeds" / f"{feed}.html").read_text(encoding="utf-8")
        html = html.replace(FEED_END, fragment + FEED_END)
    if indicator_hidden:
        html = html.replace(INDICATOR, INDICATOR.replace(">", ' style="display: none;">'))
    return html


def _frames(feed: str | None = "5501001", **options: bool) -> list[str]:
    """Every frame of the ticket tab, as in the capture: shell, dashboard, ticket, blank."""
    return [
        (PAGES_DIR / "ticket-shell.html").read_text(encoding="utf-8"),
        (PAGES_DIR / "dashboard.html").read_text(encoding="utf-8"),
        _ticket_page(feed, **options),
        "<html><head></head><body></body></html>",
    ]


def _summary(feed) -> list[tuple[str, str | None, str]]:
    return [
        (entry.kind, entry.created.isoformat() if entry.created else None, entry.author)
        for entry in feed.entries
    ]


def test_every_entry_kind_is_classified_newest_first() -> None:
    feed = parse_feed(_frames(), 5501001, TZ, NOW)

    assert feed is not None
    assert feed.ticket_id == 5501001
    assert _summary(feed) == [
        # The requestor's email reply to B, after the private note A.
        ("reply", "2026-10-07T11:30:00-04:00", "Alex Example"),
        # A private note with a status change stays private.
        ("private", "2026-10-07T09:15:00-04:00", "Riley Testperson"),
        # A reply with a status change is a reply.
        ("reply", "2026-10-06T16:40:00-04:00", "Pat Helpdesk"),
        # Undated: keeps its page position (fourth on the page, after B's reply).
        ("reply", None, "Jordan Thirdparty"),
        # A status change only.
        ("system", "2026-10-05T14:00:00-04:00", "Pat Helpdesk"),
        # A reply with an attachment and a "Notified" list.
        ("reply", "2026-10-05T13:58:00-04:00", "Pat Helpdesk"),
        # A reply by someone else under a private note is not private by inheritance.
        ("reply", "2026-10-03T10:05:00-04:00", "Pat Helpdesk"),
        ("private", "2026-10-03T10:00:00-04:00", "Riley Testperson"),
    ]
    # The undated entry and the one without an author.
    assert feed.unreadable == 2


def test_unreadable_entries_are_shown_only_as_shapes() -> None:
    details = read_feed_frames(_frames(), 5501001, TZ, NOW)

    assert details is not None
    assert details.unreadable_shapes == ("undated: aaaaaaaaa aaaaaaa", "no author")


def test_a_feed_shown_oldest_first_is_returned_newest_first() -> None:
    feed = parse_feed(_frames("5501002"), 5501002, TZ, NOW)

    assert feed is not None
    assert _summary(feed) == [
        ("private", "2026-10-03T13:00:00-04:00", "Riley Testperson"),
        # Quoted history in a blockquote does not change the kind.
        ("reply", "2026-10-02T08:15:00-04:00", "Sam Synthetic"),
        ("reply", "2026-10-01T09:00:00-04:00", "Pat Helpdesk"),
    ]
    assert feed.unreadable == 0


def test_an_empty_feed_gives_no_entries() -> None:
    frames = _frames("5501003")

    assert feed_state(frames[2]) == "empty"
    feed = parse_feed(frames, 5501003, TZ, NOW)
    assert feed is not None
    assert (feed.entries, feed.unreadable) == ((), 0)


def test_the_feed_frame_is_found_behind_the_dashboard_frame() -> None:
    frames = _frames()

    # The shell and the dashboard frame hold no feed; only the ticket frame does.
    assert [feed_state(html) for html in frames] == ["absent", "absent", "entries", "absent"]
    reordered = [frames[2], frames[1], frames[0]]
    first = parse_feed(frames, 5501001, TZ, NOW)
    second = parse_feed(reordered, 5501001, TZ, NOW)
    assert first is not None and second is not None
    assert first.entries == second.entries


def test_no_frame_with_a_feed_gives_none() -> None:
    shell, dashboard, _, blank = _frames()

    assert parse_feed([shell, dashboard, blank], 5501001, TZ, NOW) is None
    assert parse_feed([], 5501001, TZ, NOW) is None


@pytest.mark.parametrize(
    ("feed", "hidden", "state"),
    [
        (None, False, "loading"),  # the search has not returned yet
        (None, True, "empty"),
        ("5501003", True, "empty"),
        ("5501001", True, "entries"),
        # Entries count as loaded even while the indicator shows again (a live refresh).
        ("5501001", False, "entries"),
    ],
)
def test_feed_ready(feed: str | None, hidden: bool, state: str) -> None:
    html = _ticket_page(feed, indicator_hidden=hidden)

    assert feed_state(html) == state
    assert feed_ready(html) is (state != "loading")


@pytest.mark.parametrize(
    "hiding",
    [' style="display:none"', ' style="visibility: hidden"', " hidden", ' class="hidden"'],
)
def test_ways_of_hiding_the_indicator(hiding: str) -> None:
    html = '<div class="feed"><div' + hiding + '><div class="refresh-indicator"></div></div></div>'

    assert feed_state(html) == "empty"


def test_a_page_without_a_feed_is_not_ready() -> None:
    assert feed_state("<html><body><div class='feed-heading'></div></body></html>") == "absent"
    assert not feed_ready("")


@pytest.mark.parametrize(
    ("comment", "kind"),
    [
        ("Changed Status from <b>New</b> to <b>In Process</b>.", "system"),
        ("Changed the Status from <strong>New</strong> to <strong>Closed</strong>.", "system"),
        (
            "Changed Status from <b>New</b> to <b>Open</b>.<br>Changed Priority from <b>Low</b> "
            "to <b>High</b>.",
            "system",
        ),  # fmt: skip
        ("Changed Status from <b>New</b> to <b>Open</b>.<br><br>Please restart.", "reply"),
        # Without bold values it is a person's sentence, not the TDX phrase.
        ("Changed my password from the old one to a new one.", "reply"),
        ("", "system"),
        ("<br><br>", "system"),
        ("<blockquote>quoted mail only</blockquote>", "system"),
        ('<div class="feed-attachments"><a href="#">file.pdf</a></div>', "system"),
        ("Thanks!", "reply"),
    ],
)
def test_what_counts_as_a_visible_comment(comment: str, kind: str) -> None:
    html = (
        '<div class="feed"><div role="group"><div class="feed-body">'
        '<div class="feed-heading"><span class="feed-participant-name">Alex Example</span></div>'
        f'<div class="wrap-text">{comment}</div>'
        '<div class="feed-context"><span class="feed-timestamp"><span>10/5/26 2:00 PM</span>'
        "</span></div></div></div></div>"
    )

    feed = parse_feed([html], 1, TZ, NOW)

    assert feed is not None
    assert [entry.kind for entry in feed.entries] == [kind]


def test_repr_holds_no_invented_name_or_text() -> None:
    feed = parse_feed(_frames(), 5501001, TZ, NOW)

    assert feed is not None
    shown = repr(feed) + "".join(repr(entry) for entry in feed.entries)
    details = read_feed_frames(_frames(), 5501001, TZ, NOW)
    shown += repr(details)
    for invented in INVENTED:
        assert invented not in shown
    assert repr(feed) == "TicketFeed(ticket_id=5501001, entries=8)"


# --- private markers: rendered, not present (payload 04c) -------------------------------

# Document order of the markers in feeds/5501005.html: E3, E2, E2b, E1, E1a; only E2's shows.
HIDDEN_MARKERS_SHOWN = [False, True, False, False, False]


def _kinds(feed) -> list[tuple[str, str]]:
    return [(entry.kind, entry.author) for entry in feed.entries]


def test_hidden_private_markers_are_ignored_when_visibility_is_known() -> None:
    page = [_ticket_page("5501005")]

    details = read_feed_frames(page, 5501005, TZ, NOW, private_marker_visible=HIDDEN_MARKERS_SHOWN)

    assert details is not None
    assert _kinds(details.feed) == [
        ("system", "Pat Helpdesk"),  # E3: status change only
        ("reply", "Alex Example"),  # E2b: someone else under the note, own marker hidden
        ("private", "Riley Testperson"),  # E2a: the note's author, inherited
        ("private", "Riley Testperson"),  # E2: the one visible marker
        ("reply", "Alex Example"),  # E1a
        ("reply", "Pat Helpdesk"),  # E1
    ]
    assert dict(details.marker_stats) == {
        "entries": 6,
        "nested": 3,
        "private_present": 5,
        "private_visible": 1,
        "inherited_private": 1,
        "visibility_fallback": 0,
        "merge_notes": 0,
        "reassignments": 0,
        "signature_desk_replies": 0,
    }


def test_without_visibility_a_present_marker_counts() -> None:
    details = read_feed_frames([_ticket_page("5501005")], 5501005, TZ, NOW)

    assert details is not None
    assert {kind for kind, _ in _kinds(details.feed)} == {"private"}
    assert details.marker_stats["private_visible"] == 5
    assert details.marker_stats["visibility_fallback"] == 0


def test_a_visibility_list_that_does_not_fit_falls_back_to_presence() -> None:
    details = read_feed_frames(
        [_ticket_page("5501005")], 5501005, TZ, NOW, private_marker_visible=[True, False]
    )

    assert details is not None
    assert details.marker_stats["visibility_fallback"] == 1
    assert {kind for kind, _ in _kinds(details.feed)} == {"private"}


def test_parse_feed_passes_the_visibility_list_on() -> None:
    feed = parse_feed(
        [_ticket_page("5501005")], 5501005, TZ, NOW, private_marker_visible=HIDDEN_MARKERS_SHOWN
    )

    assert feed is not None
    assert [entry.kind for entry in feed.entries].count("private") == 2


@pytest.mark.parametrize(
    ("reply_author", "reply_marker", "kind"),
    [
        ("Riley Testperson", "", "private"),  # the note's author: inherited
        ("testperson, riley", "", "private"),  # the same person, written differently
        ("Alex Example", "", "reply"),  # someone else: not inherited
        ("Alex Example", '<span class="feed-private">Private</span>', "private"),  # own marker
    ],
)
def test_replies_under_a_private_note(reply_author: str, reply_marker: str, kind: str) -> None:
    html = (
        '<div class="feed"><div role="group"><div class="feed-body">'
        '<div class="feed-heading"><span class="feed-participant-name">Riley Testperson</span>'
        '<span class="feed-private">Private</span></div>'
        '<div class="wrap-text">A note.</div>'
        '<div class="feed-context"><span class="feed-timestamp"><span>10/5/26 2:00 PM</span>'
        "</span></div>"
        f'<div class="feed-reply"><span class="feed-participant-name">{reply_author}</span>'
        f'{reply_marker}<div class="wrap-text">A reply.</div><div class="reply-context">'
        '<span class="feed-timestamp"><span>10/5/26 3:00 PM</span></span></div></div>'
        "</div></div></div>"
    )

    feed = parse_feed([html], 1, TZ, NOW)

    assert feed is not None
    assert [entry.kind for entry in feed.entries] == [kind, "private"]


# --- merged tickets and reassignments (payload 04c addenda) ------------------------------


def test_tdx_notices_are_system_and_a_merge_stays_one_entry() -> None:
    details = read_feed_frames([_ticket_page("5501006")], 5501006, TZ, NOW)

    assert details is not None
    # Both notices carry a private marker; the fixed TDX phrase wins. The merge notice holds
    # the merged ticket's whole thread (From:/Sent: blocks) and is still one entry; the
    # carried-over entries keep their own kind, author and time.
    assert _kinds(details.feed) == [
        ("system", "Riley Testperson"),  # reassignment
        ("system", "Pat Helpdesk"),  # merge notice
        ("reply", "Pat Helpdesk"),
        ("reply", "Alex Example"),
        ("reply", "Jordan Carryover"),
        ("reply", "Alex Example"),  # quotes a merge notice: the phrase is not its own text
    ]
    assert details.marker_stats["entries"] == 6
    assert details.marker_stats["nested"] == 0
    assert details.marker_stats["merge_notes"] == 1
    assert details.marker_stats["reassignments"] == 1
    # The authors of both notices are marked as desk, in page order.
    assert details.feed.desk_authors == ("Riley Testperson", "Pat Helpdesk")
    assert "Pat" not in repr(details.feed)


@pytest.mark.parametrize(
    ("comment", "kind"),
    [
        ("Merged incident 123 (A title, originally created by Alex Example):", "system"),
        ("  MERGED INCIDENT 123 (A title, originally created by Alex Example):", "system"),
        ("Reassigned this incident from Group A to Group B.", "system"),
        ("reassigned this incident from Group A to Group B.", "system"),
        ("<blockquote>Reassigned this incident from Group A to Group B.</blockquote>Why?", "reply"),
        ("Thanks. Merged incident notes are confusing.", "reply"),  # not at the start
        ("I reassigned this incident from my phone.", "reply"),
    ],
)
def test_fixed_phrases_open_the_entry_s_own_comment(comment: str, kind: str) -> None:
    html = (
        '<div class="feed"><div role="group"><div class="feed-body">'
        '<div class="feed-heading"><span class="feed-participant-name">Pat Helpdesk</span>'
        '<span class="feed-private">Private</span></div>'
        f'<div class="wrap-text">{comment}</div>'
        '<div class="feed-context"><span class="feed-timestamp"><span>10/5/26 2:00 PM</span>'
        "</span></div></div></div></div>"
    )

    feed = parse_feed([html], 1, TZ, NOW, private_marker_visible=[False])

    assert feed is not None
    assert [entry.kind for entry in feed.entries] == [kind]


def test_without_the_phrases_the_notices_are_read_as_usual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feed_module, "MERGE_NOTE_PREFIXES", ())
    monkeypatch.setattr(feed_module, "SYSTEM_NOTE_PREFIXES", ())

    details = read_feed_frames([_ticket_page("5501006")], 5501006, TZ, NOW)

    assert details is not None
    assert [entry.kind for entry in details.feed.entries[:2]] == ["private", "private"]
    assert details.feed.desk_authors == ()
    assert (details.marker_stats["merge_notes"], details.marker_stats["reassignments"]) == (0, 0)


# --- the newest reply's text (payload 06b) -----------------------------------------------


@pytest.mark.parametrize(
    ("feed", "text"),
    [
        (
            "5501007",  # a plain question; a newer private note is not read
            "Hi Pat,\nIs there any update on the library printer? I still cannot print from my "
            "laptop.\nThanks,\nSam",
        ),
        (
            "5501008",  # "I'll get back to you"; a newer status change is not read
            "Thanks for the steps.\nI am off campus until Monday, so I will try them then and "
            "get back to you.",
        ),
        ("5501009", "Thanks, that fixed it!\nEverything works now."),
        (
            "5501010",  # an email reply nested under the desk's update, quoting it
            "Yes, the VPN error still shows up after the restart.\nOn Fri, Oct 2, 2026 at "
            "1:00 PM Pat Helpdesk <pat.helpdesk@example.edu> wrote:\n> Hi Sam, could you "
            "restart the laptop and try the VPN again?",
        ),
        (
            "5501011",
            "Can someone call me to set up a time? I am at sam.sample@example.edu or "
            "215-555-0142 after 2pm.",
        ),
    ],
)
def test_the_newest_reply_s_comment_text_is_read(feed: str, text: str) -> None:
    parsed = parse_feed(_frames(feed), int(feed), TZ, NOW)

    assert parsed is not None
    assert parsed.latest_reply_text == text
    assert parsed.unreadable == 0


def test_only_the_newest_reply_is_read_never_notes_heading_or_notified_list() -> None:
    # 5501001: the newest reply is the requestor's email reply B1, newer than private note A.
    parsed = parse_feed(_frames("5501001"), 5501001, TZ, NOW)

    assert parsed is not None
    text = parsed.latest_reply_text
    assert text is not None
    assert text.startswith("Hi,\nThe HDMI input shows no picture either.")
    for not_read in (
        "Called the AV team",  # private note A, newer than B1
        "Alex Example",  # B1's author line
        "Wed 10/7/26",  # its timestamp
        "Notified",
        "Changed Status",
        "Adding myself",  # an older reply
        "room-204-schedule.pdf",
    ):
        assert not_read not in text


def test_attachments_and_blockquotes_are_left_out_of_the_text() -> None:
    oldest_first = parse_feed(_frames("5501002"), 5501002, TZ, NOW)
    assert oldest_first is not None
    assert oldest_first.latest_reply_text == "It is the main lab on the second floor."

    # Empty the newest reply's comment: it becomes a system entry, so the desk reply with the
    # attachment is the newest reply.
    frames = _frames("5501009")
    frames[2] = frames[2].replace("Thanks, that fixed it!<br>Everything works now.", "", 1)
    parsed = parse_feed(frames, 5501009, TZ, NOW)

    assert parsed is not None
    assert [entry.kind for entry in parsed.entries] == ["system", "reply"]
    assert parsed.latest_reply_text == "Here are the setup steps."


def test_no_reply_or_only_private_replies_give_no_text() -> None:
    assert parse_feed(_frames("5501003"), 5501003, TZ, NOW).latest_reply_text is None
    # 5501005 without a visibility list: every entry's marker counts, so nothing is a reply.
    assert parse_feed(_frames("5501005"), 5501005, TZ, NOW).latest_reply_text is None


def test_a_reply_without_a_comment_element_gives_none_and_nothing_unreadable() -> None:
    frames = _frames("5501009")
    comment = (
        '<div class="wrap-text feed-comment-text">Thanks, that fixed it!<br>Everything works '
        "now.</div>"
    )
    assert comment in frames[2]
    frames[2] = frames[2].replace(comment, "Thanks, that fixed it!", 1)

    parsed = parse_feed(frames, 5501009, TZ, NOW)

    assert parsed is not None
    assert [entry.kind for entry in parsed.entries] == ["reply", "reply"]
    assert parsed.latest_reply_text is None
    assert parsed.unreadable == 0


def test_the_text_is_capped() -> None:
    frames = _frames("5501009")
    frames[2] = frames[2].replace("Everything works now.", "word " * 6000, 1)

    parsed = parse_feed(frames, 5501009, TZ, NOW)

    assert parsed is not None
    assert parsed.latest_reply_text is not None
    assert len(parsed.latest_reply_text) <= feed_module.REPLY_TEXT_MAX_CHARS


@pytest.mark.parametrize("feed", ["5501007", "5501010", "5501011"])
def test_repr_holds_no_reply_text(feed: str) -> None:
    details = read_feed_frames(_frames(feed), int(feed), TZ, NOW)

    assert details is not None
    assert details.feed.latest_reply_text
    shown = repr(details) + repr(details.feed)
    for word in ("Sam", "printer", "VPN", "555", "example.edu"):
        assert word not in shown
