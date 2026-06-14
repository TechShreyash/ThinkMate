"""Property test for directed-at-other suppression (Task 3.3).

# Feature: implicit-bot-addressing, Property 2: Directed-at-other suppression —
# a non-explicitly-addressed message that replies to another participant OR
# @mentions another participant is Directed_At_Other, and is therefore never
# classified as an Implicit_Address even inside the recency window.

**Validates: Requirements 2.1, 2.2, 2.3**
"""
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import ImplicitAddressGate, is_directed_at_other


def _mention_entities(n: int):
    """``n`` plain @mention entities (referencing other participants)."""
    return [SimpleNamespace(type="mention", offset=i * 4, length=3) for i in range(n)]


@settings(max_examples=200)
@given(
    reply_to_other=st.booleans(),
    num_mentions=st.integers(min_value=0, max_value=5),
)
def test_directed_at_other_suppression(reply_to_other, num_mentions):
    entities = _mention_entities(num_mentions) if num_mentions else None
    directed = is_directed_at_other(entities=entities, reply_to_other=reply_to_other)

    # Req 2.2 / 2.3: directed iff a reply-to-other OR a mention entity is present.
    expected_directed = bool(reply_to_other or num_mentions > 0)
    assert directed is expected_directed

    # Req 2.1: a directed-at-other message is never implicit, even inside the
    # recency window (bot just spoke, zero intervening messages).
    orig_secs = config.GROUP_IMPLICIT_RECENCY_SECS
    orig_msgs = config.GROUP_IMPLICIT_RECENCY_MAX_MSGS
    config.GROUP_IMPLICIT_RECENCY_SECS = 120.0
    config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = 4
    try:
        gate = ImplicitAddressGate()
        chat = 8002
        t0 = 500.0
        gate.note_bot_spoke(chat, t0)
        is_implicit, reason = gate.decide(
            chat, directed_at_other=directed, is_spam=False, now=t0 + 1.0
        )
        if directed:
            assert (is_implicit, reason) == (False, "directed_at_other")
        else:
            assert (is_implicit, reason) == (True, "implicit")
    finally:
        config.GROUP_IMPLICIT_RECENCY_SECS = orig_secs
        config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = orig_msgs
