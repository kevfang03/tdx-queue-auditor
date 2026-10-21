import pytest

from ops_suite.tdx.names import name_key, same_person


@pytest.mark.parametrize(
    "variant",
    ["Alex Example", "alex example", "Example, Alex", "Alex  Example", " EXAMPLE ,ALEX. "],
)
def test_order_case_and_punctuation_variants_share_a_key(variant: str) -> None:
    assert name_key(variant) == ("alex", "example")
    assert same_person(variant, "Alex Example")


def test_punctuation_becomes_spaces() -> None:
    assert name_key("O'Example-Smith, Alex (Al)") == ("al", "alex", "example", "o", "smith")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Alex Q. Example", "Alex Example"),
        ("Example, Alex Quinn", "alex example"),
        ("Alex Example", "Alex Quinn Example"),
    ],
)
def test_a_middle_name_on_one_side_matches(a: str, b: str) -> None:
    assert same_person(a, b)
    assert same_person(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Alex Example", "Casey Example"),
        ("Example, Alex", "Example, Casey"),
        ("Alex Example", "Alex Exampel"),  # no fuzzy matching: a typo is someone else
        ("Example", "Alex Example"),  # one word never matches a longer name
        ("Alex Alex Example", "Alex Example Example"),
    ],
)
def test_different_people_do_not_match(a: str, b: str) -> None:
    assert not same_person(a, b)
    assert not same_person(b, a)


@pytest.mark.parametrize(("a", "b"), [("", ""), ("", "Alex Example"), (" , . ", " , . ")])
def test_empty_names_never_match(a: str, b: str) -> None:
    assert not same_person(a, b)
    assert not same_person(b, a)


def test_single_word_names_match_only_when_equal() -> None:
    assert same_person("Helpdesk", "helpdesk")
    assert not same_person("Helpdesk", "Desk")
