"""Property test for burst similarity excluding @mention tokens (Task 4.2).

# Feature: implicit-bot-addressing, Property 10: Burst similarity excludes
# @mention tokens — the same base greeting content tagging different
# participants reduces to identical mention-stripped content, yielding maximal
# similarity, so such messages are treated as near-identical.

**Validates: Requirements 10.1, 10.2**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import SpamBurstDetector

# A greeting base is some low-content words; handles vary per message.
_greeting = st.sampled_from(["hi", "hello", "good morning", "hey", "yo"])
_handle = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)


def _message_with_mention(base: str, handle: str):
    """Build "``base`` @``handle``" text plus a matching mention entity."""
    text = f"{base} @{handle}"
    offset = len(base) + 1
    entity = SimpleNamespace(type="mention", offset=offset, length=len(handle) + 1)
    return text, [entity]


@settings(max_examples=200)
@given(
    base=_greeting,
    handle_a=_handle,
    handle_b=_handle,
)
def test_burst_similarity_excludes_mention_tokens(base, handle_a, handle_b):
    det = SpamBurstDetector()

    text_a, ent_a = _message_with_mention(base, handle_a)
    text_b, ent_b = _message_with_mention(base, handle_b)

    # Req 10.1: mention-stripped contents are identical regardless of the handle.
    content_a = det._strip_and_normalize(text_a, ent_a)
    content_b = det._strip_and_normalize(text_b, ent_b)
    assert content_a == content_b == base

    # Req 10.2: near-identical content reaching the burst count is classified as
    # a burst even though each message tags a different participant.
    orig_count = config.GROUP_SPAM_BURST_COUNT
    orig_window = config.GROUP_SPAM_BURST_WINDOW_SECS
    orig_sim = config.GROUP_SPAM_BURST_SIMILARITY
    config.GROUP_SPAM_BURST_COUNT = 2
    config.GROUP_SPAM_BURST_WINDOW_SECS = 1000.0
    config.GROUP_SPAM_BURST_SIMILARITY = 0.85
    try:
        chat = 6010
        assert det.observe(chat, text_a, ent_a, now=1.0) is False  # first of pair
        assert det.observe(chat, text_b, ent_b, now=2.0) is True   # reaches count 2
    finally:
        config.GROUP_SPAM_BURST_COUNT = orig_count
        config.GROUP_SPAM_BURST_WINDOW_SECS = orig_window
        config.GROUP_SPAM_BURST_SIMILARITY = orig_sim
