"""Property test for burst-aware explicit address (Task 7.6).

# Feature: implicit-bot-addressing, Property 13: Burst-aware explicit address —
# a Greeting_Burst_Spam message is treated as an Explicit_Address iff it replies
# to one of the bot's own messages; a bare bot @mention inside a burst message
# (with no reply-to-bot) is NOT explicit; a genuine explicit address that is not
# part of a burst still uses the existing addressed path.

**Validates: Requirements 10.6, 10.7, 10.9**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import SpamBurstDetector, is_addressed

_BOT_USERNAME = "ThinkMateBot"
_BOT_NAME = "ThinkMate"


def _explicit_decision(*, text, entities, reply_to_bot, spam):
    """Mirror the router's spam-aware explicit-address decision (Component A3)."""
    if reply_to_bot:
        return True
    if spam:
        return False
    return is_addressed(
        text=text,
        entities=entities,
        reply_to_bot=False,
        bot_username=_BOT_USERNAME,
        bot_name=_BOT_NAME,
    )


def _greeting_with_bot_mention():
    """A short greeting that @mentions the bot's own username."""
    text = f"hi @{_BOT_USERNAME}"
    offset = len("hi ")
    entity = SimpleNamespace(
        type="mention", offset=offset, length=len(_BOT_USERNAME) + 1
    )
    return text, [entity]


@settings(max_examples=200)
@given(reply_to_bot=st.booleans())
def test_burst_aware_explicit_address(reply_to_bot):
    orig_count = config.GROUP_SPAM_BURST_COUNT
    orig_window = config.GROUP_SPAM_BURST_WINDOW_SECS
    orig_sim = config.GROUP_SPAM_BURST_SIMILARITY
    config.GROUP_SPAM_BURST_COUNT = 3
    config.GROUP_SPAM_BURST_WINDOW_SECS = 1000.0
    config.GROUP_SPAM_BURST_SIMILARITY = 0.85
    try:
        det = SpamBurstDetector()
        chat = 9031
        text, entities = _greeting_with_bot_mention()

        # Drive a near-identical greeting burst (mention-stripped content is "hi"
        # for every message) until it is classified as Greeting_Burst_Spam.
        is_burst = False
        for i in range(3):
            is_burst = det.observe(chat, text, entities, now=100.0 + i)
        assert is_burst is True

        # Sanity: the bare bot @mention WOULD be addressed without the spam guard.
        assert (
            is_addressed(
                text=text,
                entities=entities,
                reply_to_bot=False,
                bot_username=_BOT_USERNAME,
                bot_name=_BOT_NAME,
            )
            is True
        )

        explicit = _explicit_decision(
            text=text, entities=entities, reply_to_bot=reply_to_bot, spam=is_burst
        )
        # Req 10.7: a deliberate reply-to-bot survives the burst classification.
        # Req 10.6: otherwise the bare bot @mention inside a burst is NOT explicit.
        assert explicit is reply_to_bot
    finally:
        config.GROUP_SPAM_BURST_COUNT = orig_count
        config.GROUP_SPAM_BURST_WINDOW_SECS = orig_window
        config.GROUP_SPAM_BURST_SIMILARITY = orig_sim


def test_non_burst_genuine_explicit_uses_addressed_path():
    """Req 10.9: a genuine explicit address that is not a burst is explicit.

    A single greeting @mentioning the bot (no burst history) is not classified as
    Greeting_Burst_Spam, so the spam-aware rule falls through to ``is_addressed``
    and treats it as an Explicit_Address.
    """
    orig_count = config.GROUP_SPAM_BURST_COUNT
    config.GROUP_SPAM_BURST_COUNT = 3
    try:
        det = SpamBurstDetector()
        text, entities = _greeting_with_bot_mention()
        is_burst = det.observe(424242, text, entities, now=100.0)
        assert is_burst is False  # lone message, no burst

        explicit = _explicit_decision(
            text=text, entities=entities, reply_to_bot=False, spam=is_burst
        )
        assert explicit is True
    finally:
        config.GROUP_SPAM_BURST_COUNT = orig_count
