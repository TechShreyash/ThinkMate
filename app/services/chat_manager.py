import os
import asyncio
from loguru import logger
from aiosqlite import Connection
from app.config import config
from app.database import models
from app.services.llm_service import LLMService
from app.services.memory_loader import build_memory_block
from app.services.memory_extractor import extract_and_trim
from app.services.memory_compressor import compress_user_memory

llm = LLMService()

async def handle_message(db: Connection, user_id: int, user_text: str) -> str:
    # 1. Append incoming user message to buffer
    await models.add_message_to_buffer(db, user_id, "user", user_text)
    
    # 2. Check for buffer overflow
    buffer_chars = await models.get_buffer_char_count(db, user_id)
    if buffer_chars >= config.CHAT_BUFFER_MAX_CHARS:
        logger.info(f"Buffer overflow triggered for user {user_id} ({buffer_chars} characters). Processing memory extraction...")
        # Run extraction and trim oldest messages
        await extract_and_trim(db, user_id)

    # 3. Read editable persona file
    persona_path = config.PERSONA_FILE
    if os.path.exists(persona_path):
        with open(persona_path, "r", encoding="utf-8") as f:
            persona = f.read()
    else:
        persona = "You are ThinkMate, a warm AI companion."

    # 4. Build memory context block using the active connection
    memory_block, needs_compression = await build_memory_block(db, user_id)

    # 5. Assemble complete system prompt
    from app.prompts.system_prompt import build_system_prompt
    system_prompt = build_system_prompt(persona, memory_block)

    # 6. Fetch active (remaining) history
    active_history = await models.get_chat_buffer(db, user_id)

    # 7. Query chatbot response
    reply_text = await llm.generate_response(system_prompt, active_history)

    # 8. Append bot response back to buffer
    await models.add_message_to_buffer(db, user_id, "assistant", reply_text)
    
    # 9. Trigger non-blocking memory compression in the background if threshold exceeded
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        logger.info(f"Memory size exceeded limit. Launching background compression task for user {user_id}...")
        asyncio.create_task(user_task_manager.run_compressor(user_id))
        
    return reply_text
