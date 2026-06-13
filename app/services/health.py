"""Liveness & readiness signals for Phase 10 observability.

This module exposes two cheap operational probes plus a shared compact summary
formatter over the in-memory :data:`app.services.metrics.metrics` snapshot:

* :func:`liveness` — process is up + uptime + a compact metrics summary, with
  **no I/O** (Requirement 3.2). Degrades to ``{"status": "degraded"}`` on any
  unexpected internal error (Requirement 3.6).
* :func:`readiness` — runs a single MongoDB ``ping`` and reports reachability,
  catching everything (including server-selection timeouts) so it **never
  raises** (Requirements 3.3, 3.4, 3.6).

Uptime is measured from :data:`_PROCESS_START`, captured once at import
(Requirement 3.5).

The optional periodic metrics logger (:func:`start_metrics_logger`) logs one
compact summary line per ``config.METRICS_LOG_INTERVAL_SECS`` interval when
enabled, and is a harmless no-op when disabled (Requirements 5.1–5.4).
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from app.config import config
from app.services.metrics import metrics

# Captured once at import so uptime is measured from process start (Req 3.5).
_PROCESS_START = time.time()


def _summary() -> dict:
    """Build a compact, defensive summary from ``metrics.snapshot()``.

    Reads the in-memory snapshot only (no I/O) and uses ``.get`` with defaults
    throughout so a missing metric or malformed structure never raises. Reused by
    the ``/health`` command and (later) the periodic logger.
    """
    try:
        snap = metrics.snapshot()
        counters = snap.get("counters", {}) or {}
        gauges = snap.get("gauges", {}) or {}
        timers = snap.get("timers", {}) or {}

        # Total LLM calls = sum of every ``llm.<type>.calls`` counter.
        llm_calls_total = sum(
            value
            for name, value in counters.items()
            if name.startswith("llm.") and name.endswith(".calls")
        )

        reply_latency = timers.get("llm.reply.latency", {}) or {}

        return {
            "llm_calls_total": llm_calls_total,
            "reply_latency_avg": reply_latency.get("avg", 0),
            "reply_latency_max": reply_latency.get("max", 0),
            "throttle_drops": counters.get("throttle.drops", 0),
            "queue_drops": counters.get("queue.drops", 0),
            "conversations_active": gauges.get("conversations.active", 0),
            "extraction_runs": counters.get("extraction.runs", 0),
            "compression_runs": counters.get("compression.runs", 0),
        }
    except Exception as exc:  # summary must never raise (Req 3.6)
        logger.debug(f"health._summary() failed: {exc}")
        return {}


def liveness() -> dict:
    """Report that the process is up, its uptime, and a compact metrics summary.

    Performs no I/O (Requirement 3.2). On any unexpected internal error, degrades
    to ``{"status": "degraded"}`` rather than propagating (Requirement 3.6).
    """
    try:
        return {
            "status": "ok",
            "uptime_secs": round(time.time() - _PROCESS_START, 1),
            "summary": _summary(),
        }
    except Exception as exc:  # degrade gracefully, never raise (Req 3.6)
        logger.debug(f"health.liveness() failed: {exc}")
        return {"status": "degraded"}


async def readiness(db) -> dict:
    """Check MongoDB reachability with a single ``ping``; never raises.

    Returns ``{"ready": True, "mongo": "ok"}`` when the ping succeeds, or
    ``{"ready": False, "mongo": "error", "reason": "<str>"}`` on any failure —
    including server-selection timeouts (Requirements 3.3, 3.4, 3.6).

    The ping is delegated to ``app.database.connection.ping_db`` (imported lazily
    to avoid import cycles), mirroring the existing connectivity probe so the test
    suite can patch a single path.
    """
    try:
        from app.database.connection import ping_db

        await ping_db()
        return {"ready": True, "mongo": "ok"}
    except Exception as exc:  # catch everything, including timeouts (Req 3.4, 3.6)
        logger.debug(f"health.readiness() ping failed: {exc}")
        return {"ready": False, "mongo": "error", "reason": str(exc)}


# --- Optional periodic metrics logger (Requirement 5) ---


async def _metrics_logger_loop(interval: float) -> None:
    """Log one compact metrics summary per ``interval`` until cancelled.

    Each iteration is wrapped so a transient error (e.g. a logging failure) is
    swallowed and the loop continues — it never crashes the process
    (Requirement 5.4). Cancellation exits the loop cleanly.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            logger.info(f"[metrics] {_summary()}")
        except asyncio.CancelledError:
            break
        except Exception as e:  # never crash the loop (Req 5.4)
            logger.debug(f"metrics logger iteration failed: {e}")


def start_metrics_logger() -> "asyncio.Task | None":
    """Start the periodic metrics logger when enabled; no-op (returns None) when disabled.

    Enabled only when config.METRICS_LOG_INTERVAL_SECS > 0 (Req 5.3). Logs one snapshot
    summary line per interval (Req 5.1, 5.2); the loop self-heals on errors (Req 5.4).
    """
    interval = config.METRICS_LOG_INTERVAL_SECS
    if interval <= 0:
        return None
    try:
        return asyncio.get_running_loop().create_task(_metrics_logger_loop(interval))
    except RuntimeError:
        # No running loop (e.g. called outside async context) — caller should start it under the loop.
        return None
