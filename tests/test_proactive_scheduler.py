"""Tests for the Phase 12 proactive check-in scheduler, scan, and sender.

Covers ``app.services.health``:

* ``start_proactive_scheduler`` enable/disable gating,
* ``_in_quiet_hours`` pure-function correctness (same-day, midnight-wrap, start==end),
* ``_run_proactive_scan`` dispatch bounding, per-user resilience, and quiet-hour skip,
* ``_proactive_loop`` self-healing on a raising scan,
* ``_send_proactive_checkin`` sent / skipped / failed paths.

Uses the mongomock + pytest-asyncio harness from ``tests/conftest.py`` (autouse
``mock_mongodb``), an autouse ``metrics.reset()`` fixture for counter isolation, a
mocked aiogram ``Bot`` with ``send_message=AsyncMock``, and config save/restore +
cancel/await of any background task in ``finally`` (mirrors test_metrics_logger.py).

Lazily-imported names (health.py imports ``app.database.models`` and ``llm_service``
inside the functions) are patched at their source module so the patch is observed.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import config
from app.database import connection, models
from app.services import health
from app.services.metrics import metrics


# --- shared helpers / fixtures -----------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_metrics():
    """Isolate metric state per test (mirrors test_engagement_units)."""
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
def restore_proactive_config():
    """Save and restore every PROACTIVE_* config field this module mutates."""
    names = (
        "PROACTIVE_INTERVAL_SECS",
        "PROACTIVE_INACTIVITY_SECS",
        "PROACTIVE_MIN_INTERVAL_SECS",
        "PROACTIVE_MAX_PER_SCAN",
        "PROACTIVE_MIN_ITEMS",
        "PROACTIVE_QUIET_START_HOUR",
        "PROACTIVE_QUIET_END_HOUR",
    )
    saved = {name: getattr(config, name) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(config, name, value)


async def _cancel(task):
    """Cancel a background task and await it, suppressing CancelledError."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _counters():
    return metrics.snapshot()["counters"]


# --- 1. start_proactive_scheduler gating -------------------------------------------------

async def test_start_proactive_scheduler_disabled_when_interval_zero(restore_proactive_config):
    """Interval of 0.0 starts no task (returns None)."""
    config.PROACTIVE_INTERVAL_SECS = 0.0
    task = health.start_proactive_scheduler(bot=None)
    try:
        assert task is None
    finally:
        await _cancel(task)


async def test_start_proactive_scheduler_disabled_when_interval_negative(restore_proactive_config):
    """A negative interval starts no task (returns None)."""
    config.PROACTIVE_INTERVAL_SECS = -1.0
    task = health.start_proactive_scheduler(bot=None)
    try:
        assert task is None
    finally:
        await _cancel(task)


async def test_start_proactive_scheduler_enabled_returns_task(restore_proactive_config, monkeypatch):
    """A positive interval starts a live background task."""
    config.PROACTIVE_INTERVAL_SECS = 0.02
    # Keep the loop inert so the running task never touches the DB/LLM.
    monkeypatch.setattr(health, "_run_proactive_scan", AsyncMock())
    bot = AsyncMock()
    task = health.start_proactive_scheduler(bot)
    try:
        assert task is not None
        assert not task.done()
    finally:
        await _cancel(task)


# --- 2. _in_quiet_hours pure-function correctness ----------------------------------------

def test_in_quiet_hours_wraps_midnight():
    """start>end wraps midnight: hours at/after start OR before end are quiet."""
    # quiet window 22:00 -> 07:00
    assert health._in_quiet_hours(23, 22, 7) is True   # late night
    assert health._in_quiet_hours(3, 22, 7) is True    # early morning
    assert health._in_quiet_hours(22, 22, 7) is True   # inclusive start
    assert health._in_quiet_hours(7, 22, 7) is False   # exclusive end
    assert health._in_quiet_hours(12, 22, 7) is False  # midday, awake


def test_in_quiet_hours_same_day_window():
    """start<end is a simple [start, end) interval."""
    assert health._in_quiet_hours(3, 1, 5) is True    # inside
    assert health._in_quiet_hours(1, 1, 5) is True    # inclusive start
    assert health._in_quiet_hours(5, 1, 5) is False   # exclusive end
    assert health._in_quiet_hours(6, 1, 5) is False   # after
    assert health._in_quiet_hours(0, 1, 5) is False   # before


@pytest.mark.parametrize("hour", [0, 5, 12, 22, 23])
def test_in_quiet_hours_start_equals_end_is_never_quiet(hour):
    """start==end means there is no quiet window for any hour."""
    assert health._in_quiet_hours(hour, 9, 9) is False


# --- 3. scan bounding, resilience, and quiet-hour skip -----------------------------------

async def test_run_proactive_scan_dispatches_once_per_due_user(restore_proactive_config, monkeypatch):
    """Every id returned by find_users_due_for_proactive is dispatched exactly once."""
    config.PROACTIVE_QUIET_START_HOUR = 0
    config.PROACTIVE_QUIET_END_HOUR = 0  # disable quiet hours

    due_ids = [101, 102, 103, 104]
    monkeypatch.setattr(
        "app.database.models.find_users_due_for_proactive",
        AsyncMock(return_value=list(due_ids)),
    )
    sender = AsyncMock(return_value="sent")
    monkeypatch.setattr(health, "_send_proactive_checkin", sender)

    bot = AsyncMock()
    await health._run_proactive_scan(bot)

    assert sender.await_count == len(due_ids)
    dispatched = {call.args[1] for call in sender.await_args_list}
    assert dispatched == set(due_ids)
    counters = _counters()
    assert counters["proactive.runs"] == 1
    assert counters["proactive.sent"] == len(due_ids)


async def test_run_proactive_scan_continues_past_failing_user(restore_proactive_config, monkeypatch):
    """One user's raising send must not abort the scan; the rest still process."""
    config.PROACTIVE_QUIET_START_HOUR = 0
    config.PROACTIVE_QUIET_END_HOUR = 0

    due_ids = [201, 202, 203]
    monkeypatch.setattr(
        "app.database.models.find_users_due_for_proactive",
        AsyncMock(return_value=list(due_ids)),
    )

    processed: list[int] = []

    async def flaky_send(bot, user_id, *, now):
        processed.append(user_id)
        if user_id == 202:
            raise RuntimeError("send blew up for 202")
        return "sent"

    monkeypatch.setattr(health, "_send_proactive_checkin", flaky_send)

    bot = AsyncMock()
    await health._run_proactive_scan(bot)

    # All three users were attempted despite the middle one raising.
    assert processed == due_ids
    counters = _counters()
    assert counters["proactive.sent"] == 2
    assert counters["proactive.failed"] == 1


async def test_run_proactive_scan_skips_within_quiet_hours(restore_proactive_config, monkeypatch):
    """Inside quiet hours the scan increments runs but never finds/sends."""
    monkeypatch.setattr(health, "_in_quiet_hours", lambda hour, start, end: True)

    find_mock = AsyncMock(return_value=[1, 2, 3])
    monkeypatch.setattr("app.database.models.find_users_due_for_proactive", find_mock)
    sender = AsyncMock(return_value="sent")
    monkeypatch.setattr(health, "_send_proactive_checkin", sender)

    bot = AsyncMock()
    await health._run_proactive_scan(bot)

    sender.assert_not_awaited()
    find_mock.assert_not_awaited()
    assert _counters()["proactive.runs"] == 1


# --- 4. _proactive_loop self-heals -------------------------------------------------------

async def test_proactive_loop_self_heals_on_scan_error(monkeypatch):
    """A scan that always raises must not kill the loop (errors are swallowed)."""
    monkeypatch.setattr(
        health, "_run_proactive_scan", AsyncMock(side_effect=RuntimeError("scan boom"))
    )
    bot = AsyncMock()
    task = asyncio.ensure_future(health._proactive_loop(bot, 0.02))
    try:
        # Give the loop a few iterations; each raises inside the try/except.
        await asyncio.sleep(0.08)
        assert not task.done()
    finally:
        await _cancel(task)


# --- 5. _send_proactive_checkin sender paths ---------------------------------------------

async def _seed_user_with_memory(user_id: int):
    """Create a profile with a few facts so build_memory_block yields non-empty text."""
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "user", "User")
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {"$set": {"facts": [
                {"category": "personal", "content": "loves hiking"},
                {"category": "personal", "content": "works as an engineer"},
                {"category": "personal", "content": "has a dog named Rex"},
            ]}},
        )


async def test_send_proactive_checkin_includes_time_context(restore_proactive_config, monkeypatch):
    """The proactive LLM prompt includes current UTC time, without a fabricated gap."""
    user_id = 90000
    await _seed_user_with_memory(user_id)

    generate_checkin = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.llm_service.llm_service.generate_checkin",
        generate_checkin,
    )
    bot = AsyncMock()
    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)

    outcome = await health._send_proactive_checkin(bot, user_id, now=now)

    assert outcome == "skipped"
    generate_checkin.assert_awaited_once()
    system_prompt = generate_checkin.await_args.args[1]
    assert "## ⏰ TIME CONTEXT" in system_prompt
    assert "Current time (UTC): 2024-06-01 14:30" in system_prompt
    assert "Last talked" not in system_prompt


async def test_send_proactive_checkin_sent(restore_proactive_config, monkeypatch):
    """A non-empty opener is delivered, recorded in the buffer, and the window is held."""
    user_id = 90001
    await _seed_user_with_memory(user_id)

    monkeypatch.setattr(
        "app.services.llm_service.llm_service.generate_checkin",
        AsyncMock(return_value="hey there"),
    )
    bot = AsyncMock()
    now = datetime.now(timezone.utc)

    outcome = await health._send_proactive_checkin(bot, user_id, now=now)

    assert outcome == "sent"
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == user_id
    assert bot.send_message.await_args.kwargs["text"] == "hey there"

    async with connection.db_session() as db:
        # Assistant opener appended to the buffer.
        buf = await db["chat_buffers"].find_one({"_id": user_id})
        assert buf is not None
        assistant_msgs = [m for m in buf["messages"] if m["role"] == "assistant"]
        assert any(m["content"] == "hey there" for m in assistant_msgs)
        # last_proactive_at recorded.
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc["last_proactive_at"] is not None
        assert abs(doc["last_proactive_at"].replace(tzinfo=None) - now.replace(tzinfo=None)) < timedelta(milliseconds=1)


async def test_send_proactive_checkin_skipped(restore_proactive_config, monkeypatch):
    """A falsy opener means send nothing, but the rate-limit window is still held."""
    user_id = 90002
    await _seed_user_with_memory(user_id)

    monkeypatch.setattr(
        "app.services.llm_service.llm_service.generate_checkin",
        AsyncMock(return_value=None),
    )
    bot = AsyncMock()
    now = datetime.now(timezone.utc)

    outcome = await health._send_proactive_checkin(bot, user_id, now=now)

    assert outcome == "skipped"
    bot.send_message.assert_not_awaited()
    async with connection.db_session() as db:
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc["last_proactive_at"] is not None


async def test_send_proactive_checkin_failed_disables_user(restore_proactive_config, monkeypatch):
    """A send exception (e.g. Forbidden) disables proactive for the user; window still held."""
    user_id = 90003
    await _seed_user_with_memory(user_id)

    monkeypatch.setattr(
        "app.services.llm_service.llm_service.generate_checkin",
        AsyncMock(return_value="hey"),
    )
    bot = AsyncMock()
    bot.send_message.side_effect = Exception("Forbidden: bot was blocked by the user")
    now = datetime.now(timezone.utc)

    outcome = await health._send_proactive_checkin(bot, user_id, now=now)

    assert outcome == "failed"
    async with connection.db_session() as db:
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc["proactive_enabled"] is False
        assert doc["last_proactive_at"] is not None
