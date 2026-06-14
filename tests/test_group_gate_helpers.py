"""Unit tests for the pure no-LLM scan helpers in ``group_gate`` (Task 2.3).

These helpers are pure and deterministic, so the tests are plain synchronous
functions with no mocks. Covers Requirements 2.1, 2.2, 2.3, 9.1, 9.6.
"""
from types import SimpleNamespace

from app.services.group_gate import (
    count_distinct_mentions,
    is_directed_at_other,
    is_mass_tag_spam,
)


def _mention(offset: int, length: int):
    """A plain ``@username`` mention entity."""
    return SimpleNamespace(type="mention", offset=offset, length=length)


def _text_mention(offset: int, length: int, user_id: int):
    """An inline-name-link ``text_mention`` entity carrying a user object."""
    return SimpleNamespace(
        type="text_mention",
        offset=offset,
        length=length,
        user=SimpleNamespace(id=user_id),
    )


# ---------------------------------------------------------------------------
# count_distinct_mentions
# ---------------------------------------------------------------------------

def test_count_distinct_mentions_dedups_by_handle():
    """Tagging the same @handle twice counts once (case-insensitive)."""
    text = "hi @alice and @Alice again"
    entities = [_mention(3, 6), _mention(14, 6)]
    assert count_distinct_mentions(text, entities) == 1


def test_count_distinct_mentions_dedups_by_user_id():
    """Two text_mention entities for the same user id count once."""
    text = "Bob Bob"
    entities = [_text_mention(0, 3, 42), _text_mention(4, 3, 42)]
    assert count_distinct_mentions(text, entities) == 1


def test_count_distinct_mentions_mixed_mention_and_text_mention():
    """Mention handles and text_mention user ids both contribute, de-duplicated."""
    text = "@alice @bob carol"
    entities = [
        _mention(0, 6),       # @alice
        _mention(7, 4),       # @bob
        _text_mention(12, 5, 7),  # carol (user 7)
    ]
    assert count_distinct_mentions(text, entities) == 3


def test_count_distinct_mentions_counts_bot_mention():
    """The bot's own mention is NOT excluded from the count (Req 9.4 intent)."""
    text = "@thinkmatebot @alice @bob @carol"
    entities = [_mention(0, 13), _mention(14, 6), _mention(21, 4), _mention(26, 6)]
    assert count_distinct_mentions(text, entities) == 4


def test_count_distinct_mentions_malformed_and_none_return_zero():
    """None / malformed / non-iterable entities degrade to 0 (never raises)."""
    assert count_distinct_mentions("hello", None) == 0
    assert count_distinct_mentions("hello", []) == 0
    assert count_distinct_mentions(None, None) == 0
    # Malformed: bad offsets, missing user, wrong types are all skipped.
    bad = [
        SimpleNamespace(type="mention", offset="x", length=3),
        SimpleNamespace(type="mention", offset=-1, length=3),
        SimpleNamespace(type="text_mention", offset=0, length=3, user=None),
        SimpleNamespace(type="bold", offset=0, length=2),
    ]
    assert count_distinct_mentions("some text here", bad) == 0
    assert count_distinct_mentions("text", 12345) == 0  # non-iterable


# ---------------------------------------------------------------------------
# is_mass_tag_spam (strict > boundary)
# ---------------------------------------------------------------------------

def test_is_mass_tag_spam_strict_boundary():
    """Threshold N: exactly N mentions is NOT spam, N+1 IS spam (strict >)."""
    text = "@a @b @c @d @e @f"
    entities = [_mention(i * 3, 2) for i in range(6)]  # 6 distinct handles
    # 5 distinct mentions at threshold 5 → not spam (strict >).
    five = entities[:5]
    five_text = "@a @b @c @d @e"
    assert count_distinct_mentions(five_text, five) == 5
    assert is_mass_tag_spam(five_text, five, threshold=5) is False
    # 6 distinct mentions at threshold 5 → spam.
    assert count_distinct_mentions(text, entities) == 6
    assert is_mass_tag_spam(text, entities, threshold=5) is True


def test_is_mass_tag_spam_defensive_returns_false():
    """Malformed input degrades to False ("not spam") — Req 9.6."""
    assert is_mass_tag_spam(None, None, threshold=5) is False
    assert is_mass_tag_spam("text", 999, threshold=5) is False


# ---------------------------------------------------------------------------
# is_directed_at_other
# ---------------------------------------------------------------------------

def test_is_directed_at_other_reply_to_other_true():
    """A reply to a non-bot message is Directed_At_Other (Req 2.2)."""
    assert is_directed_at_other(entities=None, reply_to_other=True) is True


def test_is_directed_at_other_mention_other_true():
    """An @mention of another participant is Directed_At_Other (Req 2.3)."""
    entities = [_mention(0, 6)]
    assert is_directed_at_other(entities=entities, reply_to_other=False) is True
    # text_mention also counts.
    tm = [_text_mention(0, 3, 5)]
    assert is_directed_at_other(entities=tm, reply_to_other=False) is True


def test_is_directed_at_other_neither_false():
    """No reply-to-other and no mention entity → not Directed_At_Other (Req 2.1)."""
    assert is_directed_at_other(entities=None, reply_to_other=False) is False
    assert is_directed_at_other(entities=[], reply_to_other=False) is False


def test_is_directed_at_other_defensive_returns_false():
    """Malformed entities degrade to False (never raises)."""
    bad = [SimpleNamespace(type="mention", offset="x", length=None)]
    assert is_directed_at_other(entities=bad, reply_to_other=False) is False
    assert is_directed_at_other(entities=12345, reply_to_other=False) is False
