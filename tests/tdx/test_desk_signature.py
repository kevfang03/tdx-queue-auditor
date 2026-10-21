"""The desk signature as a desk-member signal. Synthetic feeds, invented names."""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.common.config import TdxRulesSettings
from ops_suite.tdx.desk_memory import DeskMemory
from ops_suite.tdx.feed import parse_feed, read_feed_frames
from ops_suite.tdx.followup import strip_quoted
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import RuleSettings
from ops_suite.tdx.watch import judge

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
SIGNATURES = tuple(TdxRulesSettings().desk_signatures)
REQUESTER = "Alex Example"
NEWHIRE = "Taylor Newhire"
DESK = "Pat Helpdesk"
CARRIED = "Jordan Carryover"
URL = "https://tdx.example.edu/TDNext/Apps/1/Tickets/TicketDet?TicketID=1"
SIGNED = "Please try the printer again now.<br>Best,<br>Taylor<br>SCIS Commons Support"
UNSIGNED = "Please try the printer again now."
# Invented strings that must never show up in repr() or a log record.
INVENTED = [REQUESTER, NEWHIRE, DESK, CARRIED, "Taylor", "printer", "Commons", "broken"]


def entry(
    author: str,
    stamp: str,
    comment: str,
    *,
    private: bool = False,
    replies: tuple[tuple[str, str, str], ...] = (),
) -> str:
    marker = '<span class="feed-private">Private</span>' if private else ""
    nested = "".join(
        f'<div class="feed-reply"><span class="feed-participant-name">{name}</span>'
        f'<div class="wrap-text">{text}</div><div class="reply-context">'
        f'<span class="feed-timestamp"><span>{when}</span></span></div></div>'
        for name, when, text in replies
    )
    return (
        '<div role="group"><div class="feed-body">'
        f'<div class="feed-heading"><span class="feed-participant-name">{author}</span>'
        f"{marker}</div>"
        f'<div class="wrap-text">{comment}</div>'
        f'<div class="feed-context"><span class="feed-timestamp"><span>{stamp}</span></span>'
        f"</div>{nested}</div></div>"
    )


def page(*entries: str) -> list[str]:
    return [f'<div class="feed">{"".join(entries)}</div>']


def read(frames: list[str], requester: str | None = REQUESTER, ticket_id: int = 1):
    details = read_feed_frames(
        frames, ticket_id, TZ, NOW, desk_signatures=SIGNATURES, requester=requester
    )
    assert details is not None
    return details


def unclaimed(ticket_id: int) -> TicketRow:
    return TicketRow(
        widget="unclaimed",
        ticket_id=ticket_id,
        title="Printer will not print",
        requestor=REQUESTER,
        resp_group="Example Group",
        created=datetime(2026, 9, 17, 9, 0, tzinfo=TZ),
        modified=None,
        prim_resp=None,
        status=None,
        detail_url=URL,
    )


# --- the feed reader ---------------------------------------------------------------------


def test_a_signed_reply_makes_its_author_desk() -> None:
    details = read(
        page(
            entry(NEWHIRE, "10/5/26 2:00 PM", SIGNED),
            entry(REQUESTER, "10/1/26 9:00 AM", "The printer is broken."),
        )
    )

    assert details.feed.desk_authors == (NEWHIRE,)
    assert details.marker_stats["signature_desk_replies"] == 1


@pytest.mark.parametrize(
    "comment",
    [
        "Thanks!<br>-- CCI Commons Support",  # the old phrase
        "Done.<br>scis   commons<br>SUPPORT",  # case and whitespace, split over lines
        "Done.<br>Sincerely,<br>The SCIS Commons Support team",  # after a sign-off
    ],
)
def test_phrase_case_and_whitespace(comment: str) -> None:
    details = read(page(entry(NEWHIRE, "10/5/26 2:00 PM", comment)))

    assert details.feed.desk_authors == (NEWHIRE,)


def test_a_signed_nested_reply_counts_and_an_author_is_listed_once() -> None:
    details = read(
        page(
            entry(
                REQUESTER,
                "10/1/26 9:00 AM",
                "The printer is broken.",
                replies=((NEWHIRE, "10/2/26 9:00 AM", SIGNED),),
            ),
            entry(NEWHIRE, "10/3/26 9:00 AM", SIGNED),
        )
    )

    assert details.feed.desk_authors == (NEWHIRE,)
    assert details.marker_stats["signature_desk_replies"] == 2


@pytest.mark.parametrize(
    "comment",
    [
        # Gmail: "On ... wrote:" and everything after it.
        "Still broken.<br>On Mon, Oct 5, 2026 at 2:00 PM Taylor Newhire "
        "&lt;taylor@example.edu&gt; wrote:<br>Please try again.<br>SCIS Commons Support",
        # Outlook: the From:/Sent: header block.
        "Still broken.<br>From: Taylor Newhire<br>Sent: Monday<br>To: Alex Example<br>"
        "Please try again.<br>Best,<br>SCIS Commons Support",
        # Lines starting with ">".
        "Still broken.<br>&gt; Please try again.<br>&gt; SCIS Commons Support",
        # Quoted history in a blockquote.
        "Still broken.<blockquote>Please try again.<br>SCIS Commons Support</blockquote>",
    ],
)
def test_a_client_quoting_a_desk_reply_does_not_become_desk(comment: str) -> None:
    details = read(page(entry("Casey Testperson", "10/5/26 2:00 PM", comment)))

    assert details.feed.desk_authors == ()
    assert details.marker_stats["signature_desk_replies"] == 0


def test_nothing_carried_in_a_merge_notice_counts() -> None:
    notice = (
        f"Merged incident 5509999 (Printer, originally created by {CARRIED}):<br>"
        "Please try again.<br>Best,<br>SCIS Commons Support"
    )
    details = read(
        page(
            entry(
                DESK,
                "10/5/26 2:00 PM",
                notice,
                private=True,
                replies=((CARRIED, "10/5/26 3:00 PM", SIGNED),),
            )
        )
    )

    # The notice's author is desk as a merge-notice author, nobody through the signature.
    assert details.feed.desk_authors == (DESK,)
    assert details.marker_stats["merge_notes"] == 1
    assert details.marker_stats["signature_desk_replies"] == 0


def test_a_private_note_is_not_counted_here() -> None:
    details = read(page(entry(DESK, "10/5/26 2:00 PM", SIGNED, private=True)))

    assert [e.kind for e in details.feed.entries] == ["private"]
    assert details.feed.desk_authors == ()
    assert details.marker_stats["signature_desk_replies"] == 0


@pytest.mark.parametrize("requester", [REQUESTER, "Example, Alex"])
def test_the_requester_never_becomes_desk_through_a_signature(requester: str) -> None:
    details = read(page(entry(REQUESTER, "10/5/26 2:00 PM", SIGNED)), requester=requester)

    assert details.feed.desk_authors == ()
    assert details.marker_stats["signature_desk_replies"] == 0


def test_without_a_requester_or_signatures_the_rule_is_off() -> None:
    frames = page(entry(NEWHIRE, "10/5/26 2:00 PM", SIGNED))

    assert read(frames, requester=None).feed.desk_authors == ()
    assert read(frames, requester="").feed.desk_authors == ()
    assert parse_feed(frames, 1, TZ, NOW, requester=REQUESTER).desk_authors == ()


def test_parse_feed_passes_the_signatures_on() -> None:
    frames = page(entry(NEWHIRE, "10/5/26 2:00 PM", SIGNED))

    feed = parse_feed(frames, 1, TZ, NOW, desk_signatures=SIGNATURES, requester=REQUESTER)

    assert feed is not None
    assert feed.desk_authors == (NEWHIRE,)


def test_no_reply_text_is_kept_or_shown() -> None:
    details = read(
        page(
            entry(REQUESTER, "10/6/26 9:00 AM", "Still broken, thanks."),
            entry(NEWHIRE, "10/5/26 2:00 PM", SIGNED),
        )
    )

    shown = repr(details) + repr(details.feed) + "".join(repr(e) for e in details.feed.entries)
    for word in INVENTED:
        assert word not in shown
    # Only the newest reply's text is kept, and it is the requester's.
    assert details.feed.latest_reply_text == "Still broken, thanks."


def test_strip_quoted_can_keep_the_sign_off() -> None:
    text = "Please try again.\nBest,\nTaylor\nSCIS Commons Support\nOn Mon Alex wrote:\n> Hi"

    assert strip_quoted(text) == "Please try again."
    assert strip_quoted(text, keep_sign_off=True) == (
        "Please try again.\nBest,\nTaylor\nSCIS Commons Support"
    )


# --- rules and memory --------------------------------------------------------------------


def test_a_new_assistant_is_desk_on_first_sight_and_remembered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rules = RuleSettings()
    path = tmp_path / "desk-members.json"
    client_reply = entry(REQUESTER, "10/1/26 9:00 AM", "The printer is broken.")

    def feed_of(ticket_id: int, comment: str, memory: DeskMemory | None):
        frames = page(entry(NEWHIRE, "10/5/26 2:00 PM", comment), client_reply)
        feed = read(frames, ticket_id=ticket_id).feed
        return judge([unclaimed(ticket_id)], {ticket_id: feed}, NOW, rules, TZ, memory)

    # Without the signature, the reply 70 hours old from an unknown person needs review.
    before, _ = feed_of(1, UNSIGNED, DeskMemory(tmp_path / "other.json", forget_days=180))
    assert [(f.kind, f.reason) for f in before.findings] == [("needs_review", "other_last")]

    # With it: desk at once, the desk replied last, and the memory learns the author.
    first, remembered = feed_of(1, SIGNED, DeskMemory(path, forget_days=180))
    assert first.findings == ()
    assert first.counts["desk_replied_last"] == 1
    assert first.counts["needs_review"] == 0
    assert remembered == 1
    assert DeskMemory(path, forget_days=180).knows(NEWHIRE)

    # A later run with no signed reply still knows them.
    later, _ = feed_of(2, UNSIGNED, DeskMemory(path, forget_days=180))
    assert later.findings == ()
    assert later.counts["desk_replied_last"] == 1

    # Older than close_after_days: "ready to close", not "needs review".
    old = page(entry(NEWHIRE, "10/2/26 9:00 AM", UNSIGNED), client_reply)
    feed = read(old, ticket_id=3).feed
    closing, _ = judge([unclaimed(3)], {3: feed}, NOW, rules, TZ, DeskMemory(path, forget_days=180))
    assert [(f.kind, f.reason) for f in closing.findings] == [
        ("ready_to_close", "desk_replied_last")
    ]

    stored = path.read_text(encoding="utf-8")
    logged = "\n".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
    for word in INVENTED:
        assert word not in stored
        assert word not in logged
