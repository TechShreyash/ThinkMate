"""Property test for greeting-burst implicit/ambient suppression (Task 7.5).

# Feature: implicit-bot-addressing, Property 12: Greeting-burst suppresses
# implicit classification and ambient triggers — a Greeting_Burst_Spam message
# is never an Implicit_Address even inside the recency window
# (``decide(..., is_spam=True)`` → not implicit), and the spam-aware trigger
# ``scan_cheap_triggers(text) and not is_spam`` is forced False.

**Validates: Requirements 10.4, 10.5**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import (
    ImplicitAddressGate,
    SpamBurstDetector,
    scan_cheap_triggers,
)

# Low-content greeting bases that are also cheap-trigger keywords.
_greeting = st.sampled_from(["hi", "hello", "hey", "good morning", "gm"])


@settings(max_examples=200)
@given(base=_greeting, extra=st.integers(min_value=0, max_value=4))
def test_burst_suppresses_implicit_and_ambient(base, extra):
    orig_count = config.GROUP_SPAM_BURST_COUNT
    orig_window = config.GROUP_SPAM_BURST_WINDOW_SECS
    orig_sim = config.GROUP_SPAM_BURST_SIMILARITY
    orig_secs = config.GROUP_IMPLICIT_RECENCY_SECS
    orig_msgs = config.GROUP_IMPLICIT_RECENCY_MAX_MSGS
    config.GROUP_SPAM_BURST_COUNT = 3
    config.GROUP_SPAM_BURST_WINDOW_SECS = 1000.0
    config.GROUP_SPAM_BURST_SIMILARITY = 0.85
    config.GROUP_IMPLICIT_RECENCY_SECS = 120.0
    config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = 4
    try:
        det = SpamBurstDetector()
        chat = 8021
        text = base

        # Drive a near-identical greeting burst until it is classified as spam.
        is_burst = False
        for i in range(3 + extra):
            is_burst = det.observe(chat, text, None, now=100.0 + i)
        # The window is wide and similarity high → the burst threshold is reached.
        assert is_burst is True

        # Req 10.4: a burst message is never implicit, even inside the recency
        # window (bot just spoke, zero intervening human messages).
        gate = ImplicitAddressGate()
        gate.note_bot_spoke(chat, 100.0)
        is_implicit, reason = gate.decide(
            chat, directed_at_other=False, is_spam=is_burst, now=101.0
        )
        assert (is_implicit, reason) == (False, "spam")

        # Req 10.5: the spam-aware trigger is forced False for the burst message,
        # even though the greeting word is a cheap trigger.
        assert scan_cheap_triggers(text) is True
        triggered = scan_cheap_triggers(text) and not is_burst
        assert triggered is False
    finally:
        config.GROUP_SPAM_BURST_COUNT = orig_count
        config.GROUP_SPAM_BURST_WINDOW_SECS = orig_window
        config.GROUP_SPAM_BURST_SIMILARITY = orig_sim
        config.GROUP_IMPLICIT_RECENCY_SECS = orig_secs
        config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = orig_msgs
