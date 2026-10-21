"""The scrubber. All text here is synthetic: invented names and 555 numbers."""

import pytest

from ops_suite.common import scrub as scrub_module
from ops_suite.common.scrub import ScrubbedText, ScrubRefused, scrub


def test_known_names_are_replaced_whole_word_any_case():
    result = scrub("Sam Sample asked; SAMPLE said sam would call.", known_names=["Sam Sample"])
    assert result.text == "[NAME] [NAME] asked; [NAME] said [NAME] would call."
    assert result.replaced["name"] == 4


def test_known_name_inside_a_longer_word_is_kept():
    result = scrub("The samples arrived.", known_names=["Sam Sample"])
    assert result.text == "The samples arrived."
    assert result.replaced["name"] == 0


def test_last_first_order_and_middle_initial():
    result = scrub("Alex wrote to Example today.", known_names=["Example, Alex J."])
    assert result.text == "[NAME] wrote to [NAME] today."


def test_two_letter_name_words_are_replaced():
    result = scrub("Jo Example said thanks, jo.", known_names=["Jo Example"])
    assert result.text == "[NAME] [NAME] said thanks, [NAME]."


def test_greeting_names_at_line_start():
    text = "Hi Sam,\nthanks.\n  Good morning Alex Example,\nDear team, hello"
    result = scrub(text)
    assert result.text == "Hi [NAME],\nthanks.\n  Good morning [NAME],\nDear [NAME], hello"
    assert result.replaced["name"] == 3


def test_greeting_after_known_name_is_counted_once():
    result = scrub("Hello Sam,\nok", known_names=["Sam Sample"])
    assert result.text == "Hello [NAME],\nok"
    assert result.replaced["name"] == 1


def test_greeting_mid_line_is_left_alone():
    assert scrub("I said hi Sam, then left.").text == "I said hi Sam, then left."


def test_emails():
    result = scrub("Write to helper@example.edu or a.b+c@mail.example.com.")
    assert result.text == "Write to [EMAIL] or [EMAIL]."
    assert result.replaced["email"] == 2


def test_an_email_with_a_name_in_it_is_one_email():
    result = scrub("From sam.sample@example.edu", known_names=["Sam Sample"])
    assert result.text == "From [EMAIL]"
    assert result.replaced == {"name": 0, "email": 1, "phone": 0, "id": 0, "user": 0, "url": 0}


def test_urls():
    result = scrub("See https://example.edu/help?x=1 and www.example.com/page now.")
    assert result.text == "See [URL] and [URL] now."
    assert result.replaced["url"] == 2


def test_a_url_with_a_name_in_it_is_one_url():
    result = scrub("Profile: https://example.edu/people/sam-sample", known_names=["Sam Sample"])
    assert result.text == "Profile: [URL]"
    assert result.replaced["name"] == 0


@pytest.mark.parametrize(
    "phone",
    [
        "215-555-0100",
        "(215) 555-0100",
        "215.555.0100",
        "+1 215 555 0100",
        "+1-215-555-0100",
        "2155550100",
        "215-555-0100 x1234",
        "215-555-0100 ext. 12",
    ],
)
def test_phone_numbers(phone: str):
    result = scrub(f"Call {phone} today.")
    assert result.text == "Call [PHONE] today."
    assert result.replaced["phone"] == 1


def test_eight_digit_ids():
    result = scrub("ID 12345678, not 1234567 or 123456789.")
    assert result.text == "ID [ID], not 1234567 or 123456789."
    assert result.replaced["id"] == 1


def test_usernames():
    result = scrub("User abc123 and xy99 logged in; abcde123 and a1 did not.")
    assert result.text == "User [USER] and [USER] logged in; abcde123 and a1 did not."
    assert result.replaced["user"] == 2


def test_every_kind_is_counted_even_when_zero():
    result = scrub("Nothing personal here.")
    assert result.text == "Nothing personal here."
    kinds = ["name", "email", "phone", "id", "user", "url"]
    assert dict(result.replaced) == dict.fromkeys(kinds, 0)
    assert result.truncated is False


def test_cap_cuts_on_a_word_boundary():
    result = scrub("alpha beta gamma delta", max_chars=13)
    assert result.text == "alpha beta"
    assert result.truncated is True


def test_cap_counts_after_replacement():
    result = scrub("Mail helper@example.edu now", max_chars=200)
    assert result.truncated is False
    assert scrub("a" * 50, max_chars=10).text == "a" * 10


def test_survivor_of_a_known_name_is_refused(monkeypatch: pytest.MonkeyPatch):
    # Rule 1 is the real defence; the post-check is the net under it. Switch rule 1 off to
    # show the net holds, and that its message holds a count, never the name or the text.
    monkeypatch.setattr(scrub_module, "_replace_known_names", lambda text, names, repl: text)
    with pytest.raises(ScrubRefused) as caught:
        scrub("Sam Sample needs help.", known_names=["Sam Sample"])
    message = str(caught.value)
    assert "Sam" not in message and "Sample" not in message and "help" not in message


def test_two_letter_words_do_not_trigger_the_post_check(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scrub_module, "_replace_known_names", lambda text, names, repl: text)
    assert scrub("Jo is here.", known_names=["Jo"]).text == "Jo is here."


def test_repr_has_counts_and_never_text():
    result = scrub("Sam Sample at helper@example.edu", known_names=["Sam Sample"])
    shown = repr(result)
    assert "Sam" not in shown and "NAME" not in shown and "example" not in shown
    assert "name=2" in shown and "email=1" in shown


def test_scrubbed_text_cannot_be_built_directly():
    with pytest.raises(TypeError):
        ScrubbedText("raw text", {}, False)
    with pytest.raises(TypeError):
        ScrubbedText(text="raw text", replaced={}, truncated=False)


def test_scrubbed_text_is_read_only():
    result = scrub("hello")
    with pytest.raises(AttributeError):
        result.text = "raw"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.replaced["name"] = 5  # type: ignore[index]
