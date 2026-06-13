"""Tests for the no-LLM ambient funnel (`AmbientGate`) — Task 4.3.

The gate is pure and deterministic, so these tests are plain synchronous
functions. Each test uses a FRESH ``AmbientGate()`` for state isolation and
injects ``now`` plus a deterministic ``rng`` (an object whose ``.random()``
returns a fixed value) so the affinity-weighted dice roll is reproducible.
Config knobs are overridden with set/restore in ``try/finally`` (mirroring the
pattern in ``tests/test_hardening.py``).

Covers Requirements 3.1, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.10.
"""
from app.config import config
from app.services.group_gate import AmbientGate


class FixedRng:
    """Deterministic stand-in for ``random``: ``.random()`` returns a fixed value."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


def test_cooldown_blocks_second_chime_within_window():
    """Cooldown blocks a second chime within the window (Req 3.1, 3.8).

    Also covers Req 3.7 conceptually: mark_chimed sets the cooldown window
    regardless of whether the (eventual) reply produced any text.
    """
    rng = FixedRng(0.0)  # always passes the dice
    gate = AmbientGate()
    chat = 555
    t = 1000.0

    # First candidate survives the whole funnel.
    chime, stage = gate.decide(
        chat, affinity=1.0, mode="auto", triggered=True, now=t, rng=rng
    )
    assert (chime, stage) == (True, "pass")

    # The dispatch path records the chime (Req 3.7: holds even if reply empty).
    gate.mark_chimed(chat, t)

    # A second candidate a short time later is blocked by the cooldown window.
    cooldown = config.GROUP_AMBIENT_COOLDOWN_SECS
    chime, stage = gate.decide(
        chat, affinity=1.0, mode="auto", triggered=True, now=t + 1.0, rng=rng
    )
    assert (chime, stage) == (False, "cooldown")
    assert 1.0 < cooldown  # precondition: the second message is inside the window

    # Once the cooldown has fully elapsed, candidates can pass again.
    chime, stage = gate.decide(
        chat,
        affinity=1.0,
        mode="auto",
        triggered=True,
        now=t + cooldown + 1.0,
        rng=rng,
    )
    assert (chime, stage) == (True, "pass")


def test_no_trigger_and_not_scan_tick_drops():
    """No trigger and not a scan tick → drop "no_trigger" (Req 3.3)."""
    original = config.GROUP_CONTEXT_SCAN_EVERY
    config.GROUP_CONTEXT_SCAN_EVERY = 1000  # first message is not a scan tick
    try:
        rng = FixedRng(0.0)
        gate = AmbientGate()
        chime, stage = gate.decide(
            777, affinity=1.0, mode="auto", triggered=False, now=10.0, rng=rng
        )
        assert (chime, stage) == (False, "no_trigger")
    finally:
        config.GROUP_CONTEXT_SCAN_EVERY = original


def test_scan_tick_passes_without_keyword_trigger():
    """Scan tick passes without a keyword trigger (Req 3.3 hybrid path)."""
    original = config.GROUP_CONTEXT_SCAN_EVERY
    config.GROUP_CONTEXT_SCAN_EVERY = 1  # every message is a scan tick
    try:
        rng = FixedRng(0.0)
        gate = AmbientGate()
        chime, stage = gate.decide(
            888, affinity=1.0, mode="auto", triggered=False, now=10.0, rng=rng
        )
        assert (chime, stage) == (True, "pass")
    finally:
        config.GROUP_CONTEXT_SCAN_EVERY = original


def test_dice_roll_respects_probability():
    """Dice roll respects probability p = base * affinity * mode_factor (Req 3.4).

    With base_rate=0.25, affinity=1.0, mode="auto" (mode_factor 1.0) → p = 0.25.
    A draw below p passes; a draw at/above p drops at "dice". Distinct chat_ids
    are used so the (never-set) cooldown cannot interfere.
    """
    original = config.GROUP_AMBIENT_BASE_RATE
    config.GROUP_AMBIENT_BASE_RATE = 0.25
    try:
        gate = AmbientGate()

        # draw 0.1 < 0.25 → pass
        chime, stage = gate.decide(
            101, affinity=1.0, mode="auto", triggered=True, now=5.0,
            rng=FixedRng(0.1),
        )
        assert (chime, stage) == (True, "pass")

        # draw 0.9 >= 0.25 → dice drop (distinct chat so no cooldown carryover)
        chime, stage = gate.decide(
            202, affinity=1.0, mode="auto", triggered=True, now=5.0,
            rng=FixedRng(0.9),
        )
        assert (chime, stage) == (False, "dice")
    finally:
        config.GROUP_AMBIENT_BASE_RATE = original


def test_quiet_mode_forces_no_chime():
    """Quiet mode forces no chime: mode_factor 0 → p = 0 → "dice" (Req 3.5).

    Even with a dice that always passes and a trigger present, quiet wins.
    """
    rng = FixedRng(0.0)
    gate = AmbientGate()
    chime, stage = gate.decide(
        303, affinity=1.0, mode="quiet", triggered=True, now=5.0, rng=rng
    )
    assert (chime, stage) == (False, "dice")


def test_budget_bound_over_a_burst():
    """At most one chime over a burst within one cooldown window (Req 3.8).

    With a dice that always passes and triggered=True, 20 messages arrive at the
    same instant. The first passes and is marked; the cooldown then blocks the
    rest — bounding the ambient cost to ~1 per window regardless of volume.
    """
    rng = FixedRng(0.0)
    gate = AmbientGate()
    chat = 404
    fixed_t = 2000.0
    chimes = 0

    for _ in range(20):
        chime, _stage = gate.decide(
            chat, affinity=1.0, mode="auto", triggered=True, now=fixed_t, rng=rng
        )
        if chime:
            # Mirror the dispatch path: record the chime (Req 3.7) so the
            # cooldown holds for the remainder of the window.
            gate.mark_chimed(chat, fixed_t)
            chimes += 1

    assert chimes == 1
    assert chimes <= 1  # explicit budget bound assertion


def test_prune_drops_stale_entries():
    """prune drops stale per-chat state and leaves fresh chats intact (Req 3.10)."""
    rng = FixedRng(0.0)
    gate = AmbientGate()
    cooldown = config.GROUP_AMBIENT_COOLDOWN_SECS
    t = 100.0

    # Warm two chats at t.
    gate.decide(1, affinity=1.0, mode="auto", triggered=True, now=t, rng=rng)
    gate.decide(2, affinity=1.0, mode="auto", triggered=True, now=t, rng=rng)

    # A third chat is active far later (well past the default idle horizon).
    later = t + 10.0 * cooldown + 1.0
    gate.decide(3, affinity=1.0, mode="auto", triggered=True, now=later, rng=rng)

    removed = gate.prune(now=later)

    # The two stale chats are gone; the recently-active chat survives.
    assert removed == 2
    assert 1 not in gate._last_seen
    assert 2 not in gate._last_seen
    assert 1 not in gate._last_chime_time
    assert 1 not in gate._msg_counter
    assert 3 in gate._last_seen  # fresh chat's state is independent


def test_mark_chimed_holds_window_regardless_of_reply():
    """mark_chimed holds the cooldown window even if the reply was empty (Req 3.7).

    The cooldown is set at dispatch time, independent of any model output, so a
    follow-up candidate inside the window is gated. (Conceptually reinforced by
    tests 1 and 6.)
    """
    gate = AmbientGate()
    chat = 606
    t = 3000.0

    # Simulate dispatch with an *empty* model reply: only mark_chimed is called.
    gate.mark_chimed(chat, t)

    chime, stage = gate.decide(
        chat, affinity=1.0, mode="auto", triggered=True, now=t + 1.0,
        rng=FixedRng(0.0),
    )
    assert (chime, stage) == (False, "cooldown")
