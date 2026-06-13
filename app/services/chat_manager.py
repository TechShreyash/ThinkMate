"""Response-flow orchestrator.

For each user batch: append to the buffer, trigger background extraction on overflow,
assemble the system prompt (cached persona + compiled memory), generate the reply and an
optional reaction in a single LLM call, persist the reply, and trigger background
compression (rate-limited) when the memory profile outgrows its budget.
"""
import os
import asyncio
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.prompts.system_prompt import build_system_prompt

_DEFAULT_PERSONA = "You are ThinkMate, a warm, witty AI companion."
_persona_cache: dict = {"path": None, "mtime": None, "content": _DEFAULT_PERSONA}


def _load_persona() -> str:
    """Return the persona text, re-reading the file only when it changes on disk."""
    path = config.PERSONA_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _DEFAULT_PERSONA
    if _persona_cache["path"] != path or _persona_cache["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as f:
                _persona_cache.update(path=path, mtime=mtime, content=f.read())
        except OSError as e:
            logger.warning(f"Could not read persona file {path}: {e}")
            return _persona_cache["content"] or _DEFAULT_PERSONA
    return _persona_cache["content"]


async def handle_message(
    db: AsyncIOMotorDatabase, user_id: int, user_text: str
) -> tuple[str, str | None]:
    """Process one combined user message; return ``(reply_text, reaction_emoji_or_None)``."""
    # 1. Append user message; the returned array gives us char count + active history
    #    in a single round-trip.
    messages = await models.add_message_to_buffer(db, user_id, "user", user_text)
    buffer_chars = sum(len(m["content"]) for m in messages)
    active_history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # 2. Buffer overflow -> non-blocking background extraction.
    if buffer_chars >= config.CHAT_BUFFER_MAX_CHARS:
        from app.services.user_task_manager import user_task_manager
        logger.info(f"Buffer overflow for user {user_id} ({buffer_chars} chars); launching extraction.")
        asyncio.create_task(user_task_manager.run_extractor(user_id))

    # 3. Assemble system prompt (cached persona + compiled memory).
    persona = _load_persona()
    memory_block, needs_compression = await build_memory_block(db, user_id)
    system_prompt = build_system_prompt(persona, memory_block)

    # 4. Single LLM call -> reply + optional reaction.
    reply_text, reaction = await llm_service.generate_reply_bundle(user_id, system_prompt, active_history)

    # 5. Persist the assistant reply.
    await models.add_message_to_buffer(db, user_id, "assistant", reply_text)

    # 6. Memory over budget -> rate-limited background compression.
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_compressor(user_id))

    return reply_text, reaction
