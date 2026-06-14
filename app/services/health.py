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


# --- Metrics persistence (survive restarts/crashes) ---


async def load_persisted_metrics() -> None:
    """Restore the metrics registry from MongoDB at startup. Best-effort, never raises.

    Seeds the in-memory registry from the last saved snapshot so counters and timer
    aggregates keep accumulating across restarts instead of resetting to zero. A missing
    or unreadable document simply leaves the registry empty.
    """
    try:
        from app.database.connection import db_session
        from app.database import models

        async with db_session() as db:
            state = await models.load_metrics_state(db)
        if state:
            metrics.load_state(state)
            logger.info("Restored persisted metrics from MongoDB.")
    except Exception as exc:  # never block startup on metrics restore
        logger.debug(f"load_persisted_metrics failed: {exc}")


async def flush_metrics() -> None:
    """Write the current metrics snapshot to MongoDB. Best-effort, never raises."""
    try:
        from app.database.connection import db_session
        from app.database import models

        async with db_session() as db:
            await models.save_metrics_state(db, metrics.snapshot())
    except Exception as exc:  # a flush failure must never crash a loop or shutdown
        logger.debug(f"flush_metrics failed: {exc}")


async def _metrics_persist_loop(interval: float) -> None:
    """Flush the metrics snapshot to MongoDB every ``interval`` seconds until cancelled.

    On cancellation (shutdown) it performs one final flush so the latest counters are not
    lost between the last periodic write and process exit. Self-heals on transient errors.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            await flush_metrics()
        except asyncio.CancelledError:
            await flush_metrics()  # final flush on graceful shutdown
            break
        except Exception as e:  # never crash the loop
            logger.debug(f"metrics persist iteration failed: {e}")


def start_metrics_persister() -> "asyncio.Task | None":
    """Start the periodic metrics persister when enabled; no-op (None) when disabled.

    Enabled when config.METRICS_PERSIST_INTERVAL_SECS > 0. When disabled, callers should
    still invoke :func:`flush_metrics` once on shutdown to capture the session's totals.
    """
    interval = config.METRICS_PERSIST_INTERVAL_SECS
    if interval <= 0:
        return None
    try:
        return asyncio.get_running_loop().create_task(_metrics_persist_loop(interval))
    except RuntimeError:
        return None


# --- Periodic consolidation scheduler (Phase 11, Requirement 1) ---


async def _run_consolidation_scan() -> None:
    """One scan: find due users and dispatch each through the per-user memory_lock."""
    # Lazy imports avoid an import cycle (user_task_manager -> chat_manager -> ...).
    from app.database.connection import db_session
    from app.database import models
    from app.services.user_task_manager import user_task_manager

    async with db_session() as db:
        due = await models.find_users_due_for_consolidation(
            db,
            interval_secs=config.CONSOLIDATION_INTERVAL_SECS,
            min_items=config.CONSOLIDATION_MIN_ITEMS,
            limit=config.CONSOLIDATION_MAX_USERS_PER_SCAN,
        )
    processed = 0
    for user_id in due:
        try:
            await user_task_manager.run_consolidator(user_id)
            processed += 1
        except Exception as e:  # one user's failure must not abort the scan (Req 1.6)
            logger.warning(f"Consolidation failed for user {user_id}: {e}")
    logger.info(f"[consolidation] scan: {len(due)} due, {processed} processed.")


async def _consolidation_loop(scan_interval: float) -> None:
    while True:
        try:
            await asyncio.sleep(scan_interval)
            await _run_consolidation_scan()
        except asyncio.CancelledError:
            break
        except Exception as e:  # never crash the loop (Req 1.7)
            logger.debug(f"consolidation scan iteration failed: {e}")


def start_consolidation_scheduler() -> "asyncio.Task | None":
    """Start the periodic consolidation scheduler when enabled; no-op (None) when disabled.

    Enabled only when config.CONSOLIDATION_INTERVAL_SECS > 0 (Req 1.2). Mirrors
    start_metrics_logger. The loop self-heals (Req 1.7).
    """
    if config.CONSOLIDATION_INTERVAL_SECS <= 0:
        return None
    try:
        return asyncio.get_running_loop().create_task(
            _consolidation_loop(config.CONSOLIDATION_SCAN_INTERVAL_SECS)
        )
    except RuntimeError:
        return None


# --- Proactive check-in scheduler (Phase 12, Requirements 5, 6.6/6.7, 7.5-7.8, 11, 12.3/12.6) ---


def _in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """True if `hour` (0-23, UTC) is within [start, end). start==end => no quiet window.
    Handles same-day (start<end) and midnight-wrapping (start>end)."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


async def _send_proactive_checkin(bot, user_id: int, *, now) -> str:
    """Generate + send one check-in. Returns 'sent' | 'skipped' | 'failed'.

    Always sets last_proactive_at (holds the rate-limit window even on skip/fail).
    Never raises.
    """
    from app.database.connection import db_session
    from app.database import models
    from app.services.memory_loader import build_memory_block
    from app.services.llm_service import llm_service
    from app.services.chat_manager import _load_persona
    from app.prompts.system_prompt import build_system_prompt

    async with db_session() as db:
        memory_text, _ = await build_memory_block(db, user_id)
        # Always hold the window for this attempt.
        await models.set_last_proactive(db, user_id, now=now)
        system_prompt = build_system_prompt(_load_persona(), memory_text)
        text = await llm_service.generate_checkin(user_id, system_prompt, memory_text)
        if not text:
            return "skipped"
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception as e:  # Forbidden / blocked / network — stop nagging this user.
            logger.warning(f"Proactive send failed for user {user_id}: {e}; disabling for them.")
            await models.set_proactive_enabled(db, user_id, False)
            return "failed"
        # Record the assistant message in the buffer so a reply flows normally.
        await models.add_message_to_buffer(db, user_id, "assistant", text, sender_id=0, sender_name=config.bot_display_name)
        # Count this delivered check-in toward the unanswered streak; if the user keeps
        # ignoring us they'll be auto-paused once it reaches PROACTIVE_MAX_UNANSWERED.
        await models.increment_proactive_unanswered(db, user_id)
        return "sent"


async def _run_proactive_scan(bot) -> None:
    from datetime import datetime, timezone
    from app.database.connection import db_session
    from app.database import models

    metrics.incr("proactive.runs")
    now = datetime.now(timezone.utc)
    if _in_quiet_hours(now.hour, config.PROACTIVE_QUIET_START_HOUR, config.PROACTIVE_QUIET_END_HOUR):
        logger.debug("[proactive] in quiet hours; skipping scan.")
        return
    async with db_session() as db:
        due = await models.find_users_due_for_proactive(
            db,
            inactivity_secs=config.PROACTIVE_INACTIVITY_SECS,
            min_interval_secs=config.PROACTIVE_MIN_INTERVAL_SECS,
            limit=config.PROACTIVE_MAX_PER_SCAN,
            max_unanswered=config.PROACTIVE_MAX_UNANSWERED,
            now=now,
        )
    sent = skipped = failed = 0
    for user_id in due:
        try:
            outcome = await _send_proactive_checkin(bot, user_id, now=now)
            if outcome == "sent": sent += 1; metrics.incr("proactive.sent")
            elif outcome == "skipped": skipped += 1; metrics.incr("proactive.skipped")
            else: failed += 1; metrics.incr("proactive.failed")
        except Exception as e:  # one user's failure must not abort the scan
            failed += 1; metrics.incr("proactive.failed")
            logger.warning(f"Proactive check-in error for user {user_id}: {e}")
    logger.info(f"[proactive] scan: {len(due)} due, {sent} sent, {skipped} skipped, {failed} failed.")


async def _proactive_loop(bot, scan_interval: float) -> None:
    while True:
        try:
            await asyncio.sleep(scan_interval)
            await _run_proactive_scan(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"proactive scan iteration failed: {e}")


def start_proactive_scheduler(bot) -> "asyncio.Task | None":
    """Start the proactive check-in scheduler when enabled; no-op (None) when disabled.

    Enabled only when config.PROACTIVE_INTERVAL_SECS > 0. Needs the aiogram bot to send.
    """
    if config.PROACTIVE_INTERVAL_SECS <= 0:
        return None
    try:
        return asyncio.get_running_loop().create_task(_proactive_loop(bot, config.PROACTIVE_INTERVAL_SECS))
    except RuntimeError:
        return None
