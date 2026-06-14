"""Property test for implicit-cooldown bounding of direct replies (Task 3.4).

# Feature: implicit-bot-addressing, Property 3: Implicit cooldown bounds direct
# replies — across a burst of implicit candidates arriving within a single
# Implicit_Cooldown window, the gate authorizes at most one direct reply; once
# the cooldown has fully elapsed a new candidate may reply again.

**Validates: Requirements 3.1, 4.1, 4.4**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import config
from app.services.group_gate import ImplicitAddressGate

_COOLDOWN = 30.0


@settings(max_examples=200)
@given(
    # Arrival offsets (seconds) of a burst of implicit candidates, all strictly
    # inside one cooldown window.
    offsets=st.lists(
        st.floats(min_value=0.0, max_value=_COOLDOWN - 0.001, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=12,
    ),
)
def test_implicit_cooldown_bounds_direct_replies(offsets):
    orig = config.GROUP_IMPLICIT_COOLDOWN_SECS
    config.GROUP_IMPLICIT_COOLDOWN_SECS = _COOLDOWN
    try:
        gate = ImplicitAddressGate()
        chat = 9003
        t0 = 2000.0

        replies = 0
        # Process the burst in arrival order (mirrors the router commit pattern:
        # check cooldown_elapsed, then mark_implicit_reply before enqueue).
        for off in sorted(offsets):
            now = t0 + off
            if gate.cooldown_elapsed(chat, now):
                gate.mark_implicit_reply(chat, now)
                replies += 1

        # Req 4.4 / 4.1: at most one direct reply within a single window.
        assert replies == 1

        # After the cooldown fully elapses (measured from the last reply, which
        # is the earliest candidate), a new candidate may reply again.
        later = t0 + max(offsets) + _COOLDOWN + 1.0
        assert gate.cooldown_elapsed(chat, later) is True
    finally:
        config.GROUP_IMPLICIT_COOLDOWN_SECS = orig
