"""tdx demo (payload 07): the generated dataset, the demo card, the synthetic-only LLM
client, and the command end to end on the synthetic app (browser tests)."""

import json
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common.config import Settings
from ops_suite.common.llm import LlmClient, LlmDayUsage, LlmResult, LlmUsage, set_api_key
from ops_suite.common.scrub import scrub
from ops_suite.common.teams import set_webhook_url
from ops_suite.tdx.demo.app import PAGES_DIR
from ops_suite.tdx.demo.dataset import (
    EXPECTED_DUPLICATES,
    EXPECTED_FINDINGS,
    EXPECTED_VERDICTS,
    EXPECTED_WEEK_EVENTS,
    demo_pages,
    demo_tickets,
    expected_findings,
)
from ops_suite.tdx.demo.run import DEMO_CARD_TITLE, DEMO_HEADER, DemoLlmClient, demo_card
from ops_suite.tdx.duplicates import possible_duplicates
from ops_suite.tdx.event_form import EventFormOptions, description_text, parse_event_form
from ops_suite.tdx.feed import parse_feed
from ops_suite.tdx.followup import FollowUpAnswer
from ops_suite.tdx.rows import parse_widget
from ops_suite.tdx.rules import RuleSettings, evaluate
from ops_suite.tdx.week import events_this_week

TZ = ZoneInfo("America/New_York")
FEED_END = '<div class="text-center NoPrint js-feed-end">'
DESCRIPTION_MARK = "<!-- ticket description -->"
INDICATOR = '<div class="refresh-indicator-wrapper NoPrint">'
PAGE_URL = "http://127.0.0.1:1/TDWorkManagement/WorkManagement/Dashboard/Dashboard"
WEBHOOK = "https://example.logic.azure.com/workflows/x/triggers/manual/paths/invoke?sig=fake"
NO_LLM_FINDINGS = (
    "Findings: client waiting 6, follow up with client 0, event outreach due 1, "
    "event details pending 1, event over 1, location TBD soon 2, needs review 1, "
    "ready to close 1."
)
LLM_FINDINGS = (
    "Findings: client waiting 4, follow up with client 2, event outreach due 1, "
    "event details pending 1, event over 1, location TBD soon 2, needs review 1, "
    "ready to close 1."
)


# --- the dataset, without a browser -------------------------------------------------------


def _ticket_frame(fragment: bytes, description: bytes | None = None) -> str:
    """The ticket page as the browser shows it once the feed has loaded."""
    html = (PAGES_DIR / "ticket.html").read_text(encoding="utf-8")
    html = html.replace(DESCRIPTION_MARK, (description or b"").decode("utf-8"))
    html = html.replace(FEED_END, fragment.decode("utf-8") + FEED_END)
    return html.replace(INDICATOR, INDICATOR.replace(">", ' style="display: none;">'))


def _audit_pages(now: datetime):
    tickets = demo_tickets(now)
    pages = demo_pages(tickets, now, TZ)
    rows = []
    for name, html in pages.items():
        for widget, kind in (("40101", "unclaimed"), ("40102", "claimed")):
            if name.startswith(f"widgets/{widget}-"):
                rows += parse_widget(html.decode("utf-8"), kind, PAGE_URL, TZ, now).rows
    feeds = {}
    forms = {}
    for ticket in tickets:
        fragment = pages.get(f"feeds/{ticket.ticket_id}.html")
        if fragment is not None:
            frame = _ticket_frame(fragment, pages.get(f"descriptions/{ticket.ticket_id}.html"))
            feed = parse_feed([frame], ticket.ticket_id, TZ, now)
            assert feed is not None and feed.unreadable == 0
            feeds[ticket.ticket_id] = feed
            text = description_text(frame)
            form = parse_event_form(text, TZ, now, EventFormOptions()) if text else None
            if form is not None:
                forms[ticket.ticket_id] = form
    return tickets, rows, feeds, forms


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 17, 0, 5, tzinfo=TZ),
        datetime(2026, 10, 17, 12, 0, tzinfo=TZ),
        datetime(2026, 10, 17, 23, 55, tzinfo=TZ),
        datetime(2026, 11, 1, 1, 30, tzinfo=TZ),  # the night the clocks go back
        datetime(2027, 1, 1, 9, 0, tzinfo=TZ),  # dates in the titles cross a year
    ],
)
def test_the_dataset_gives_the_expected_findings_at_any_time(now: datetime) -> None:
    tickets, rows, feeds, forms = _audit_pages(now)

    result = evaluate(rows, feeds, now, RuleSettings(), TZ, forms=forms)

    assert len(rows) == 27
    assert Counter(row.widget for row in rows) == {"unclaimed": 21, "claimed": 6}
    assert sorted(forms) == [1000019, 1000020, 1000021, 1000024, 1000026, 1000027]
    assert {kind: result.counts[kind] for kind in EXPECTED_FINDINGS} == EXPECTED_FINDINGS
    assert expected_findings(tickets) == {k: v for k, v in EXPECTED_FINDINGS.items() if v}
    by_ticket = {
        finding.ticket_id: f"{finding.kind}/{finding.reason}" for finding in result.findings
    }
    for ticket in tickets:
        if "/" in ticket.expect:
            assert by_ticket[ticket.ticket_id] == ticket.expect
        else:
            assert ticket.ticket_id not in by_ticket
    counts = result.counts
    assert counts["event_recurring"] == 1 and counts["event_multi_date"] == 1
    assert counts["unclaimed_recent"] == 2 and counts["desk_replied_last"] == 2
    assert counts["event_date_unreadable"] == 0 and counts["internal_requestor"] == 0
    assert counts["event_forms"] == 4 and counts["event_marketing_only"] == 1
    assert counts["event_forms_unreadable"] == 0 and counts["event_no_outreach"] == 1
    # Payload 22: renamed events within 14 days are read too.
    assert counts["event_in_progress"] == 1 and counts["event_location_missing"] == 1
    assert result.event_form_missing_ids == (1000007, 1000025)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 17, 0, 5, tzinfo=TZ),
        datetime(2026, 10, 17, 23, 55, tzinfo=TZ),
        datetime(2026, 11, 1, 1, 30, tzinfo=TZ),
        datetime(2027, 1, 1, 9, 0, tzinfo=TZ),
    ],
)
def test_the_dataset_gives_one_duplicate_pair_and_each_event_state(now: datetime) -> None:
    _, rows, feeds, forms = _audit_pages(now)
    rules = RuleSettings()
    result = evaluate(rows, feeds, now, rules, TZ, forms=forms)

    events = events_this_week(rows, forms, result.event_states, now, TZ, rules, 7)

    assert possible_duplicates(rows, 0.6, rules.form_titles) == EXPECTED_DUPLICATES
    assert [(event.ticket_id, event.state) for event in events] == list(
        EXPECTED_WEEK_EVENTS.items()
    )


def test_the_demo_forms_use_both_college_names_and_both_are_judged() -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)
    tickets, rows, feeds, forms = _audit_pages(now)
    titles = {
        ticket.ticket_id: ticket.title
        for ticket in tickets
        if ticket.form is not None and ticket.title.endswith("Request Form")
    }

    result = evaluate(rows, feeds, now, RuleSettings(), TZ, forms=forms)

    assert set(titles.values()) == {"CCI Event Request Form", "SCIS Event Request Form"}
    assert result.counts["event_forms"] == len(titles)
    assert all(forms[ticket_id].unknown_options == 0 for ticket_id in titles)


def test_the_dataset_uses_only_placeholder_people() -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)
    people = {ticket.requestor for ticket in demo_tickets(now)}
    people |= {entry.author for ticket in demo_tickets(now) for entry in ticket.entries}
    placeholders = ("Sample", "Example", "Placeholder", "Testcase", "Desk", "Fulltime", "Vendor")

    assert all(name.split()[-1] in placeholders for name in people)


def test_the_requestor_last_tickets_have_reply_text() -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)
    _, _, feeds, _ = _audit_pages(now)

    for ticket_id in EXPECTED_VERDICTS:
        assert feeds[ticket_id].latest_reply_text


# --- the card and the client --------------------------------------------------------------


def test_demo_card_is_marked_and_the_original_is_untouched() -> None:
    card = {"type": "AdaptiveCard", "fallbackText": "ops-suite: 1 ticket", "body": [{"x": 1}]}

    marked = demo_card(card)

    assert marked["body"][0]["text"] == DEMO_CARD_TITLE
    assert marked["body"][1] == {"x": 1}
    assert marked["fallbackText"] == "DEMO: ops-suite: 1 ticket"
    assert card["body"] == [{"x": 1}] and card["fallbackText"] == "ops-suite: 1 ticket"


def test_the_demo_client_forces_the_switch_for_synthetic_data_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(LlmClient, "classify", lambda self, text, **options: calls.append(options))
    client = DemoLlmClient(Settings(), api_key="sk-ant-test-0000")
    text = scrub("Synthetic text.")

    client.classify(
        text, system="s", output=FollowUpAnswer, data_class="synthetic", purpose="follow_up"
    )
    with pytest.raises(ValueError, match="synthetic data only"):
        client.classify(
            text, system="s", output=FollowUpAnswer, data_class="drexel", purpose="follow_up"
        )

    assert len(calls) == 1
    assert calls[0]["force_enabled"] is True and calls[0]["data_class"] == "synthetic"


def test_send_without_a_webhook_exits_2_before_any_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_browser(*args, **kwargs):
        raise AssertionError("no audit may start")

    monkeypatch.setattr(cli, "read_and_judge", no_browser)

    assert main(["tdx", "demo", "--send"]) == 2
    assert 'run "uv run ops-suite teams set-webhook" first' in capsys.readouterr().err


# --- end to end on the synthetic app ------------------------------------------------------


class FakeDemoClient:
    """Stands in for DemoLlmClient: records each call and answers the expected verdict."""

    instances: list["FakeDemoClient"] = []

    def __init__(self, settings, **options) -> None:
        assert options.get("api_key") == "sk-ant-test-0000"
        self.settings = settings
        self.calls: list[dict] = []
        FakeDemoClient.instances.append(self)

    def classify(self, text, **options):
        self.calls.append(options)
        verdict = EXPECTED_VERDICTS[int(options["ref"])]
        usage = LlmUsage("claude-haiku-5-5", 40, 9, 900, 0, 0.0001)
        return LlmResult(FollowUpAnswer(verdict=verdict), "ok", usage)

    def used_today(self) -> LlmDayUsage:
        return LlmDayUsage("2026-10-17", 3, 120, 27, 2700, 0, 0.0003)


class FakePost:
    def __init__(self) -> None:
        self.cards: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, url: str, card: dict, *, timeout: float) -> int:
        self.urls.append(url)
        self.cards.append(card)
        return 202


@pytest.fixture
def real_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[Path, bytes]:
    """The person's own alert state and desk memory, and an empty temp folder to watch."""
    tdx_dir = tmp_path / "appdata" / "tdx"
    tdx_dir.mkdir(parents=True)
    files = {
        tdx_dir / "alerts-state.json": b'{"schema": 1, "alerts": []}',
        tdx_dir / "desk-members.json": b'{"schema": 1, "salt": "x", "members": {}}',
    }
    for path, content in files.items():
        path.write_bytes(content)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return files


@pytest.mark.browser
def test_demo_without_the_llm_prints_the_findings_and_leaves_nothing_behind(
    tmp_path: Path, real_files: dict[Path, bytes], capsys: pytest.CaptureFixture[str]
) -> None:
    set_api_key("sk-ant-test-0000")  # --no-llm must not use it
    card_path = tmp_path / "card.json"

    code = main(["tdx", "demo", "--no-llm", "--card-json", str(card_path)])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0] == DEMO_HEADER
    assert NO_LLM_FINDINGS in out
    assert 'LLM: off; 3 "requestor wrote last" findings keep their wording.' in out
    assert "Unclaimed: 21 ticket(s) on 3 page(s)." in out
    assert any(
        line.startswith("Unreadable dates: 0. Duplicates: 0. Blocked requests: 0.") for line in out
    )
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["body"][0]["text"] == DEMO_CARD_TITLE
    assert card["fallbackText"].startswith("DEMO: ")
    assert list((tmp_path / "temp").iterdir()) == []  # the temporary data folder is gone
    for path, content in real_files.items():
        assert path.read_bytes() == content
    assert not (tmp_path / "appdata" / "tdx" / "llm-verdicts.json").exists()
    assert not (tmp_path / "appdata" / "tdx" / "browser-profile").exists()


@pytest.mark.browser
def test_demo_with_a_key_classifies_as_synthetic_and_sends_a_demo_card(
    tmp_path: Path,
    real_files: dict[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_api_key("sk-ant-test-0000")
    set_webhook_url(WEBHOOK, [".logic.azure.com"])
    FakeDemoClient.instances = []
    monkeypatch.setattr(cli, "DemoLlmClient", FakeDemoClient)
    post = FakePost()
    monkeypatch.setattr(cli, "post_card", post)

    code = main(["tdx", "demo", "--send"])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert LLM_FINDINGS in out
    (client,) = FakeDemoClient.instances
    assert {call["data_class"] for call in client.calls} == {"synthetic"}
    assert sorted(int(call["ref"]) for call in client.calls) == sorted(EXPECTED_VERDICTS)
    # Built from the person's own settings, so llm-usage.json stays the real one.
    assert client.settings.paths.data_dir == tmp_path / "appdata"
    (card,) = post.cards
    assert card["body"][0]["text"] == DEMO_CARD_TITLE
    assert card["fallbackText"] == (
        "DEMO: ops-suite: 12 tickets need attention (12 new, 0 reminders)"
    )
    assert out[-1] == "Teams: sent 1 demo card (12 new, 0 reminders)."
    assert list((tmp_path / "temp").iterdir()) == []
    for path, content in real_files.items():
        assert path.read_bytes() == content


@pytest.mark.browser
def test_demo_send_to_desk_is_allowed_without_approval_and_shows_no_titles(
    tmp_path: Path,
    real_files: dict[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    desk = WEBHOOK.replace("example", "desk-example")
    set_webhook_url(desk, [".logic.azure.com"], "desk")
    post = FakePost()
    monkeypatch.setattr(cli, "post_card", post)

    code = main(["tdx", "demo", "--send", "--to", "desk", "--no-llm"])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[-1] == "Teams (desk): sent 1 demo card (12 new, 0 reminders)."
    assert post.urls == [desk]
    (card,) = post.cards
    assert card["body"][0]["text"] == DEMO_CARD_TITLE
    text = json.dumps(card)
    for ticket in demo_tickets(datetime.now(TZ)):
        assert ticket.title not in text
    assert "](http://127.0.0.1:" in text  # the ids are still linked
    for path, content in real_files.items():
        assert path.read_bytes() == content
    assert not (tmp_path / "appdata" / "tdx" / "alerts-state-desk.json").exists()


@pytest.mark.browser
def test_demo_summary_shows_events_duplicates_and_ready_to_close(
    tmp_path: Path, real_files: dict[Path, bytes], capsys: pytest.CaptureFixture[str]
) -> None:
    card_path = tmp_path / "summary.json"

    code = main(
        ["tdx", "demo", "--no-llm", "--summary", "--to", "desk", "--card-json", str(card_path)]
    )

    out = capsys.readouterr().out.splitlines()
    assert code == 0 and NO_LLM_FINDINGS in out
    card = json.loads(card_path.read_text(encoding="utf-8"))
    lines = [block["text"] for block in card["body"]]
    assert lines[0] == DEMO_CARD_TITLE
    assert lines[1].startswith("TDX queue at ") and lines[1].endswith(": 13 open (12 new)")
    sections = [line for line in lines if line.endswith(")") and " (" in line[-5:]]
    assert sections == [
        "Events this week (8)",
        "Client waiting (6)",
        "Needs review (1)",
        "Event outreach due (1)",
        "Event details pending (1)",
        "Location TBD soon (2)",
        "Event over (1)",
        "Ready to close (1)",
    ]
    week = lines[lines.index("Events this week (8)") + 1 : lines.index("Client waiting (6)")]
    assert [line.split("]")[0] for line in week] == [
        f"[#{ticket_id}" for ticket_id in EXPECTED_WEEK_EVENTS
    ]
    # One row per state (payload 22), in date order; space and A/V from the form when known.
    assert week[0].endswith("11:00–12:00 · Classroom · A/V: yes · requester replied")
    assert week[1].endswith("18:00–19:00 · — · A/V: — · location missing")
    assert week[2].endswith("15:00 · — · A/V: — · location TBD")
    assert week[3].endswith("12:00–14:00 · Lobby · A/V: no · no outreach needed")
    assert week[4].endswith("14:00–16:00 · Classroom · A/V: yes · in progress (requester answered)")
    assert " · Classroom · A/V: no · waiting on requester since " in week[5]
    assert week[6].endswith("15:00–17:00 · Lobby · A/V: yes · no outreach yet")
    assert week[7].endswith("18:00–20:00 · Other · A/V: no · marketing only")
    assert any(
        "[#1000023](" in line and "(possible duplicate of #1000001)" in line for line in lines
    )
    text = json.dumps(card)
    for ticket in demo_tickets(datetime.now(TZ)):
        assert ticket.requestor not in text and ticket.title not in text  # desk: no titles
    for path, content in real_files.items():
        assert path.read_bytes() == content


@pytest.mark.browser
def test_demo_summary_kind_delta_shows_new_cleared_and_open(
    tmp_path: Path, real_files: dict[Path, bytes], capsys: pytest.CaptureFixture[str]
) -> None:
    card_path = tmp_path / "delta.json"

    code = main(
        ["tdx", "demo", "--no-llm", "--summary-kind", "delta", "--to", "desk"]
        + ["--card-json", str(card_path)]
    )

    assert code == 0
    card = json.loads(card_path.read_text(encoding="utf-8"))
    lines = [block["text"] for block in card["body"]]
    assert lines[0] == DEMO_CARD_TITLE
    assert lines[1].startswith("Since ") and lines[1].endswith(": 2 new · 1 cleared · 13 open")
    assert lines[2].startswith("Shift update, read-only audit ")
    assert lines[3] == "Client waiting (2)"
    assert "Events this week" not in json.dumps(card) and "Ready to close" not in json.dumps(card)
    assert all("(new)" in line for line in lines[4:] if line.startswith("[#"))
    for path, content in real_files.items():
        assert path.read_bytes() == content


def test_the_live_audit_path_has_no_data_class_switch(capsys: pytest.CaptureFixture[str]) -> None:
    """No flag of tdx audit or tdx watch can make a real-TDX run synthetic."""
    for command in ("audit", "watch"):
        with pytest.raises(SystemExit):
            main(["tdx", command, "--help"])
        assert "synthetic" not in capsys.readouterr().out
