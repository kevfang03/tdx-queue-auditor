"""Written notes, staff activity and the no-reply hold, through both feed readers.

Every feed here is invented. Each case is built once and read by the browser reader (from
HTML) and the Web API reader (from API items), so the two cannot drift apart.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from api_fakes import entry as api_entry
from api_fakes import reply as api_reply

from ops_suite.tdx.alerts import (
    REQUESTER_WAITING_HINT,
    Reader,
    desk_activity,
    finding_wording,
    section_hint,
    untouched_keys,
)
from ops_suite.tdx.api_source import UPDATE_TYPE_CREATED, map_feed
from ops_suite.tdx.feed import read_feed_frames
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import (
    AuditResult,
    Finding,
    RuleSettings,
    evaluate,
    format_audit_summary,
    in_hand_note,
    strong_desk_names,
)

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
REQUESTER = "Alex Example"
DESK = "Pat Helpdesk"
OTHER = "Riley Testperson"
SYSTEM = "System"
URL = "https://tdx.example.edu/TDNext/Apps/1/Tickets/TicketDet?TicketID=1"

STATUS = "Changed Status from <b>New</b> to <b>In Process</b>."
PERCENT = "Changed % Complete from <b>0%</b> to <b>50%</b>."
TASK = f"Ticket Task / Image the example laptop<br>{PERCENT}"
APPROVED = 'Approved the "Check the form" step in the "Example Intake" workflow.'
SELECTED = 'Selected "Done" for the "Check the form" step in the "Example Intake" workflow.'
WORKFLOW = (
    'Changing Workflow from "None" to "Example Intake" as a result of applying the '
    '"Example Routing" rule.'
)
TYPED = "Called the requester and left a message."
REASSIGNED = "Reassigned this incident from Group A to Group B."
MERGED = "Merged incident 1002 (Example title, originally created by Riley Testperson):"
REQUESTOR_CHANGE = "Changed Requestor from <b>Group Inbox</b> to <b>Alex Example</b>."
ACCOUNT_CHANGE = "Changed Account from <b>None</b> to <b>Example Department</b>."
PRIORITY_CHANGE = "Changed Priority from <b>Low</b> to <b>High</b>."
RESPONSIBILITY_CHANGE = "Changed Responsibility from <b>Group A</b> to <b>Group B</b>."
TITLE_CHANGE = "Changed Title from <b>Help</b> to <b>Laptop will not connect</b>."


@dataclass(frozen=True)
class Item:
    author: str
    hours_ago: float
    body: str
    private: bool = False
    replies: tuple["Item", ...] = ()


def _html_entry(item: Item) -> str:
    marker = '<span class="feed-private">Private</span>' if item.private else ""
    nested = "".join(
        f'<div class="feed-reply"><span class="feed-participant-name">{r.author}</span>'
        f'<div class="wrap-text">{r.body}</div><div class="reply-context">'
        f'<span class="feed-timestamp"><span>{_stamp(r)}</span></span></div></div>'
        for r in item.replies
    )
    return (
        '<div role="group"><div class="feed-body">'
        f'<div class="feed-heading"><span class="feed-participant-name">{item.author}</span>'
        f"{marker}</div>"
        f'<div class="wrap-text">{item.body}</div>'
        f'<div class="feed-context"><span class="feed-timestamp"><span>{_stamp(item)}</span>'
        f"</span></div>{nested}</div></div>"
    )


def _when(item: Item) -> datetime:
    return NOW - timedelta(hours=item.hours_ago)


def _stamp(item: Item) -> str:
    return _when(item).strftime("%m/%d/%y %I:%M %p")


def browser(*items: Item, requester: str | None = REQUESTER) -> TicketFeed:
    frames = [f'<div class="feed">{"".join(_html_entry(item) for item in items)}</div>']
    details = read_feed_frames(frames, 1, TZ, NOW, requester=requester)
    assert details is not None
    return details.feed


def api(*items: Item, requester: str | None = REQUESTER) -> TicketFeed:
    entries, replies = [], {}
    for number, item in enumerate(items, start=1):
        entries.append(
            api_entry(
                number,
                item.author,
                _when(item),
                item.body,
                private=item.private,
                replies=len(item.replies),
            )
        )
        if item.replies:
            replies[number] = [
                api_reply(100 * number + i, r.author, _when(r), r.body)
                for i, r in enumerate(item.replies)
            ]
    return map_feed(1, entries, replies, TZ, requester=requester).feed


READERS = [browser, api]
both = pytest.mark.parametrize("read", READERS, ids=["browser", "api"])


def facts(feed: TicketFeed) -> list[tuple[str, bool]]:
    return [(entry.kind, entry.desk_work) for entry in feed.entries]


def unclaimed(requestor: str = REQUESTER, created_hours: float = 600) -> TicketRow:
    return TicketRow(
        widget="unclaimed",
        ticket_id=1,
        title="Laptop will not connect",
        requestor=requestor,
        resp_group="Example Group",
        created=NOW - timedelta(hours=created_hours),
        modified=None,
        prim_resp=None,
        status=None,
        detail_url=URL,
    )


def judged(feed: TicketFeed, row: TicketRow | None = None, **options) -> AuditResult:
    known = options.pop("known_desk", None)
    return evaluate(
        [row or unclaimed()], {1: feed}, NOW, RuleSettings(**options), TZ, known_desk=known
    )


# --- what kind of entry it is ------------------------------------------------------------


@both
def test_a_private_status_change_alone_is_activity_and_its_author_is_desk(read) -> None:
    # This used to read as a written note, which could hold a ticket as in hand.
    feed = read(Item(DESK, 24, STATUS, private=True))

    assert facts(feed) == [("system", True)]
    assert DESK in feed.desk_authors


@both
def test_a_field_change_with_a_typed_comment_is_a_written_note(read) -> None:
    feed = read(Item(DESK, 24, f"{STATUS}<br>{TYPED}", private=True))

    assert facts(feed) == [("private", True)]


@both
@pytest.mark.parametrize("body", [TASK, APPROVED, SELECTED], ids=["task", "approved", "selected"])
def test_private_task_and_workflow_lines_are_activity(read, body: str) -> None:
    feed = read(Item(DESK, 24, body, private=True))

    assert facts(feed) == [("system", True)]
    assert DESK in feed.desk_authors


@both
def test_the_system_workflow_line_is_not_desk_work(read) -> None:
    feed = read(Item(SYSTEM, 24, WORKFLOW, private=True))

    assert facts(feed) == [("system", False)]
    assert feed.desk_authors == ()
    assert strong_desk_names([unclaimed()], {1: feed}) == []


@both
@pytest.mark.parametrize("body", [TASK, APPROVED, SELECTED, STATUS])
def test_an_activity_line_and_a_typed_sentence_is_a_written_note(read, body: str) -> None:
    feed = read(Item(DESK, 24, f"{body}<br>{TYPED}", private=True))

    assert facts(feed) == [("private", True)]


@both
def test_a_sentence_on_the_same_line_as_a_workflow_step_is_written(read) -> None:
    feed = read(Item(DESK, 24, f"{APPROVED} {TYPED}", private=True))

    assert feed.entries[0].kind == "private"


@both
def test_the_activity_phrases_ignore_case(read) -> None:
    feed = read(Item(DESK, 24, APPROVED.upper(), private=True))

    assert feed.entries[0].kind == "system"


@both
def test_an_unknown_phrase_stays_a_note(read) -> None:
    feed = read(Item(DESK, 24, 'Reopened the "Example" step.', private=True))

    assert feed.entries[0].kind == "private"


@both
@pytest.mark.parametrize(
    "what", ["incident", "service request", "problem", "change", "release", "ticket"]
)
def test_every_reassignment_phrase_is_a_notice_by_the_desk(read, what: str) -> None:
    feed = read(
        Item(
            DESK,
            24,
            f"Reassigned this {what} from Group A to Group B.",
            private=True,
            replies=(Item(DESK, 23, TYPED),),
        )
    )

    # The nested reply inherits nothing from a TDX notice, so it is visible.
    assert {entry.kind for entry in feed.entries} == {"system", "reply"}
    assert next(e for e in feed.entries if e.kind == "system").desk_work is False  # routing
    assert DESK in feed.desk_authors


@both
def test_a_reply_inherits_private_and_needs_written_text_too(read) -> None:
    feed = read(
        Item(
            DESK,
            30,
            TYPED,
            private=True,
            replies=(Item(DESK, 28, STATUS), Item(DESK, 26, "Still waiting on the part.")),
        )
    )

    assert facts(feed) == [("private", True), ("system", True), ("private", True)]


@both
def test_a_visible_status_change_by_staff_is_desk_work(read) -> None:
    feed = read(Item(DESK, 24, STATUS))

    assert facts(feed) == [("system", True)]


@both
def test_the_requester_is_never_desk_work(read) -> None:
    feed = read(Item(REQUESTER, 24, STATUS), Item(REQUESTER, 30, TYPED))

    assert [entry.desk_work for entry in feed.entries] == [False, False]


@both
def test_a_visible_reply_is_not_desk_work(read) -> None:
    feed = read(Item(DESK, 24, TYPED))

    assert facts(feed) == [("reply", False)]


@both
def test_with_the_requester_unknown_a_visible_status_change_does_not_count(read) -> None:
    feed = read(
        Item(OTHER, 10, STATUS),
        Item(DESK, 20, STATUS, private=True),
        Item(DESK, 30, "Reassigned this ticket from Group A to Group B."),
        requester=None,
    )

    assert [entry.desk_work for entry in feed.entries] == [False, True, False]


# --- routing and clean-up are not desk work ----------------------------------------------


@both
@pytest.mark.parametrize("private", [False, True], ids=["visible", "private"])
@pytest.mark.parametrize(
    "body",
    [
        REASSIGNED,
        "Reassigned this service request from Group A to Group B.",
        MERGED,
        REQUESTOR_CHANGE,
        ACCOUNT_CHANGE,
        f"{ACCOUNT_CHANGE}<br>{REQUESTOR_CHANGE}",
        PRIORITY_CHANGE,
        RESPONSIBILITY_CHANGE,
        TITLE_CHANGE,
    ],
)
def test_routing_leaves_no_reply_yet_unheld(read, body: str, private: bool) -> None:
    feed = read(Item(DESK, 24, body, private=private))

    assert facts(feed) == [("system", False)]
    result = judged(feed)
    assert [f.reason for f in result.findings] == ["no_reply_yet"]
    assert result.counts["no_reply_work_hold"] == 0


@both
@pytest.mark.parametrize(
    "body",
    [REASSIGNED, MERGED, REQUESTOR_CHANGE, f"{ACCOUNT_CHANGE}<br>{REQUESTOR_CHANGE}"],
    ids=["reassigned", "merged", "requestor", "account_and_requestor"],
)
def test_the_author_of_private_routing_is_still_desk(read, body: str) -> None:
    feed = read(Item(DESK, 24, body, private=True))

    assert DESK in feed.desk_authors


@both
def test_a_status_change_beside_routing_counts(read) -> None:
    feed = read(Item(DESK, 24, f"{REQUESTOR_CHANGE}<br>{STATUS}", private=True))

    assert facts(feed) == [("system", True)]
    assert judged(feed).counts["no_reply_work_hold"] == 1


@both
def test_a_private_status_change_holds_with_the_requester_unknown_too(read) -> None:
    feed = read(Item(DESK, 24, STATUS, private=True), requester=None)

    result = judged(feed, unclaimed(""))

    assert result.findings == () and result.counts["no_reply_work_hold"] == 1


@both
def test_a_visible_status_change_holds_only_with_the_requester_known(read) -> None:
    known = judged(read(Item(DESK, 24, STATUS)))
    assert known.findings == () and known.counts["no_reply_work_hold"] == 1

    unknown = judged(read(Item(DESK, 24, STATUS), requester=None), unclaimed(""))
    assert [f.reason for f in unknown.findings] == ["no_reply_yet"]
    assert unknown.counts["no_reply_work_hold"] == 0


@both
def test_a_private_task_update_with_a_percent_change_holds(read) -> None:
    result = judged(read(Item(DESK, 24, TASK, private=True)))

    assert result.findings == () and result.counts["no_reply_work_hold"] == 1


@both
@pytest.mark.parametrize("private", [False, True], ids=["visible", "private"])
@pytest.mark.parametrize(
    ("body", "still_untouched"), [(REASSIGNED, True), (REQUESTOR_CHANGE, True), (STATUS, False)]
)
def test_routing_after_the_post_does_not_clear_still_untouched(
    read, body: str, still_untouched: bool, private: bool
) -> None:
    finding = Finding(1, "client_waiting", "high", "no_reply_yet", 100)
    key = (1, "client_waiting")
    feed = read(Item(DESK, 1, body, private=private))

    left = untouched_keys(
        [finding], {1: feed}, {1: unclaimed()}, NOW, {key: NOW - timedelta(hours=4)}
    )

    assert (key in left) is still_untouched


def test_a_ticket_created_entry_is_never_desk_work() -> None:
    item = api_entry(1, DESK, NOW - timedelta(hours=24), STATUS, update_type=UPDATE_TYPE_CREATED)

    feed = map_feed(1, [item], {}, TZ, requester=REQUESTER).feed

    assert facts(feed) == [("system", False)]


def test_the_two_readers_agree_on_a_whole_feed() -> None:
    items = (
        Item(REQUESTER, 2, TYPED),
        Item(DESK, 5, STATUS, private=True),
        Item(DESK, 6, f"{STATUS}<br>{TYPED}", private=True),
        Item(DESK, 8, TASK, private=True),
        Item(DESK, 9, APPROVED, private=True),
        Item(DESK, 10, SELECTED, private=True),
        Item(SYSTEM, 11, WORKFLOW, private=True),
        Item(DESK, 12, "Reassigned this service request from Group A to Group B."),
        Item(DESK, 14, STATUS),
        Item(DESK, 16, TYPED, private=True, replies=(Item(DESK, 15, PERCENT),)),
        Item(DESK, 17, REQUESTOR_CHANGE, private=True),
        Item(DESK, 18, ACCOUNT_CHANGE),
        Item(DESK, 19, MERGED),
        Item(OTHER, 20, TYPED),
    )

    from_page, from_api = browser(*items), api(*items)

    assert facts(from_page) == facts(from_api)
    assert sorted(from_page.desk_authors) == sorted(from_api.desk_authors)


def test_repr_shows_no_desk_work_and_no_author() -> None:
    text = repr(FeedEntry(DESK, NOW, "system", True))

    assert "desk_work" not in text and DESK not in text


# --- System is never desk ----------------------------------------------------------------


def test_an_old_memory_holding_system_does_not_make_it_desk() -> None:
    feed = TicketFeed(1, (FeedEntry(SYSTEM, NOW - timedelta(hours=96), "reply"),))

    result = judged(feed, known_desk=lambda name: name == SYSTEM)

    assert [(f.kind, f.reason) for f in result.findings] == [("needs_review", "other_last")]


def test_system_as_the_requester_is_not_an_internal_ticket() -> None:
    feed = TicketFeed(1, (FeedEntry(OTHER, NOW - timedelta(hours=96), "system"),))

    result = judged(feed, unclaimed(SYSTEM), known_desk=lambda name: name == SYSTEM)

    assert result.counts["internal_requestor"] == 0
    assert [f.reason for f in result.findings] == ["no_reply_yet"]


def test_system_on_many_tickets_is_not_guessed_to_be_desk() -> None:
    rows = [
        TicketRow(
            widget="unclaimed",
            ticket_id=number,
            title="Laptop",
            requestor=REQUESTER,
            resp_group="Example Group",
            created=NOW - timedelta(hours=200),
            modified=None,
            prim_resp=None,
            status=None,
            detail_url=URL,
        )
        for number in (1, 2)
    ]
    feeds = {
        number: TicketFeed(number, (FeedEntry(SYSTEM, NOW - timedelta(hours=96), "reply"),))
        for number in (1, 2)
    }

    result = evaluate(rows, feeds, NOW, RuleSettings(), TZ)

    assert {f.reason for f in result.findings} == {"other_last"}


# --- in hand -----------------------------------------------------------------------------


@both
def test_a_private_field_edit_no_longer_holds_ready_to_close(read) -> None:
    feed = read(Item(DESK, 48, STATUS, private=True), Item(DESK, 120, TYPED))

    assert in_hand_note(feed) is None
    result = judged(feed)
    assert [(f.kind, f.reason) for f in result.findings] == [
        ("ready_to_close", "desk_replied_last")
    ]


@both
def test_a_written_note_still_holds_ready_to_close(read) -> None:
    feed = read(
        Item(DESK, 48, "Laptop is waiting for pickup.", private=True), Item(DESK, 120, TYPED)
    )

    assert in_hand_note(feed) is not None
    result = judged(feed)
    assert result.findings == ()
    assert result.counts["desk_note_hold"] == 1


# --- no visible reply --------------------------------------------------------------------


@both
def test_recent_staff_work_holds_no_reply_yet(read) -> None:
    result = judged(read(Item(DESK, 72, TASK, private=True)))

    assert result.findings == ()
    assert result.counts["no_reply_work_hold"] == 1
    assert "No reply yet held by desk work: 1." in format_audit_summary(result, show_ids=False)[1]


@both
def test_old_staff_work_comes_back_naming_it(read) -> None:
    result = judged(read(Item(DESK, 288, TYPED, private=True)))

    assert result.findings == (
        Finding(1, "client_waiting", "high", "no_reply_worked", 600, note_hours=288),
    )
    assert result.counts["no_reply_work_hold"] == 0
    assert finding_wording(result.findings[0], Reader("desk", "the Commons")) == (
        "No reply yet, opened 25 days ago. The Commons last worked on it 12 days ago: reply or "
        "check in"
    )


@both
def test_the_newest_desk_work_decides(read) -> None:
    result = judged(read(Item(DESK, 24, STATUS), Item(DESK, 288, TYPED, private=True)))

    assert result.findings == ()
    assert result.counts["no_reply_work_hold"] == 1


@both
def test_system_workflow_entries_leave_no_reply_yet(read) -> None:
    result = judged(read(Item(SYSTEM, 24, WORKFLOW, private=True)))

    assert [f.reason for f in result.findings] == ["no_reply_yet"]
    assert result.counts["no_reply_work_hold"] == 0


def test_undated_desk_work_does_not_hold() -> None:
    feed = TicketFeed(1, (FeedEntry(DESK, None, "system", True),))

    assert [f.reason for f in judged(feed).findings] == ["no_reply_yet"]


@both
def test_a_desk_member_requester_stays_internal(read) -> None:
    # The private activity makes the requester desk staff; the internal check comes first.
    result = judged(read(Item(DESK, 288, STATUS, private=True)), unclaimed(DESK))

    assert result.findings == ()
    assert result.counts["internal_requestor"] == 1
    assert result.counts["no_reply_work_hold"] == 0


def test_the_hold_length_is_the_setting() -> None:
    feed = TicketFeed(1, (FeedEntry(DESK, NOW - timedelta(days=2), "system", True),))

    assert [f.reason for f in judged(feed, waiting_hold_days=1).findings] == ["no_reply_worked"]


# --- the 7-day hold on requester-last tickets --------------------------------------------


@pytest.mark.parametrize(
    ("note_days", "reasons", "held"), [(5, [], 1), (8, ["requestor_last_noted"], 0)]
)
def test_a_note_holds_a_requester_last_ticket_for_seven_days(
    note_days: int, reasons: list[str], held: int
) -> None:
    feed = TicketFeed(
        1,
        (
            FeedEntry(DESK, NOW - timedelta(days=note_days), "private", True),
            FeedEntry(REQUESTER, NOW - timedelta(days=10), "reply"),
        ),
    )

    result = judged(feed)

    assert [f.reason for f in result.findings] == reasons
    assert result.counts["requester_note_hold"] == held


# --- the card ----------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["no_reply_yet", "no_reply_worked"])
def test_the_requester_waiting_tip_covers_the_no_reply_reasons(reason: str) -> None:
    rows = [Finding(1, "client_waiting", "high", reason, 100)]

    assert section_hint("client_waiting", rows, 7) == REQUESTER_WAITING_HINT.format(hold="7 days")
    assert "7 days" in section_hint("client_waiting", rows, 7)


@pytest.mark.parametrize("reason", ["event_requester_replied", "expects_reply"])
def test_the_tip_stays_off_where_a_note_holds_nothing(reason: str) -> None:
    rows = [Finding(1, "client_waiting", "high", reason, 100)]

    assert section_hint("client_waiting", rows, 7) is None


def test_desk_work_counts_as_desk_activity_and_system_does_not() -> None:
    assert desk_activity(FeedEntry(DESK, NOW, "system", True), REQUESTER)
    assert not desk_activity(FeedEntry(SYSTEM, NOW, "system", False), REQUESTER)


def test_a_staff_status_change_after_the_post_clears_still_untouched() -> None:
    finding = Finding(1, "client_waiting", "high", "no_reply_yet", 100)
    key = (1, "client_waiting")
    shown = {key: NOW - timedelta(hours=4)}
    slot = NOW

    def still(*entries: FeedEntry) -> frozenset:
        feeds = {1: TicketFeed(1, entries)}
        return untouched_keys([finding], feeds, {1: unclaimed()}, slot, shown)

    assert still(FeedEntry(DESK, NOW - timedelta(hours=1), "system", True)) == frozenset()
    assert still(FeedEntry(SYSTEM, NOW - timedelta(hours=1), "system", False)) == {key}
    assert still(FeedEntry(DESK, NOW - timedelta(hours=6), "system", True)) == {key}
