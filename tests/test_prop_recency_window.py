"""Property test for recency-window implicit classification (Task 3.2).

# Feature: implicit-bot-addressing, Property 1: Recency-window implicit
# classification — a non-spam, non-directed message is classified implicit iff
# the bot has spoken and its last message is within BOTH the elapsed-time bound
# and the intervening-human-message bound of the Bot_Recency_Window.

**Validates: Requirements 1.2, 1.3, 1.4, 6.1, 6.2**

Pure/deterministic: a fresh ``ImplicitAddressGate()`` per example, an injected
``now``, and config knobs overridden with set/restore in ``try/finally``.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import ImplicitAddressGate

_RECENCY_SECS = 120.0
_MAX_MSGS = 4


@settings(max_examples=200)
@given(
    bot_spoke=st.booleans(),
    elapsed=st.floats(min_value=0.0, max_value=400.0, allow_nan=False, allow_infinity=False),
    intervening=st.integers(min_value=0, max_value=10),
)
def test_recency_window_implicit_classification(bot_spoke, elapsed, intervening):
    orig_secs = config.GROUP_IMPLICIT_RECENCY_SECS
    orig_msgs = config.GROUP_IMPLICIT_RECENCY_MAX_MSGS
    config.GROUP_IMPLICIT_RECENCY_SECS = _RECENCY_SECS
    config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = _MAX_MSGS
    try:
        gate = ImplicitAddressGate()
        chat = 7001
        t0 = 1000.0

        if bot_spoke:
            gate.note_bot_spoke(chat, t0)
            # Build up the intervening-human-message counter.
            for _ in range(intervening):
                gate.note_human_message(chat, t0)

        now = t0 + elapsed
        is_implicit, reason = gate.decide(
            chat, directed_at_other=False, is_spam=False, now=now
        )

        if not bot_spoke:
            # Req 1.4: never spoke → never implicit.
            assert (is_implicit, reason) == (False, "no_bot_activity")
        else:
            within = elapsed <= _RECENCY_SECS and intervening <= _MAX_MSGS
            if within:
                # Req 1.2: inside the window → implicit.
                assert (is_implicit, reason) == (True, "implicit")
            else:
                # Req 1.3: outside either bound → not implicit.
                assert (is_implicit, reason) == (False, "out_of_window")
    finally:
        config.GROUP_IMPLICIT_RECENCY_SECS = orig_secs
        config.GROUP_IMPLICIT_RECENCY_MAX_MSGS = orig_msgs
