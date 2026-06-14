"""Background memory consolidation (the Phase 11 'dreaming' pass).

Periodically reviews a user's WHOLE profile to refresh the summary/style, merge and
de-duplicate items, and synthesize a small bounded set of durable behavioral insights.
Runs off the hot path (scheduler -> run_consolidator under memory_lock). Mirrors
``compress_user_memory``: one LLM call, single-write apply, never-wipe-on-failure,
deterministic budget enforcement, and metrics.
"""
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.services.memory_compressor import _enforce_budget
from app.services.metrics import metrics
from app.prompts.consolidation_prompt import SYSTEM_CONSOLIDATION_PROMPT


async def consolidate_user_memory(user_id: int) -> None:
    """Review the full profile, synthesize durable insights, merge/refresh, enforce budget.

    One LLM call. Never wipes memory on failure (mirrors compress_user_memory): a ``None``
    result skips the write AND does not advance ``last_consolidated_at``. Runs entirely off
    the hot path and never raises into the scheduler.
    """
    logger.info(f"Memory consolidation started for user {user_id}.")
    metrics.incr("consolidation.runs")
    try:
        async with db_session() as db:
            memory_text, _ = await build_memory_block(db, user_id)
            system_prompt = (
                f"{SYSTEM_CONSOLIDATION_PROMPT}\n\n"
                f"MAX INSIGHTS: {config.MAX_INSIGHTS}. Emit at most {config.MAX_INSIGHTS} insights."
            )
            consolidation = await llm_service.consolidate_memory(user_id, system_prompt, memory_text)
            if consolidation is None:
                logger.warning(f"Consolidation failed for user {user_id}; keeping existing memory.")
                metrics.incr("consolidation.failure")
                return
            await models.apply_consolidation(db, user_id, consolidation)
            await _enforce_budget(db, user_id)
            metrics.incr("consolidation.success")
            logger.info(f"Memory consolidation done for user {user_id}.")
    except Exception as e:  # noqa: BLE001 - never raise into the scheduler
        logger.error(f"Consolidation failed for user {user_id}: {e}")
        metrics.incr("consolidation.failure")
