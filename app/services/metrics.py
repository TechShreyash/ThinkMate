"""Process-wide, in-memory metrics registry (Phase 10 observability).

A dependency-free (stdlib + loguru only) registry of counters, gauges, and
timer/histogram-lite aggregates, exposed as a single module-level singleton
``metrics``. It exists so an operator can answer "are we near the LLM ceiling?"
without adding infrastructure (no Prometheus/OTel server).

Design constraints (see ``.kiro/specs/observability``):

* **Never fail the caller.** Every mutator wraps its body in ``try/except`` and
  logs at debug on failure — a metrics error must never break a reply, a drop
  decision, or a background job (Requirement 2.8).
* **Cheap & atomic.** Mutations take a brief :class:`threading.Lock` so a record
  is applied atomically; the lock is uncontended on a single event loop
  (Requirement 1.8).
* **Bounded.** Callers only ever use a small fixed set of metric names, so
  registry memory cannot grow without limit (Requirement 1.9).
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

from loguru import logger

# Maps each known LLM ``call_type`` value onto its metric name prefix.
# Any other call_type is used as-is (Requirement 2.1).
_LLM_TYPE_PREFIX: dict[str, str] = {
    "chat_reply": "reply",
    "memory_extraction": "extraction",
    "group_memory_extraction": "group_extraction",
    "memory_compression": "compression",
    # These two keep their full call_type as the prefix (the prior fall-through
    # behavior) so existing counter names like ``llm.proactive_checkin.calls`` and
    # ``llm.memory_consolidation.calls`` stay stable for the /health summary and tests.
    "memory_consolidation": "memory_consolidation",
    "proactive_checkin": "proactive_checkin",
}

# Single ordered source of truth for the known LLM task types and their metric
# prefixes (Requirements 6.1, 6.2, 6.3, 6.7). Derived directly from
# ``_LLM_TYPE_PREFIX`` so the two can never drift out of sync.
LLM_TASK_TYPES: tuple[tuple[str, str], ...] = tuple(_LLM_TYPE_PREFIX.items())


class MetricsRegistry:
    """In-memory counters, gauges, and timer aggregates, safe for concurrent use."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        # name -> {"count": int, "sum": float, "max": float}
        self._timers: dict[str, dict] = {}
        self._lock = threading.Lock()

    def incr(self, name: str, n: int = 1) -> None:
        """Add ``n`` (default 1) to the named counter, creating it at 0 on first use."""
        try:
            with self._lock:
                self._counters[name] = self._counters.get(name, 0) + n
        except Exception as exc:  # never raise into the caller (Req 2.8)
            logger.debug(f"metrics.incr({name!r}) failed: {exc}")

    def set_gauge(self, name: str, value: float) -> None:
        """Replace the named gauge's value with ``value``."""
        try:
            with self._lock:
                self._gauges[name] = value
        except Exception as exc:
            logger.debug(f"metrics.set_gauge({name!r}) failed: {exc}")

    def observe(self, name: str, value: float) -> None:
        """Record ``value`` into the named timer aggregate (count+1, sum+=value, max)."""
        try:
            with self._lock:
                agg = self._timers.get(name)
                if agg is None:
                    self._timers[name] = {"count": 1, "sum": value, "max": value}
                else:
                    agg["count"] += 1
                    agg["sum"] += value
                    if value > agg["max"]:
                        agg["max"] = value
        except Exception as exc:
            logger.debug(f"metrics.observe({name!r}) failed: {exc}")

    def record_latency(self, name: str, seconds: float) -> None:
        """Alias of :meth:`observe` for latency values."""
        self.observe(name, seconds)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        """Context manager recording the block's wall-clock duration exactly once.

        The duration is observed in a ``finally`` so it is recorded even when the
        wrapped block raises; the original exception propagates unchanged
        (Requirement 1.5).
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            try:
                self.observe(name, time.perf_counter() - start)
            except Exception as exc:  # the record step must never raise (Req 2.8)
                logger.debug(f"metrics.timer({name!r}) record failed: {exc}")

    def record_llm(self, call_type: str, *, ok: bool, latency: float) -> None:
        """Record an LLM call: ``llm.<type>.calls`` + success/failure + latency.

        ``call_type`` is mapped via :data:`_LLM_TYPE_PREFIX`; unknown types use the
        ``call_type`` string as-is for the metric prefix.
        """
        try:
            prefix = _LLM_TYPE_PREFIX.get(call_type, call_type)
            self.incr(f"llm.{prefix}.calls")
            self.incr(f"llm.{prefix}.success" if ok else f"llm.{prefix}.failure")
            self.observe(f"llm.{prefix}.latency", latency)
        except Exception as exc:
            logger.debug(f"metrics.record_llm({call_type!r}) failed: {exc}")

    def snapshot(self) -> dict:
        """Return a plain dict of all counters, gauges, and timers (with derived avg).

        Always returns a well-formed ``{"counters", "gauges", "timers"}`` structure,
        even for an empty registry or on an internal error (Requirements 1.6, 1.10).
        """
        try:
            with self._lock:
                counters = dict(self._counters)
                gauges = dict(self._gauges)
                timers = {}
                for name, agg in self._timers.items():
                    count = agg["count"]
                    total = agg["sum"]
                    timers[name] = {
                        "count": count,
                        "sum": total,
                        "max": agg["max"],
                        "avg": (total / count) if count else 0,
                    }
            return {"counters": counters, "gauges": gauges, "timers": timers}
        except Exception as exc:  # stay well-formed even on internal error
            logger.debug(f"metrics.snapshot() failed: {exc}")
            return {"counters": {}, "gauges": {}, "timers": {}}

    def reset(self) -> None:
        """Clear all counters, gauges, and timers (used for test isolation)."""
        try:
            with self._lock:
                self._counters.clear()
                self._gauges.clear()
                self._timers.clear()
        except Exception as exc:
            logger.debug(f"metrics.reset() failed: {exc}")


# Process-wide singleton.
metrics = MetricsRegistry()
