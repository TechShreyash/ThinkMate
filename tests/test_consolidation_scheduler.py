"""Tests for the periodic consolidation scheduler (Phase 11, Requirements 1.2, 1.4, 1.6, 1.7, 9.5).

These verify ``app.services.health.start_consolidation_scheduler`` and its backing
``_consolidation_loop`` / ``_run_consolidation_scan`` against a mongomock-backed DB
(via the autouse fixture in ``tests/conftest.py``) with ``run_consolidator`` patched
to an ``AsyncMock`` so no real LLM, network, or memory writes occur:

* disabled (interval <= 0, positive or negative) starts no task,
* a scan processes at most ``CONSOLIDATION_MAX_USERS_PER_SCAN`` due users (bounded work),
* a scan continues past a user whose ``run_consolidator`` raises,
* the loop self-heals when ``_run_consolidation_scan`` raises (the task stays alive).

Config is saved/restored around every test and any background task is cancelled/awaited
in a ``finally`` so none leaks between tests (mirrors ``tests/test_metrics_logger.py``).
"""
import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from app.config import config
from app.database.connection import db_session
from app.services import health
from app.services.user_task_manager import user_task_manager


@pytest.fixture
def restore_consolidation_config():
    """Save and restore all consolidation config knobs around a test."""
    keys = (
        "CONSOLIDATION_INTERVAL_SECS",
        "CONSOLIDATION_SCAN_INTERVAL_SECS",
        "CONSOLIDATION_MAX_USERS_PER_SCAN",
        "CONSOLIDATION_MIN_ITEMS",
        "MAX_INSIGHTS",
    )
    originals = {k: getattr(config, k) for k in keys}
    try:
        yield
    finally:
        for k, v in originals.items():
            setattr(config, k, v)


async def _cancel(task):
    """Cancel a background task and await it, suppressing CancelledError."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _seed_due_users(count: int, *, items_per_user: int = 2):
    """Insert ``count`` user_profiles that are due for consolidation.

    Each profile has ``last_consolidated_at`` absent (never consolidated) and
    ``items_per_user`` facts so it clears any small ``CONSOLIDATION_MIN_ITEMS``.
    """
    async with db_session() as db:
        for uid in range(1, count + 1):
            await db["user_profiles"].insert_one(
                {
                    "_id": uid,
                    "profile_summary": "",
                    "communication_style": "",
                    "facts": [{"content": f"fact-{uid}-{i}"} for i in range(items_per_user)],
                    "beliefs": [],
                    "events": [],
                }
            )


# --- Requirement 1.2 / 9.5: disabled when interval <= 0 ---

async def test_disabled_when_interval_zero(restore_consolidation_config):
    """Interval of 0.0 starts no task (returns None). Validates: Requirements 1.2, 9.5"""
    config.CONSOLIDATION_INTERVAL_SECS = 0.0
    task = health.start_consolidation_scheduler()
    try:
        assert task is None
    finally:
        await _cancel(task)


async def test_disabled_when_interval_negative(restore_consolidation_config):
    """A negative interval starts no task (returns None). Validates: Requirements 1.2, 9.5"""
    config.CONSOLIDATION_INTERVAL_SECS = -5.0
    task = health.start_consolidation_scheduler()
    try:
        assert task is None
    finally:
        await _cancel(task)


# --- Requirement 1.4 / 9.5: bounded work per scan ---

async def test_scan_processes_at_most_max_users(restore_consolidation_config, monkeypatch):
    """A scan dispatches at most CONSOLIDATION_MAX_USERS_PER_SCAN due users.

    Validates: Requirements 1.4, 9.5
    """
    config.CONSOLIDATION_INTERVAL_SECS = 3600.0
    config.CONSOLIDATION_SCAN_INTERVAL_SECS = 3600.0
    config.CONSOLIDATION_MIN_ITEMS = 1
    config.CONSOLIDATION_MAX_USERS_PER_SCAN = 3

    # Seed twice as many due users as the per-scan cap.
    await _seed_due_users(6, items_per_user=2)

    mock_run = AsyncMock()
    monkeypatch.setattr(user_task_manager, "run_consolidator", mock_run)

    await health._run_consolidation_scan()

    # The due-user query is itself capped at the limit, so exactly the cap is dispatched.
    assert mock_run.call_count == 3


# --- Requirement 1.6 / 9.5: one user's failure does not abort the scan ---

async def test_scan_continues_past_failing_user(restore_consolidation_config, monkeypatch):
    """A user whose run_consolidator raises does not stop the rest of the scan.

    Validates: Requirements 1.6, 9.5
    """
    config.CONSOLIDATION_INTERVAL_SECS = 3600.0
    config.CONSOLIDATION_SCAN_INTERVAL_SECS = 3600.0
    config.CONSOLIDATION_MIN_ITEMS = 1
    config.CONSOLIDATION_MAX_USERS_PER_SCAN = 50

    await _seed_due_users(4, items_per_user=2)

    state = {"n": 0}

    def side_effect(user_id):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first user blew up")

    mock_run = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(user_task_manager, "run_consolidator", mock_run)

    # Must not raise even though the first dispatched user fails.
    await health._run_consolidation_scan()

    # Every due user was still dispatched (the loop continued past the failure).
    assert mock_run.call_count == 4


# --- Requirement 1.7 / 9.5: the loop self-heals on an iteration error ---

async def test_loop_self_heals_on_error(restore_consolidation_config, monkeypatch):
    """An exception inside a scan iteration does not kill the loop. Validates: Requirements 1.7, 9.5"""
    config.CONSOLIDATION_INTERVAL_SECS = 3600.0
    config.CONSOLIDATION_SCAN_INTERVAL_SECS = 0.02

    boom = AsyncMock(side_effect=RuntimeError("scan blew up"))
    monkeypatch.setattr(health, "_run_consolidation_scan", boom)

    task = None
    try:
        task = health.start_consolidation_scheduler()
        assert task is not None
        # Give the loop a few iterations; each one raises inside the try/except.
        await asyncio.sleep(0.07)
        # The loop must still be alive — the error was swallowed, not propagated.
        assert not task.done()
        # And it actually attempted to scan (the failing scan ran at least once).
        assert boom.await_count >= 1
    finally:
        await _cancel(task)
