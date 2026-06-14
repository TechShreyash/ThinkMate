"""Property test for greeting-burst classification threshold (Task 4.3).

# Feature: implicit-bot-addressing, Property 11: Greeting-burst classification
# threshold within the window — a message is classified as Greeting_Burst_Spam
# iff the count of near-identical messages within GROUP_SPAM_BURST_WINDOW_SECS
# reaches GROUP_SPAM_BURST_COUNT; lone, sub-threshold, or beyond-window-spaced
# greetings are not classified as a burst.

**Validates: Requirements 10.3, 10.8**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import SpamBurstDetector

_WINDOW = 60.0


@settings(max_examples=200)
@given(
    burst_count=st.integers(min_value=2, max_value=5),
    num_messages=st.integers(min_value=1, max_value=9),
    within_window=st.booleans(),
)
def test_greeting_burst_threshold(burst_count, num_messages, within_window):
    orig_count = config.GROUP_SPAM_BURST_COUNT
    orig_window = config.GROUP_SPAM_BURST_WINDOW_SECS
    orig_sim = config.GROUP_SPAM_BURST_SIMILARITY
    orig_track = config.GROUP_SPAM_BURST_TRACK_MAX
    config.GROUP_SPAM_BURST_COUNT = burst_count
    config.GROUP_SPAM_BURST_WINDOW_SECS = _WINDOW
    config.GROUP_SPAM_BURST_SIMILARITY = 0.85
    config.GROUP_SPAM_BURST_TRACK_MAX = 50
    try:
        det = SpamBurstDetector()
        chat = 7011
        text = "good morning"
        # Tight spacing keeps messages inside the window; wide spacing evicts the
        # prior message before the next arrives.
        spacing = 1.0 if within_window else (_WINDOW + 10.0)

        for i in range(num_messages):
            now = 100.0 + i * spacing
            result = det.observe(chat, text, None, now=now)
            if within_window:
                # Including the just-added message, count == i + 1.
                expected = (i + 1) >= burst_count
            else:
                # Each prior entry is evicted → count is always 1 < burst_count.
                expected = False
            assert result is expected
    finally:
        config.GROUP_SPAM_BURST_COUNT = orig_count
        config.GROUP_SPAM_BURST_WINDOW_SECS = orig_window
        config.GROUP_SPAM_BURST_SIMILARITY = orig_sim
        config.GROUP_SPAM_BURST_TRACK_MAX = orig_track
