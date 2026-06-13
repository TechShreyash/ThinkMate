"""Background memory compression.

Triggered when a user's compiled memory profile exceeds ``USER_MEMORY_BUDGET_CHARS``.
The LLM is asked to condense the profile to ~80% of budget; because models cannot count
characters reliably, a deterministic post-pass then enforces the budget by dropping the
lowest-priority items (oldest events, then beliefs, then facts) until it fits. This,
together with the per-user cooldown in ``UserTaskManager``, prevents a re-trigger loop.
"""
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT

# Hard ceiling on deterministic-trim iterations (safety against pathological loops).
_MAX_TRIM_ITERS = 200


async def _enforce_budget(db, user_id: int):
    """Deterministically drop the lowest-priority memory items until under budget."""
    for _ in range(_MAX_TRIM_ITERS):
        _, over_budget = await build_memory_block(db, user_id)
        if not over_budget:
            return
        doc = await db["user_profiles"].find_one({"_id": user_id}) or {}
        for field in ("events", "beliefs", "facts"):  # priority: shed events first
            items = doc.get(field) or []
            if items:
                await db["user_profiles"].update_one(
                    {"_id": user_id}, {"$set": {field: items[1:]}}
                )
                break
        else:
            return  # nothing left to drop
    logger.warning(f"Budget enforcement hit iteration cap for user {user_id}.")


async def compress_user_memory(user_id: int):
    """Compress the user's memory profile and enforce the character budget."""
    logger.info(f"Memory compression started for user {user_id}.")
    try:
        async with db_session() as db:
            memory_text, _ = await build_memory_block(db, user_id)
            target_chars = int(config.USER_MEMORY_BUDGET_CHARS * 0.8)

            system_prompt = (
                f"{SYSTEM_COMPRESSION_PROMPT}\n\n"
                f"TARGET CHARACTER BUDGET: {target_chars} characters.\n"
                f"Your compressed memory profile MUST fit within {target_chars} characters."
            )

            compression = await llm_service.compress_memory(user_id, system_prompt, memory_text)
            await models.replace_user_memory(db, user_id, compression)

            # Guarantee we end up under budget even if the model overshot.
            await _enforce_budget(db, user_id)
            logger.info(f"Memory compression done for user {user_id}.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Compression failed for user {user_id}: {e}")
