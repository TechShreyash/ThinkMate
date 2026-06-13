# Sliding Window Memory Engine Details

This document covers the core memory architecture of ThinkMate, detailing the sliding window extraction pipeline, memory loader block compilers, and memory compressors. All components are updated to use Pydantic models and MongoDB.

---

## 🛠️ Chat Manager Orchestration (`chat_manager.py`)

The orchestration process in [chat_manager.py](../../app/services/chat_manager.py) coordinates message updates, triggers memory extraction, compiles prompts, and runs chat generation:

```python
# app/services/chat_manager.py
import asyncio
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.llm_service import llm_service          # shared singleton
from app.services.memory_loader import build_memory_block
from app.prompts.system_prompt import build_system_prompt

async def handle_message(
    db: AsyncIOMotorDatabase, user_id: int, user_text: str
) -> tuple[str, str | None]:
    # 1. Append the user message; the returned array gives char count + active
    #    history in a single round-trip (no separate buffer reads).
    messages = await models.add_message_to_buffer(db, user_id, "user", user_text)
    buffer_chars = sum(len(m["content"]) for m in messages)
    active_history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # 2. Buffer overflow -> non-blocking background extraction.
    if buffer_chars >= config.CHAT_BUFFER_MAX_CHARS:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_extractor(user_id))

    # 3. Assemble the system prompt (persona is cached by mtime; see _load_persona).
    memory_block, needs_compression = await build_memory_block(db, user_id)
    system_prompt = build_system_prompt(_load_persona(), memory_block)

    # 4. ONE LLM call -> reply + optional reaction.
    reply_text, reaction = await llm_service.generate_reply_bundle(
        user_id, system_prompt, active_history
    )

    # 5. Persist the assistant reply.
    await models.add_message_to_buffer(db, user_id, "assistant", reply_text)

    # 6. Memory over budget -> rate-limited background compression.
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_compressor(user_id))

    return reply_text, reaction
```

> The persona file is read through `_load_persona`, which re-reads only when the file's mtime
> changes — preserving "edit persona without restart" while avoiding a blocking disk read on
> every message. The reply and reaction are produced in a single call, so the batch processor
> simply applies the returned reaction and sends the reply.

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
