"""Property test for spam-aware explicit address (Task 7.4).

# Feature: implicit-bot-addressing, Property 7: Spam-aware explicit address — a
# Mass_Tag_Spam message is treated as an Explicit_Address iff it replies to one
# of the bot's own messages; a bare bot @mention buried in the bulk-tag list
# (with no reply-to-bot) does NOT make the message explicit.

**Validates: Requirements 9.4, 9.5**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.group_gate import is_addressed, is_mass_tag_spam

_THRESHOLD = 5
_BOT_USERNAME = "ThinkMateBot"
_BOT_NAME = "ThinkMate"


def _explicit_decision(*, text, entities, reply_to_bot, spam):
    """Mirror the router's spam-aware explicit-address decision (Component A3).

    ``reply_to_bot`` → explicit; ``elif spam`` → not explicit; else the existing
    ``is_addressed`` scan.
    """
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


def _bulk_tag_including_bot(n: int):
    """A bulk-tag greeting whose mention list includes the bot's own @username.

    The bot's mention is buried among ``n`` other distinct @mentions, so the
    distinct-mention count is ``n + 1``.
    """
    parts = [f"@{_BOT_USERNAME}"] + [f"@user{i}" for i in range(n)]
    text = "hi " + " ".join(parts)
    entities = []
    offset = len("hi ")
    for part in parts:
        entities.append(
            SimpleNamespace(type="mention", offset=offset, length=len(part))
        )
        offset += len(part) + 1
    return text, entities


@settings(max_examples=200)
@given(
    num_others=st.integers(min_value=6, max_value=15),
    reply_to_bot=st.booleans(),
)
def test_spam_aware_explicit_address(num_others, reply_to_bot):
    text, entities = _bulk_tag_including_bot(num_others)

    # The message is Mass_Tag_Spam (count > threshold) and its only bot-addressing
    # signal in the text is the bot @mention inside the bulk list.
    spam = is_mass_tag_spam(text, entities, threshold=_THRESHOLD)
    assert spam is True

    # Sanity: without the spam guard, the bare bot @mention WOULD be addressed —
    # which is exactly what the spam-aware rule must override (Req 9.4).
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
        text=text, entities=entities, reply_to_bot=reply_to_bot, spam=spam
    )

    # Req 9.5: a deliberate reply-to-bot survives the spam classification.
    # Req 9.4: otherwise a bot @mention buried in the bulk list is NOT explicit.
    assert explicit is reply_to_bot
