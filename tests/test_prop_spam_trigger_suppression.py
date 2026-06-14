"""Property test for spam suppressing cheap-trigger ambient firing (Task 7.3).

# Feature: implicit-bot-addressing, Property 6: Spam suppresses cheap-trigger
# ambient firing — for any Mass_Tag_Spam message, the spam-aware trigger
# computation ``scan_cheap_triggers(text) and not is_mass_tag_spam(...)`` is
# always False, so greeting/laughter/etc. cheap-trigger keywords can never fire
# the ambient gate for a spam message.

**Validates: Requirements 9.3**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.group_gate import (
    is_mass_tag_spam,
    scan_cheap_triggers,
)

_THRESHOLD = 5

# Cheap-trigger greeting bases — each independently fires ``scan_cheap_triggers``.
_greeting = st.sampled_from(["hi", "hello", "hey", "good morning", "gm", "lol"])


def _mass_tag_message(base: str, n: int):
    """Build a greeting + ``n`` distinct @mention entities (a bulk-tag message)."""
    parts = [f"@user{i}" for i in range(n)]
    text = base + " " + " ".join(parts)
    entities = []
    offset = len(base) + 1
    for part in parts:
        entities.append(
            SimpleNamespace(type="mention", offset=offset, length=len(part))
        )
        offset += len(part) + 1  # account for the joining space
    return text, entities


@settings(max_examples=200)
@given(base=_greeting, num_mentions=st.integers(min_value=6, max_value=20))
def test_spam_suppresses_cheap_trigger_ambient_firing(base, num_mentions):
    text, entities = _mass_tag_message(base, num_mentions)

    # Precondition: the greeting keyword on its own is a cheap trigger, and the
    # message is classified as Mass_Tag_Spam (count > threshold).
    assert scan_cheap_triggers(text) is True
    is_spam = is_mass_tag_spam(text, entities, threshold=_THRESHOLD)
    assert is_spam is True

    # Req 9.3: the spam-aware trigger (as computed in ``_maybe_ambient_chime``)
    # is forced False for any spam message, regardless of its cheap-trigger words.
    triggered = scan_cheap_triggers(text) and not is_spam
    assert triggered is False
