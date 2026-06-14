"""Property test for recency and burst state pruning (Task 5.1).

# Feature: implicit-bot-addressing, Property 8: Recency and burst state pruning —
# prune(now, max_idle) on both ImplicitAddressGate and SpamBurstDetector removes
# exactly the chats idle beyond max_idle and keeps recently-active ones.

**Validates: Requirements 6.4, 10.13**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.group_gate import ImplicitAddressGate, SpamBurstDetector

# chat_id -> last activity time
_activity = st.dictionaries(
    keys=st.integers(min_value=1, max_value=10_000),
    values=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    min_size=0,
    max_size=12,
)


@settings(max_examples=200)
@given(
    activity=_activity,
    extra=st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    max_idle=st.floats(min_value=1.0, max_value=800.0, allow_nan=False, allow_infinity=False),
)
def test_recency_and_burst_state_pruning(activity, extra, max_idle):
    now = (max(activity.values()) if activity else 0.0) + extra
    cutoff = now - max_idle
    expected_removed = {cid for cid, seen in activity.items() if seen <= cutoff}
    expected_kept = set(activity) - expected_removed

    # --- ImplicitAddressGate ---
    gate = ImplicitAddressGate()
    for cid, t in activity.items():
        gate.note_bot_spoke(cid, t)
    removed = gate.prune(now, max_idle=max_idle)
    assert removed == len(expected_removed)
    for cid in expected_removed:
        assert cid not in gate._last_seen
        assert cid not in gate._bot_last_spoke
    for cid in expected_kept:
        assert cid in gate._last_seen

    # --- SpamBurstDetector ---
    # Keyed per (chat_id, user_id); these calls omit user_id so the key is (cid, None).
    det = SpamBurstDetector()
    for cid, t in activity.items():
        det.observe(cid, "hi", None, now=t)
    removed_b = det.prune(now, max_idle=max_idle)
    assert removed_b == len(expected_removed)
    for cid in expected_removed:
        assert (cid, None) not in det._last_seen
        assert (cid, None) not in det._recent
    for cid in expected_kept:
        assert (cid, None) in det._last_seen
