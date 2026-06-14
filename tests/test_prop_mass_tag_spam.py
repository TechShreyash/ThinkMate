"""Property test for mass-tag-spam classification and implicit suppression (Task 3.6).

# Feature: implicit-bot-addressing, Property 5: Mass-tag-spam classification and
# implicit suppression — a message with more than the threshold of distinct
# @mentions is Mass_Tag_Spam, and a spam message is never an Implicit_Address
# even inside the recency window.

**Validates: Requirements 9.1, 9.2**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import (
    ImplicitAddressGate,
    count_distinct_mentions,
    is_mass_tag_spam,
)

_THRESHOLD = 5


def _distinct_mention_message(n: int):
    """Build text + ``n`` distinct @mention entities."""
    parts = [f"@user{i}" for i in range(n)]
    text = " ".join(parts)
    entities = []
    offset = 0
    for part in parts:
        entities.append(SimpleNamespace(type="mention", offset=offset, length=len(part)))
        offset += len(part) + 1  # account for the joining space
    return text, entities


@settings(max_examples=200)
@given(num_mentions=st.integers(min_value=0, max_value=15))
def test_mass_tag_spam_classification_and_implicit_suppression(num_mentions):
    text, entities = _distinct_mention_message(num_mentions)
    assert count_distinct_mentions(text, entities) == num_mentions

    is_spam = is_mass_tag_spam(text, entities, threshold=_THRESHOLD)
    # Req 9.1: strict ">" threshold.
    assert is_spam is (num_mentions > _THRESHOLD)

    # Req 9.2: spam is never implicit even inside the recency window.
    orig_secs = config.GROUP_IMPLICIT_RECENCY_SECS
    orig_msgs = config.GROUP_IMPLICIT_RECENCY_MAX_MSGS
    config.GROUP_IMPLICIT_RECENCY_SECS = 120.0
    config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = 4
    try:
        gate = ImplicitAddressGate()
        chat = 5005
        t0 = 100.0
        gate.note_bot_spoke(chat, t0)  # bot just spoke; window wide open
        is_implicit, reason = gate.decide(
            chat, directed_at_other=False, is_spam=is_spam, now=t0 + 1.0
        )
        if is_spam:
            assert (is_implicit, reason) == (False, "spam")
        else:
            assert (is_implicit, reason) == (True, "implicit")
    finally:
        config.GROUP_IMPLICIT_RECENCY_SECS = orig_secs
        config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = orig_msgs
