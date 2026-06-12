from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import LLMService
from app.services.memory_loader import build_memory_block
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT

llm = LLMService()

async def extract_and_trim(user_id: int):
    """
    Background non-blocking task that extracts lasting memories from the
    oldest buffer messages, saves them to DB, and trims the buffer.
    """
    logger.info(f"Memory extraction triggered in background for user {user_id}...")
    try:
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
            # We keep the latest CHAT_BUFFER_TRIM messages as active context
            keep_count = config.CHAT_BUFFER_TRIM
            
            if len(buffer_messages) <= keep_count:
                return
                
            trim_size = len(buffer_messages) - keep_count
            extraction_segment = buffer_messages[:trim_size]
            
            # Format segment as a readable conversation text block
            formatted_chat_log = ""
            for msg in extraction_segment:
                role_label = "User" if msg["role"] == "user" else "Assistant"
                formatted_chat_log += f"{role_label}: {msg['content']}\n"

            # 2. Get current memory profile (gives context to prevent duplicate facts)
            current_memory_text, _ = await build_memory_block(db, user_id)

            # 3. Compile extraction instructions
            instruction_prompt = (
                f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                f"=== CURRENT MEMORIES ===\n"
                f"{current_memory_text}\n"
            )

            # 4. Query LLM to parse updates (returns validated MemoryExtraction model)
            extraction = await llm.extract_memory(
                user_id=user_id,
                system_prompt=instruction_prompt,
                user_history_text=formatted_chat_log
            )
            
            # 5. Apply extracted facts and events to the database
            await models.save_extracted_memories(db, user_id, extraction)
            
            # 6. Delete the processed segment from buffer
            await models.delete_oldest_buffer_messages(db, user_id, trim_size)
            logger.info(f"Memory extraction completed. Trimmed oldest {trim_size} messages from user {user_id}'s buffer.")
            
    except Exception as e:
        logger.error(f"Failed to execute extraction pipeline for user {user_id}: {e}")

