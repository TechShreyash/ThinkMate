"""Tests for the optional periodic metrics logger (Requirements 5.2, 5.3, 5.4).

These verify the behavior of ``app.services.health.start_metrics_logger`` and its
backing ``_metrics_logger_loop`` without any DB or LLM access:

* disabled (interval <= 0) starts no task,
* enabled emits at least one ``[metrics]`` log line per interval,
* an error raised inside one iteration is swallowed and the loop keeps running.

All tests cancel/await the background task in a ``finally`` so no task leaks
between tests, and any temporary loguru sink added is removed in ``finally``.
"""
import asyncio
from contextlib import suppress

import pytest
from loguru import logger

from app.config import config
from app.services import health


@pytest.fixture
def restore_interval():
    """Save and restore ``config.METRICS_LOG_INTERVAL_SECS`` around a test."""
    original = config.METRICS_LOG_INTERVAL_SECS
    try:
        yield
    finally:
        config.METRICS_LOG_INTERVAL_SECS = original


async def _cancel(task):
    """Cancel a background task and await it, suppressing CancelledError."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# --- Requirement 5.3: disabled when interval <= 0 ---

async def test_disabled_when_interval_zero(restore_interval):
    """Interval of 0.0 starts no task (returns None). Validates: Requirements 5.3"""
    config.METRICS_LOG_INTERVAL_SECS = 0.0
    task = health.start_metrics_logger()
    try:
        assert task is None
    finally:
        await _cancel(task)


async def test_disabled_when_interval_negative(restore_interval):
    """A negative interval starts no task (returns None). Validates: Requirements 5.3"""
    config.METRICS_LOG_INTERVAL_SECS = -1.5
    task = health.start_metrics_logger()
    try:
        assert task is None
    finally:
        await _cancel(task)


# --- Requirement 5.2: enabled emits one summary line per interval ---

async def test_enabled_emits_metrics_log_line(restore_interval):
    """Enabled logger emits at least one ``[metrics]`` line. Validates: Requirements 5.2"""
    config.METRICS_LOG_INTERVAL_SECS = 0.02
    sink: list[str] = []
    sink_id = logger.add(lambda m: sink.append(str(m)), level="INFO")
    task = None
    try:
        task = health.start_metrics_logger()
        assert task is not None
        # ~2 intervals worth of time so at least one line is emitted.
        await asyncio.sleep(0.07)
        await _cancel(task)
        task = None
        metrics_lines = [line for line in sink if "[metrics]" in line]
        assert len(metrics_lines) >= 1
    finally:
        logger.remove(sink_id)
        await _cancel(task)


# --- Requirement 5.4: self-heals on error inside an iteration ---

async def test_loop_self_heals_on_error(restore_interval, monkeypatch):
    """An exception inside one iteration does not kill the loop. Validates: Requirements 5.4"""
    config.METRICS_LOG_INTERVAL_SECS = 0.02

    def boom():
        raise RuntimeError("summary blew up")

    monkeypatch.setattr(health, "_summary", boom)

    task = None
    try:
        task = health.start_metrics_logger()
        assert task is not None
        # Give the loop a few iterations; each one raises inside the try/except.
        await asyncio.sleep(0.07)
        # The loop must still be alive — the error was swallowed, not propagated.
        assert not task.done()
    finally:
        await _cancel(task)


async def test_loop_continues_after_transient_error(restore_interval, monkeypatch):
    """After one failing iteration, a later iteration still emits. Validates: Requirements 5.4"""
    config.METRICS_LOG_INTERVAL_SECS = 0.02

    calls = {"n": 0}
    real_summary = health._summary

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call fails")
        return real_summary()

    monkeypatch.setattr(health, "_summary", flaky)

    sink: list[str] = []
    sink_id = logger.add(lambda m: sink.append(str(m)), level="INFO")
    task = None
    try:
        task = health.start_metrics_logger()
        assert task is not None
        # Enough time for the first (failing) and subsequent (succeeding) iterations.
        await asyncio.sleep(0.1)
        assert not task.done()
        metrics_lines = [line for line in sink if "[metrics]" in line]
        # A subsequent successful iteration still emitted a metrics line.
        assert len(metrics_lines) >= 1
    finally:
        logger.remove(sink_id)
        await _cancel(task)
