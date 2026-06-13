"""Background memory extraction.

Reads the oldest buffer messages (everything except the latest ``CHAT_BUFFER_TRIM``),
asks the extraction model for structured updates, applies them to the user profile, and
atomically trims the processed messages from the buffer.

The extraction call is retried up to ``MAX_EXTRACTION_ATTEMPTS`` times. Each attempt
re-reads the buffer, so any messages that arrive *while* a slow call is in flight are
folded into the next attempt instead of being missed. If every attempt fails (e.g. an LLM
outage), the processed segment is trimmed anyway so the buffer can't grow without bound —
a deliberate trade of a bounded amount of un-extracted memory for a healthy buffer.
"""
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT

# Maximum number of extraction LLM calls per run; each call re-snapshots the latest buffer.
MAX_EXTRACTION_ATTEMPTS = 3


def _format_segment(segment: list[dict]) -> str:
    """Render a buffer slice as a readable ``User:``/``Assistant:`` transcript."""
    return "".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}\n"
        for m in segment
    )


async def extract_and_trim(user_id: int):
    """Extract lasting memories from older buffer messages, then trim them.

    Retries the extraction call up to ``MAX_EXTRACTION_ATTEMPTS`` times, re-reading the
    buffer on each attempt so messages that arrived mid-call are included. On success the
    processed segment is saved and atomically trimmed. If all attempts fail, the oldest
    messages are trimmed anyway to keep the buffer bounded during an outage.
    """
    logger.info(f"Memory extraction started for user {user_id}.")
    keep_count = config.CHAT_BUFFER_TRIM
    try:
        for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
            async with db_session() as db:
                buffer_messages = await models.get_chat_buffer(db, user_id)
                if len(buffer_messages) <= keep_count:
                    # Nothing left to process (e.g. a concurrent run already trimmed it).
                    return

                trim_size = len(buffer_messages) - keep_count
                segment = buffer_messages[:trim_size]

                # Give the model current memories so it can de-dupe / merge / correct.
                current_memory_text, _ = await build_memory_block(db, user_id)
                instruction_prompt = (
                    f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                    f"=== CURRENT MEMORIES ===\n{current_memory_text}\n"
                )

                extraction = await llm_service.extract_memory(
                    user_id=user_id,
                    system_prompt=instruction_prompt,
                    user_history_text=_format_segment(segment),
                )

                if extraction is not None:
                    await models.save_extracted_memories(db, user_id, extraction)
                    # Atomic trim of the processed segment (concurrent appends preserved):
                    # the segment we processed is the oldest `trim_size` messages, so a
                    # fresh-read oldest-N trim removes exactly those, keeping newer arrivals.
                    await models.delete_oldest_buffer_messages(db, user_id, trim_size)
                    logger.info(
                        f"Memory extraction done for user {user_id}; trimmed {trim_size} messages."
                    )
                    return

            logger.warning(
                f"Extraction attempt {attempt}/{MAX_EXTRACTION_ATTEMPTS} failed for user {user_id}."
            )

        # Every attempt failed. Trim the oldest messages anyway so a sustained outage can't
        # let the buffer grow unbounded. This drops un-extracted memory by design.
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
            if len(buffer_messages) > keep_count:
                trim_size = len(buffer_messages) - keep_count
                await models.delete_oldest_buffer_messages(db, user_id, trim_size)
                logger.error(
                    f"All {MAX_EXTRACTION_ATTEMPTS} extraction attempts failed for user {user_id}; "
                    f"trimmed {trim_size} un-extracted messages to keep the buffer bounded."
                )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Extraction pipeline failed for user {user_id}: {e}")
