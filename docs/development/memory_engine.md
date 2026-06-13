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

The memory extraction pipeline in [memory_extractor.py](../../app/services/memory_extractor.py) extracts key details from conversation histories and saves them to the database. All LLM access goes through the shared `llm_service` singleton (one client/connection pool per process).

The extraction call is **retried up to `MAX_EXTRACTION_ATTEMPTS` (3) times**, and the buffer is **re-read on every attempt** so messages that arrive while a slow call is in flight are folded into the next attempt rather than missed. Success vs. failure is distinguished by `extract_memory` returning a value vs. `None` — an *empty* `MemoryExtraction` still counts as success (nothing was worth saving). If every attempt fails (e.g. an LLM outage), the oldest messages are trimmed anyway so the buffer stays bounded; memory is never written on a failed run.

```python
# app/services/memory_extractor.py
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service          # shared singleton
from app.services.memory_loader import build_memory_block
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT

MAX_EXTRACTION_ATTEMPTS = 3  # max extraction LLM calls per run; each re-snapshots the buffer

async def extract_and_trim(user_id: int):
    logger.info(f"Memory extraction started for user {user_id}.")
    keep_count = config.CHAT_BUFFER_TRIM
    try:
        for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
            async with db_session() as db:
                buffer_messages = await models.get_chat_buffer(db, user_id)
                if len(buffer_messages) <= keep_count:
                    return  # nothing left (a concurrent run may have trimmed it)

                trim_size = len(buffer_messages) - keep_count
                segment = buffer_messages[:trim_size]            # oldest messages
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
                if extraction is not None:                       # success (may be empty)
                    await models.save_extracted_memories(db, user_id, extraction)
                    await models.delete_oldest_buffer_messages(db, user_id, trim_size)
                    return
            logger.warning(f"Extraction attempt {attempt}/{MAX_EXTRACTION_ATTEMPTS} failed.")

        # Every attempt failed -> trim anyway so an outage can't grow the buffer unbounded.
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
            if len(buffer_messages) > keep_count:
                await models.delete_oldest_buffer_messages(db, user_id, len(buffer_messages) - keep_count)
    except Exception as e:
        logger.error(f"Extraction pipeline failed for user {user_id}: {e}")
```

---

## 🧹 Memory Compression (`memory_compressor.py`)

To prevent profile bloat and respect context limits, `memory_compressor.py` runs when the
compiled memory block exceeds `USER_MEMORY_BUDGET_CHARS` (default `4000`). It runs as a
background task (off the hot path) and uses the shared `llm_service` singleton.

Two correctness/efficiency properties matter here:

1. **Never wipe memory on failure.** `compress_memory` returns `None` when the LLM call fails;
   in that case the replace step is **skipped**, so existing memory is preserved.
2. **Single-pass budget enforcement.** Models can't count characters reliably, so after the LLM
   pass a deterministic enforcement drops the lowest-priority items (oldest events → beliefs →
   facts) until the block fits — computed **in memory from a single read** and persisted in
   **one write**, not a per-item read/write loop. A per-user cooldown
   (`COMPRESSION_COOLDOWN_SECS`) prevents a re-trigger loop.

```python
# app/services/memory_compressor.py
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service          # shared singleton
from app.services.memory_loader import build_memory_block
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT

async def compress_user_memory(user_id: int):
    try:
        async with db_session() as db:
            memory_text, _ = await build_memory_block(db, user_id)
            target = int(config.USER_MEMORY_BUDGET_CHARS * 0.8)
            system_prompt = (
                f"{SYSTEM_COMPRESSION_PROMPT}\n\n"
                f"TARGET CHARACTER BUDGET: {target} characters.\n"
                f"Your compressed memory profile MUST fit within {target} characters."
            )
            compression = await llm_service.compress_memory(user_id, system_prompt, memory_text)
            if compression is None:
                logger.warning(f"Compression failed for user {user_id}; keeping existing memory.")
                return                                     # never wipe on failure
            await models.replace_user_memory(db, user_id, compression)
            await _enforce_budget(db, user_id)             # single read + single write
    except Exception as e:
        logger.error(f"Compression failed for user {user_id}: {e}")
```

> The caller (`UserTaskManager.run_compressor`) enforces the per-user cooldown and acquires the
> shared `memory_lock` so compression never races the extractor.

---

## 🔒 Shared Task Concurrency Lock
Because extraction and compression are executed asynchronously, concurrency issues can arise where the extractor and compressor write or modify user memory simultaneously. 

To prevent data corruption, a unified `memory_lock = asyncio.Lock()` is initialized inside `UserState` inside the `UserTaskManager`. The manager acquires this lock before initiating both the `run_extractor` and `run_compressor` background tasks, guaranteeing sequential executions per user.

---

## 👥 Multi-Party Extraction in Groups *(Phase 9)*

In group chats the buffer is shared (`chat_id`-keyed) and each message carries `sender_id` +
`sender_name`, but **memory stays per `user_id`**. Extraction over a group segment is a single
LLM call that returns updates tagged by participant name; those are mapped back to each
`sender_id` using the segment's own name→id map and saved into each participant's profile via
the same normalized, deduped CRUD described above. This keeps group extraction to one LLM call
while still attributing facts/beliefs/events to the correct person. DMs are unchanged (a single
participant). See [group_chat.md](group_chat.md).
