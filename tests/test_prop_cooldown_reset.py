"""Property test for cooldown reset on implicit reply (Task 3.5).

# Feature: implicit-bot-addressing, Property 4: Cooldown reset on implicit reply
# — after mark_implicit_reply(t), cooldown_elapsed(t') is False for every
# t <= t' < t + GROUP_IMPLICIT_COOLDOWN_SECS and True once t' >= t + cooldown.

**Validates: Requirements 3.3**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import ImplicitAddressGate

_COOLDOWN = 30.0


@settings(max_examples=200)
@given(
    reply_at=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=0.0, max_value=2.0 * _COOLDOWN, allow_nan=False, allow_infinity=False),
)
def test_cooldown_reset_on_implicit_reply(reply_at, delta):
    orig = config.GROUP_IMPLICIT_COOLDOWN_SECS
    config.GROUP_IMPLICIT_COOLDOWN_SECS = _COOLDOWN
    try:
        gate = ImplicitAddressGate()
        chat = 4004

        gate.mark_implicit_reply(chat, reply_at)
        later = reply_at + delta
        elapsed = gate.cooldown_elapsed(chat, later)

        # The cooldown is elapsed exactly when the observed gap reaches the
        # configured window (Req 3.3). Compare against the same arithmetic the
        # gate uses to avoid float boundary ambiguity.
        assert elapsed is ((later - reply_at) >= _COOLDOWN)
    finally:
        config.GROUP_IMPLICIT_COOLDOWN_SECS = orig
