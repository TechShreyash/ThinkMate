"""Tests for the in-memory metrics registry (Phase 10 observability, Task 1.2).

Covers the counter/gauge/timer/snapshot/reset contract of
``app/services/metrics.py`` — both a fresh ``MetricsRegistry()`` (for full
isolation) and the process-wide ``metrics`` singleton.

Conventions follow ``tests/conftest.py`` (pytest; ``asyncio_mode = "auto"``).
The registry itself is synchronous, so these are plain (non-async) tests.

Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.10, 7.1, 7.2
"""
from __future__ import annotations

import pytest

from app.services.metrics import MetricsRegistry, metrics


@pytest.fixture(autouse=True)
def reset_singleton_metrics():
    """Isolate the module singleton's state before and after each test."""
    metrics.reset()
    yield
    metrics.reset()


# --- incr (Requirement 1.2) ------------------------------------------------

def test_incr_default_and_explicit_n_accumulate():
    reg = MetricsRegistry()
    reg.incr("c")          # default n=1
    reg.incr("c", 3)       # explicit n
    assert reg.snapshot()["counters"]["c"] == 4


def test_incr_auto_creates_counter_at_zero_on_first_use():
    reg = MetricsRegistry()
    reg.incr("brand_new")
    # First-ever use creates it starting from 0, then adds the default 1.
    assert reg.snapshot()["counters"]["brand_new"] == 1


def test_incr_on_module_singleton():
    metrics.incr("singleton.c")
    metrics.incr("singleton.c", 5)
    assert metrics.snapshot()["counters"]["singleton.c"] == 6


# --- set_gauge (Requirement 1.3) -------------------------------------------

def test_set_gauge_replaces_rather_than_accumulates():
    reg = MetricsRegistry()
    reg.set_gauge("g", 5)
    reg.set_gauge("g", 9)
    assert reg.snapshot()["gauges"]["g"] == 9


# --- observe / record_latency (Requirements 1.4, 1.6) ----------------------

def test_observe_builds_count_sum_max_and_snapshot_derives_avg():
    reg = MetricsRegistry()
    reg.observe("t", 1.0)
    reg.observe("t", 3.0)
    agg = reg.snapshot()["timers"]["t"]
    assert agg["count"] == 2
    assert agg["sum"] == 4.0
    assert agg["max"] == 3.0
    assert agg["avg"] == 2.0  # sum / count


def test_record_latency_is_an_alias_of_observe():
    reg = MetricsRegistry()
    reg.record_latency("lat", 2.0)
    reg.record_latency("lat", 6.0)
    agg = reg.snapshot()["timers"]["lat"]
    assert agg["count"] == 2
    assert agg["sum"] == 8.0
    assert agg["max"] == 6.0
    assert agg["avg"] == 4.0


# --- timer context manager (Requirement 1.5) -------------------------------

def test_timer_records_exactly_one_observation_on_normal_exit():
    reg = MetricsRegistry()
    with reg.timer("block"):
        pass
    agg = reg.snapshot()["timers"]["block"]
    assert agg["count"] == 1
    assert agg["max"] >= 0
    assert agg["sum"] >= 0


def test_timer_records_once_and_propagates_on_exception():
    reg = MetricsRegistry()

    with pytest.raises(ValueError, match="boom"):
        with reg.timer("block"):
            raise ValueError("boom")

    # Recorded exactly once even though the block raised.
    agg = reg.snapshot()["timers"]["block"]
    assert agg["count"] == 1
    assert agg["max"] >= 0


# --- record_llm convenience (Requirements 1.2, 1.4) ------------------------

def test_record_llm_success_increments_calls_success_and_latency():
    reg = MetricsRegistry()
    reg.record_llm("chat_reply", ok=True, latency=0.2)

    snap = reg.snapshot()
    assert snap["counters"]["llm.reply.calls"] == 1
    assert snap["counters"]["llm.reply.success"] == 1
    assert "llm.reply.failure" not in snap["counters"]
    assert snap["timers"]["llm.reply.latency"]["count"] == 1


def test_record_llm_failure_increments_calls_and_failure():
    reg = MetricsRegistry()
    reg.record_llm("memory_extraction", ok=False, latency=0.1)

    snap = reg.snapshot()
    assert snap["counters"]["llm.extraction.calls"] == 1
    assert snap["counters"]["llm.extraction.failure"] == 1
    assert "llm.extraction.success" not in snap["counters"]
    assert snap["timers"]["llm.extraction.latency"]["count"] == 1


# --- snapshot shape (Requirements 1.6, 1.10) -------------------------------

def test_snapshot_has_three_sections():
    reg = MetricsRegistry()
    reg.incr("c")
    reg.set_gauge("g", 1)
    reg.observe("t", 1.0)
    snap = reg.snapshot()
    assert set(snap.keys()) == {"counters", "gauges", "timers"}


def test_empty_registry_snapshot_returns_well_formed_empty_sections():
    reg = MetricsRegistry()
    snap = reg.snapshot()
    assert snap == {"counters": {}, "gauges": {}, "timers": {}}


# --- reset (Requirement 1.7) -----------------------------------------------

def test_reset_clears_everything():
    reg = MetricsRegistry()
    reg.incr("c", 2)
    reg.set_gauge("g", 7)
    reg.observe("t", 1.5)

    reg.reset()

    assert reg.snapshot() == {"counters": {}, "gauges": {}, "timers": {}}


def test_reset_isolates_module_singleton():
    metrics.incr("c")
    metrics.set_gauge("g", 3)
    metrics.observe("t", 1.0)

    metrics.reset()

    assert metrics.snapshot() == {"counters": {}, "gauges": {}, "timers": {}}
