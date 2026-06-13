"""Task 7.2 — config-knob honoring + ambient funnel observability.

These tests validate Requirement 7.1 (group behavior reads its tuning knobs from
``config`` rather than hardcoded literals) and Requirement 7.2 (each funnel-drop
stage is observable) for the no-LLM ambient gate.

Conventions (per ``tests/conftest.py`` / ``tests/test_hardening.py``):
- Each test uses a *fresh* ``AmbientGate()`` so per-chat in-memory state never
  leaks between scenarios.
- ``now`` and ``rng`` are injected for fully deterministic decisions (no real
  clock, no real randomness).
- Config overrides are set on the singleton ``config`` and restored in a
  ``finally`` block, exactly like the existing hardening tests.

Observability is asserted primarily at the gate level: ``AmbientGate.decide``
returns the exact stage string for every drop/pass (``cooldown`` / ``no_trigger``
/ ``dice`` / ``pass``). This is the cleanest, most robust assertion of the funnel
being observable, since the routing layer logs whatever stage ``decide`` reports.
A second, optional test additionally drives the routing-level drop log through
``messages._maybe_ambient_chime`` and captures it via a temporary loguru sink.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import config
from app.services.group_gate import AmbientGate


class _FixedRng:
    """Deterministic stand-in for the ``random`` module's ``.random()``.

    Returns a fixed value on every call so the dice-roll outcome is fully
    controlled by the configured probability, not chance.
    """

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


# ---------------------------------------------------------------------------
# 1. Cooldown knob honored (Req 7.1)
# ---------------------------------------------------------------------------
def test_cooldown_knob_blocks_then_releases():
    """A large cooldown blocks a chime right after one; a tiny cooldown does not.

    Asserts the behavior *changes* with ``GROUP_AMBIENT_COOLDOWN_SECS``: with a
    huge cooldown the gate drops at the ``cooldown`` stage immediately after a
    chime; with a tiny cooldown the same elapsed gap is past the window, so the
    candidate is no longer cooldown-blocked.
    """
    original = config.GROUP_AMBIENT_COOLDOWN_SECS
    t = 1000.0
    try:
        # --- Large cooldown: still inside the window → dropped at "cooldown". ---
        config.GROUP_AMBIENT_COOLDOWN_SECS = 10_000.0
        gate_big = AmbientGate()
        gate_big.mark_chimed(42, t)
        should, stage = gate_big.decide(
            42, affinity=1.0, mode="auto", triggered=True, now=t + 1.0,
            rng=_FixedRng(0.0),
        )
        assert should is False
        assert stage == "cooldown"

        # --- Tiny cooldown: same small gap is now past the window. ---
        config.GROUP_AMBIENT_COOLDOWN_SECS = 0.001
        gate_small = AmbientGate()
        gate_small.mark_chimed(42, t)
        should2, stage2 = gate_small.decide(
            42, affinity=1.0, mode="auto", triggered=True, now=t + 1.0,
            rng=_FixedRng(0.0),
        )
        # No longer cooldown-blocked; with a trigger + winning dice it passes.
        assert stage2 != "cooldown"
        assert should2 is True
        assert stage2 == "pass"
    finally:
        config.GROUP_AMBIENT_COOLDOWN_SECS = original


# ---------------------------------------------------------------------------
# 2. Base-rate knob honored (Req 7.1)
# ---------------------------------------------------------------------------
def test_base_rate_knob_changes_dice_outcome():
    """With a fixed RNG of 0.3, the base-rate knob flips the dice outcome.

    p = GROUP_AMBIENT_BASE_RATE * affinity(1.0) * mode_factor(auto=1.0).
    - base rate 0.2 → p=0.2; draw 0.3 >= 0.2 → drop at "dice".
    - base rate 0.5 → p=0.5; draw 0.3 <  0.5 → pass.
    Fresh gates per scenario so the (never-chimed) cooldown check passes and does
    not interfere.
    """
    original = config.GROUP_AMBIENT_BASE_RATE
    rng = _FixedRng(0.3)
    try:
        # --- Low base rate → dice drop. ---
        config.GROUP_AMBIENT_BASE_RATE = 0.2
        gate_low = AmbientGate()
        should_low, stage_low = gate_low.decide(
            7, affinity=1.0, mode="auto", triggered=True, now=0.0, rng=rng,
        )
        assert should_low is False
        assert stage_low == "dice"

        # --- High base rate → pass. ---
        config.GROUP_AMBIENT_BASE_RATE = 0.5
        gate_high = AmbientGate()
        should_high, stage_high = gate_high.decide(
            7, affinity=1.0, mode="auto", triggered=True, now=0.0, rng=rng,
        )
        assert should_high is True
        assert stage_high == "pass"
    finally:
        config.GROUP_AMBIENT_BASE_RATE = original


# ---------------------------------------------------------------------------
# 3. Context-scan-every knob honored (Req 7.1)
# ---------------------------------------------------------------------------
def test_context_scan_every_knob_controls_tick_cadence():
    """With GROUP_CONTEXT_SCAN_EVERY=3 and no cheap trigger, only every 3rd msg ticks.

    Messages 1 and 2 have no trigger and are not on a scan tick → drop at
    ``no_trigger``. Message 3 hits ``counter % 3 == 0`` → becomes a scan tick →
    reaches the dice stage where (rng=0.0, affinity=1.0, auto) wins → pass.
    A single gate is reused across the three calls so the per-chat counter
    advances 1 → 2 → 3.
    """
    original_scan = config.GROUP_CONTEXT_SCAN_EVERY
    original_rate = config.GROUP_AMBIENT_BASE_RATE
    try:
        config.GROUP_CONTEXT_SCAN_EVERY = 3
        config.GROUP_AMBIENT_BASE_RATE = 0.25  # p = 0.25 > 0 so 0.0 draw wins
        gate = AmbientGate()
        rng = _FixedRng(0.0)

        should1, stage1 = gate.decide(
            5, affinity=1.0, mode="auto", triggered=False, now=0.0, rng=rng,
        )
        assert should1 is False
        assert stage1 == "no_trigger"

        should2, stage2 = gate.decide(
            5, affinity=1.0, mode="auto", triggered=False, now=0.0, rng=rng,
        )
        assert should2 is False
        assert stage2 == "no_trigger"

        # 3rd message: counter % 3 == 0 → scan tick → reaches dice → pass.
        should3, stage3 = gate.decide(
            5, affinity=1.0, mode="auto", triggered=False, now=0.0, rng=rng,
        )
        assert should3 is True
        assert stage3 == "pass"
    finally:
        config.GROUP_CONTEXT_SCAN_EVERY = original_scan
        config.GROUP_AMBIENT_BASE_RATE = original_rate


# ---------------------------------------------------------------------------
# 4a. Drop-stage observability at the gate level (Req 7.2) — PRIMARY.
# ---------------------------------------------------------------------------
def test_decide_reports_each_drop_stage():
    """``decide`` returns the correct stage string for every funnel outcome.

    This is the cleanest, most robust observability assertion: the routing layer
    logs exactly the stage ``decide`` reports, so verifying every stage here
    proves the funnel is observable at each drop point and on pass.
    """
    original_cd = config.GROUP_AMBIENT_COOLDOWN_SECS
    original_scan = config.GROUP_CONTEXT_SCAN_EVERY
    original_rate = config.GROUP_AMBIENT_BASE_RATE
    try:
        config.GROUP_AMBIENT_COOLDOWN_SECS = 10_000.0
        config.GROUP_CONTEXT_SCAN_EVERY = 12  # message #1 is never a scan tick
        config.GROUP_AMBIENT_BASE_RATE = 0.25

        # cooldown: chimed recently, still inside the (huge) window.
        gate_cd = AmbientGate()
        gate_cd.mark_chimed(1, 100.0)
        assert gate_cd.decide(
            1, affinity=1.0, mode="auto", triggered=True, now=101.0,
            rng=_FixedRng(0.0),
        ) == (False, "cooldown")

        # no_trigger: no cheap trigger and not a scan tick (counter 1, every=12).
        gate_nt = AmbientGate()
        assert gate_nt.decide(
            2, affinity=1.0, mode="auto", triggered=False, now=0.0,
            rng=_FixedRng(0.0),
        ) == (False, "no_trigger")

        # dice: triggered, but the draw loses (0.9 >= p=0.25).
        gate_dice = AmbientGate()
        assert gate_dice.decide(
            3, affinity=1.0, mode="auto", triggered=True, now=0.0,
            rng=_FixedRng(0.9),
        ) == (False, "dice")

        # dice (quiet): mode_factor 0 hard-zeroes p, so a quiet member drops at dice.
        gate_quiet = AmbientGate()
        assert gate_quiet.decide(
            4, affinity=1.0, mode="quiet", triggered=True, now=0.0,
            rng=_FixedRng(0.0),
        ) == (False, "dice")

        # pass: triggered and the draw wins (0.0 < p=0.25).
        gate_pass = AmbientGate()
        assert gate_pass.decide(
            5, affinity=1.0, mode="auto", triggered=True, now=0.0,
            rng=_FixedRng(0.0),
        ) == (True, "pass")
    finally:
        config.GROUP_AMBIENT_COOLDOWN_SECS = original_cd
        config.GROUP_CONTEXT_SCAN_EVERY = original_scan
        config.GROUP_AMBIENT_BASE_RATE = original_rate


# ---------------------------------------------------------------------------
# 4b. Routing-level drop log emitted (Req 7.2) — OPTIONAL, via loguru sink.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_routing_emits_drop_stage_log():
    """``_maybe_ambient_chime`` emits the per-stage drop log when the gate drops.

    loguru does not route through pytest's caplog by default, so we attach a
    temporary debug sink, drive the routing helper with a patched gate (forcing a
    ``cooldown`` drop) and a stubbed affinity read, then assert the stage appears
    in the captured output. The sink is always removed in ``finally``.
    """
    from loguru import logger
    from app.handlers import messages

    sink: list[str] = []
    sink_id = logger.add(lambda m: sink.append(str(m)), level="DEBUG")
    try:
        fake_message = SimpleNamespace(
            chat=SimpleNamespace(id=-100123, type="supergroup"),
            from_user=SimpleNamespace(id=222),
            bot=object(),
        )
        member = {"affinity": 0.5, "mode": "auto"}

        with patch.object(
            messages.affinity_cache, "get", new=AsyncMock(return_value=member)
        ), patch.object(
            messages.ambient_gate, "decide", return_value=(False, "cooldown")
        ), patch.object(
            messages.user_task_manager, "enqueue_message", new=AsyncMock()
        ) as mock_enqueue:
            await messages._maybe_ambient_chime(
                fake_message, db=None, user_text="just chatting", sender_name="Bob"
            )

        captured = "\n".join(sink)
        assert "ambient drop stage=cooldown" in captured
        # A dropped candidate must never reach the LLM enqueue path.
        mock_enqueue.assert_not_awaited()
    finally:
        logger.remove(sink_id)
