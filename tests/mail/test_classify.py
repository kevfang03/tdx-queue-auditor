from pathlib import Path

import pytest

from ops_suite.common.config import load_settings
from ops_suite.mail.classify import MailRules, Verdict, classify
from ops_suite.mail.drop_folder import parse_drop_file

RULES = MailRules(
    mailbox_address="shared-mailbox@example.edu",
    automated_senders=("notifications@tdx.example.edu", "@lists.example.edu"),
    automated_local_parts=("no-reply", "noreply", "mailer-daemon"),
)


def test_rule_1_own_mailbox(make_message) -> None:
    message = make_message(sender="shared-mailbox@example.edu")

    assert classify(message, RULES) == Verdict(False, "own_mailbox")


def test_rule_2_automated_sender_full_address(make_message) -> None:
    message = make_message(sender="notifications@tdx.example.edu")

    assert classify(message, RULES) == Verdict(False, "automated_sender")


def test_rule_2_automated_sender_whole_domain(make_message) -> None:
    message = make_message(sender="anyone@lists.example.edu")

    assert classify(message, RULES) == Verdict(False, "automated_sender")


def test_rule_2_domain_entry_does_not_match_other_domains(make_message) -> None:
    for sender in ("anyone@sub.lists.example.edu", "anyone@notlists.example.edu"):
        assert classify(make_message(sender=sender), RULES) == Verdict(True, "person")


def test_rule_3_automated_local_part(make_message) -> None:
    for sender in ("no-reply@vendor.example.com", "noreply+bounces@vendor.example.com"):
        verdict = classify(make_message(sender=sender), RULES)
        assert verdict == Verdict(False, "automated_local_part")


def test_rule_3_matches_the_start_of_the_local_part_only(make_message) -> None:
    message = make_message(sender="jane.noreply@example.edu")

    assert classify(message, RULES) == Verdict(True, "person")


def test_rule_4_auto_submitted(make_message) -> None:
    message = make_message(headers={"auto-submitted": "auto-replied"})

    assert classify(message, RULES) == Verdict(False, "auto_submitted")


def test_rule_4_auto_submitted_no_is_a_person(make_message) -> None:
    message = make_message(headers={"auto-submitted": "No"})

    assert classify(message, RULES) == Verdict(True, "person")


@pytest.mark.parametrize("value", ["bulk", "list", "junk", "Bulk"])
def test_rule_5_bulk_precedence(make_message, value: str) -> None:
    message = make_message(headers={"precedence": value})

    assert classify(message, RULES) == Verdict(False, "bulk_precedence")


def test_rule_5_other_precedence_is_a_person(make_message) -> None:
    message = make_message(headers={"precedence": "first-class"})

    assert classify(message, RULES) == Verdict(True, "person")


def test_rule_6_list_mail(make_message) -> None:
    message = make_message(headers={"list-unsubscribe": "<mailto:leave@lists.example.com>"})

    assert classify(message, RULES) == Verdict(False, "list_mail")


def test_rule_7_person(make_message) -> None:
    assert classify(make_message(), RULES) == Verdict(True, "person")


def test_rule_order_own_mailbox_wins_over_list_header(make_message) -> None:
    message = make_message(
        sender="shared-mailbox@example.edu",
        headers={"list-unsubscribe": "<mailto:leave@lists.example.com>", "precedence": "bulk"},
    )

    assert classify(message, RULES) == Verdict(False, "own_mailbox")


def test_rule_order_headers_are_checked_in_table_order(make_message) -> None:
    message = make_message(
        headers={
            "list-unsubscribe": "<mailto:leave@lists.example.com>",
            "precedence": "bulk",
            "auto-submitted": "auto-generated",
        }
    )

    assert classify(message, RULES) == Verdict(False, "auto_submitted")


def test_comparisons_ignore_case(make_message) -> None:
    rules = MailRules(
        mailbox_address="Shared-Mailbox@Example.EDU",
        automated_senders=("Notifications@TDX.example.edu", "@Lists.Example.edu"),
        automated_local_parts=("No-Reply",),
    )

    assert classify(make_message(sender="shared-mailbox@example.edu"), rules).reason == (
        "own_mailbox"
    )
    assert classify(make_message(sender="notifications@tdx.example.edu"), rules).reason == (
        "automated_sender"
    )
    assert classify(make_message(sender="news@lists.example.edu"), rules).reason == (
        "automated_sender"
    )
    assert classify(make_message(sender="no-reply@example.com"), rules).reason == (
        "automated_local_part"
    )
    assert classify(make_message(headers={"Auto-Submitted": "AUTO-REPLIED"}), rules).reason == (
        "auto_submitted"
    )


def test_blank_rule_entries_match_nothing(make_message) -> None:
    rules = MailRules(
        mailbox_address="", automated_senders=("", "  "), automated_local_parts=("", " ")
    )

    assert classify(make_message(), rules) == Verdict(True, "person")


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("01_student_wifi.json", Verdict(True, "person")),
        ("02_staff_printer.json", Verdict(True, "person")),
        ("03_tdx_notification.json", Verdict(False, "automated_sender")),
        ("04_noreply_vendor.json", Verdict(False, "automated_local_part")),
        ("05_auto_reply.json", Verdict(False, "auto_submitted")),
        ("06_newsletter.json", Verdict(False, "list_mail")),
        ("07_desk_reply.json", Verdict(False, "own_mailbox")),
        ("08_mixed_case_list_to.json", Verdict(True, "person")),
    ],
)
def test_fixtures_classify_as_documented(
    drop_dir: Path, test_config: Path, file_name: str, expected: Verdict
) -> None:
    rules = MailRules.from_settings(load_settings(test_config).mail.work)

    assert classify(parse_drop_file(drop_dir / file_name), rules) == expected
