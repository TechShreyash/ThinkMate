"""Background memory extraction.

Reads the oldest buffer messages (everything except the latest ``CHAT_BUFFER_TRIM``),
asks the extraction model for structured updates, applies them to the user profile, and
atomically trims the processed messages from the buffer.
"""
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT


async def extract_and_trim(user_id: int):
    """Extract lasting memories from older buffer messages, then trim them."""
    logger.info(f"Memory extraction started for user {user_id}.")
    try:
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
            keep_count = config.CHAT_BUFFER_TRIM
            if len(buffer_messages) <= keep_count:
                return

            trim_size = len(buffer_messages) - keep_count
            segment = buffer_messages[:trim_size]

            formatted_chat_log = "".join(
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}\n"
                for m in segment
            )

            # Give the model current memories so it can de-dupe / merge / correct.
            current_memory_text, _ = await build_memory_block(db, user_id)
            instruction_prompt = (
                f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                f"=== CURRENT MEMORIES ===\n{current_memory_text}\n"
            )

            extraction = await llm_service.extract_memory(
                user_id=user_id,
                system_prompt=instruction_prompt,
                user_history_text=formatted_chat_log,
            )

            await models.save_extracted_memories(db, user_id, extraction)
            # Atomic trim of the processed segment (concurrent appends are preserved).
            await models.delete_oldest_buffer_messages(db, user_id, trim_size)
            logger.info(f"Memory extraction done for user {user_id}; trimmed {trim_size} messages.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Extraction pipeline failed for user {user_id}: {e}")
