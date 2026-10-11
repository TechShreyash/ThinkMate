"""Background memory compression.

Triggered when a user's compiled memory profile exceeds ``USER_MEMORY_BUDGET_CHARS``.
The LLM is asked to condense the profile to ~80% of budget; because models cannot count
characters reliably, a deterministic post-pass then enforces the budget by dropping the
lowest-priority items (oldest events, then beliefs, then facts) until it fits. This,
together with the per-user cooldown in ``UserTaskManager``, prevents a re-trigger loop.

Two correctness/efficiency invariants (see hardening_plan.md H2/H3):

* **Never wipe memory on failure.** ``compress_memory`` returns ``None`` when the LLM call
  fails; in that case the replace step is skipped and existing memory is preserved.
* **Single-pass budget enforcement.** ``_enforce_budget`` does one read, drops the
  lowest-priority items in memory, then issues a single write — never a per-item
  read/write loop.
"""

from loguru import logger

from app.config import config
from app.database import models
from app.database.connection import db_session
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT
from app.services.llm_service import llm_service
from app.services.memory_loader import (
    compile_memory_block,
    compile_memory_text,
    load_profile_doc,
)
from app.services.metrics import metrics


async def _enforce_budget(db, user_id: int, *, source_profile: dict | None = None):
    """Deterministically drop the lowest-priority memory items until under budget.

    Single read + in-memory trim + single write: load the profile once, then drop the
    lowest-priority items (oldest events first, then beliefs, then facts) in memory,
    recomputing the compiled block length locally after each drop. Persist the trimmed
    arrays in one ``update_one`` only if anything was actually removed.
    """
    doc = await db["user_profiles"].find_one({"_id": user_id})
    if not doc:
        return
    if source_profile is not None and any(
        (field in doc) != (field in source_profile)
        or doc.get(field) != source_profile.get(field)
        for field in ("created_at", "memory_generation")
    ):
        return

    facts = list(doc.get("facts") or [])
    beliefs = list(doc.get("beliefs") or [])
    events = list(doc.get("events") or [])
    original_lengths = (len(facts), len(beliefs), len(events))
    budget = config.USER_MEMORY_BUDGET_CHARS

    working = dict(doc)

    def over_budget() -> bool:
        working["facts"] = facts
        working["beliefs"] = beliefs
        working["events"] = events
        return len(compile_memory_text(working)) > budget

    # Priority: shed oldest events first, then beliefs, then facts (item[0] is oldest).
    while over_budget():
        if events:
            events.pop(0)
        elif beliefs:
            beliefs.pop(0)
        elif facts:
            facts.pop(0)
        else:
            break  # nothing left to drop

    if (len(facts), len(beliefs), len(events)) != original_lengths:
        applied = await models.update_memory_if_unchanged(
            db, doc, {"facts": facts, "beliefs": beliefs, "events": events}
        )
        if not applied:
            logger.info(
                f"Budget enforcement skipped for user {user_id}; memory changed."
            )


async def compress_user_memory(user_id: int):
    """Compress the user's memory profile and enforce the character budget."""
    logger.info(f"Memory compression started for user {user_id}.")
    metrics.incr("compression.runs")
    try:
        async with db_session() as db:
            snapshot = await load_profile_doc(db, user_id)
            if not snapshot:
                return
            memory_text, _ = compile_memory_block(snapshot)
            target_chars = int(config.USER_MEMORY_BUDGET_CHARS * 0.8)

            system_prompt = (
                f"{SYSTEM_COMPRESSION_PROMPT}\n\n"
                f"TARGET CHARACTER BUDGET: {target_chars} characters.\n"
                f"Your compressed memory profile MUST fit within {target_chars} characters."
            )

            compression = await llm_service.compress_memory(
                user_id, system_prompt, memory_text
            )
            if compression is None:
                # Failed call: keep existing memory untouched rather than wiping it.
                logger.warning(
                    f"Compression failed for user {user_id}; keeping existing memory."
                )
                return

            applied = await models.replace_user_memory(
                db, user_id, compression, snapshot=snapshot
            )
            if not applied:
                logger.info(f"Compression skipped for user {user_id}; memory changed.")
                return

            # Guarantee we end up under budget even if the model overshot.
            await _enforce_budget(db, user_id, source_profile=snapshot)
            logger.info(f"Memory compression done for user {user_id}.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Compression failed for user {user_id}: {e}")
