# Sliding Window Memory Engine Details

This document covers the core memory architecture of ThinkMate, detailing the sliding window extraction pipeline, memory loader block compilers, and memory compressors. All components are updated to use Pydantic models and MongoDB.

---

## 🛠️ Chat Manager Orchestration (`chat_manager.py`)

The orchestration process in [chat_manager.py](../../app/services/chat_manager.py) coordinates message updates, triggers memory extraction, compiles prompts, and runs chat generation:

```python
# app/services/chat_manager.py
import os
import asyncio
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.llm_service import LLMService
from app.services.memory_loader import build_memory_block

llm = LLMService()

async def handle_message(db: AsyncIOMotorDatabase, user_id: int, user_text: str) -> str:
    # 1. Append incoming user message to buffer
    await models.add_message_to_buffer(db, user_id, "user", user_text)
    
    # 2. Check for buffer overflow
    buffer_chars = await models.get_buffer_char_count(db, user_id)
    if buffer_chars >= config.CHAT_BUFFER_MAX_CHARS:
        from app.services.user_task_manager import user_task_manager
        logger.info(f"Buffer overflow triggered for user {user_id} ({buffer_chars} characters). Launching background extraction...")
        asyncio.create_task(user_task_manager.run_extractor(user_id))

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

    # 7. Query chatbot response (passing user_id for logging)
    reply_text = await llm.generate_response(user_id, system_prompt, active_history)

    # 8. Append bot response back to buffer
    await models.add_message_to_buffer(db, user_id, "assistant", reply_text)
    
    # 9. Trigger non-blocking memory compression in the background if threshold exceeded
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        logger.info(f"Memory size exceeded limit. Launching background compression task for user {user_id}...")
        asyncio.create_task(user_task_manager.run_compressor(user_id))
        
    return reply_text
```

---

## 🔍 Memory Extraction Logic (`memory_extractor.py`)

The memory extraction pipeline in [memory_extractor.py](../../app/services/memory_extractor.py) extracts key details from conversation histories and saves them to the database.

```python
# app/services/memory_extractor.py
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import LLMService
from app.services.memory_loader import build_memory_block
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT

llm = LLMService()

async def extract_and_trim(user_id: int):
    logger.info(f"Memory extraction triggered in background for user {user_id}...")
    try:
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
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

            # 2. Get current memory profile
            current_memory_text, _ = await build_memory_block(db, user_id)

            # 3. Compile extraction instructions
            instruction_prompt = (
                f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                f"=== CURRENT MEMORIES ===\n"
                f"{current_memory_text}\n"
            )

            # 4. Query LLM to parse updates (passing user_id for logging)
            extraction = await llm.extract_memory(
                user_id=user_id,
                system_prompt=instruction_prompt,
                user_history_text=formatted_chat_log
            )
            
            # 5. Apply extracted facts, beliefs, and events to the database
            await models.save_extracted_memories(db, user_id, extraction)
            
            # 6. Delete the processed segment from buffer
            await models.delete_oldest_buffer_messages(db, user_id, trim_size)
            logger.info(f"Memory extraction completed. Trimmed oldest {trim_size} messages from user {user_id}'s buffer.")
            
    except Exception as e:
        logger.error(f"Failed to execute extraction pipeline for user {user_id}: {e}")
```

---

## 🧹 Memory Compression (`memory_compressor.py`)

To prevent database bloating and respect context limits, `memory_compressor.py` runs optimization routines when the total memory block exceeds `USER_MEMORY_BUDGET_CHARS` (default `4000` characters). This compression runs as a background task without blocking the user response loop:

```python
# app/services/memory_compressor.py
import asyncio
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import LLMService
from app.services.memory_loader import build_memory_block
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT

async def compress_user_memory(user_id: int):
    logger.info(f"Memory compression triggered in background for user {user_id}...")
    try:
        async with db_session() as db:
            # 1. Compile the current memories block
            memory_text, _ = await build_memory_block(db, user_id)
            
            # Calculate target character size (80% of budget)
            target_chars = int(config.USER_MEMORY_BUDGET_CHARS * 0.8)
            
            # 2. Setup system prompt
            system_prompt = (
                f"{SYSTEM_COMPRESSION_PROMPT}\n\n"
                f"TARGET CHARACTER BUDGET: {target_chars} characters.\n"
                f"Your compressed memory profile MUST fit within {target_chars} characters."
            )
            
            # 3. Call LLM compression service
            llm = LLMService()
            compression_res = await llm.compress_memory(user_id, system_prompt, memory_text)
            
            # 4. Save/replace in DB atomically (performing hard deletes)
            await models.replace_user_memory(db, user_id, compression_res)
            logger.info(f"Memory compression successfully completed in background for user {user_id}.")
    except Exception as e:
        logger.error(f"Failed to compress memory in background for user {user_id}: {e}")
```

---

## 🔒 Shared Task Concurrency Lock
Because extraction and compression are executed asynchronously, concurrency issues can arise where the extractor and compressor write or modify user memory simultaneously. 

To prevent data corruption, a unified `memory_lock = asyncio.Lock()` is initialized inside `UserState` inside the `UserTaskManager`. The manager acquires this lock before initiating both the `run_extractor` and `run_compressor` background tasks, guaranteeing sequential executions per user.
