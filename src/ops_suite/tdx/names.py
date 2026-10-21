"""Lenient person-name matching for the stale rules. Pure functions.

The dashboard grid and the activity feed may format the same person differently ("Example,
Alex" in one, "Alex Example" in the other), so names are compared as sorted sets of words.
There is no fuzzy or edit-distance matching: a typo is a different person.

Personal data rule: names are student data. Nothing here logs, prints or raises with a name.
"""

import re
from collections import Counter

# Punctuation that separates name parts: comma, period, apostrophe, hyphen, parentheses.
_PUNCTUATION = re.compile(r"[,.'()\-]")


def name_key(name: str) -> tuple[str, ...]:
    """The words of ``name``, case-folded and sorted, punctuation treated as spaces."""
    return tuple(sorted(_PUNCTUATION.sub(" ", name.casefold()).split()))


def same_person(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` name the same person.

    Equal keys match. Keys of two or more words also match when every word of the shorter
    one is in the longer one, so a middle name on one side only still matches.
    """
    key_a, key_b = name_key(a), name_key(b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    shorter, longer = sorted((key_a, key_b), key=len)
    if len(shorter) < 2:
        return False
    return Counter(shorter) <= Counter(longer)
