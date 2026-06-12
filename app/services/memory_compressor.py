import asyncio
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import LLMService
from app.services.memory_loader import build_memory_block
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT

async def compress_user_memory(user_id: int):
    """
    Background non-blocking task that retrieves current user memories,
    queries the extraction LLM to compress them, and atomically replaces
    them in the database.
    """
    logger.info(f"Memory compression triggered in background for user {user_id}...")
    try:
        # Establish a separate db connection session for the background task
        async with db_session() as db:
            # 1. Compile the current memories block
            memory_text, _ = await build_memory_block(db, user_id)
            
            # Calculate target character size (80% of budget)
            target_chars = int(config.USER_MEMORY_BUDGET_CHARS * 0.8)
            
            # 2. Setup system prompt with instruction + target
            system_prompt = (
                f"{SYSTEM_COMPRESSION_PROMPT}\n\n"
                f"TARGET CHARACTER BUDGET: {target_chars} characters.\n"
                f"Your compressed memory profile MUST fit within {target_chars} characters."
            )
            
            # 3. Call LLM compression service
            llm = LLMService()
            compression_res = await llm.compress_memory(user_id, system_prompt, memory_text)
            
            # 4. Save/replace in DB atomically
            await models.replace_user_memory(db, user_id, compression_res)
            
            logger.info(f"Memory compression successfully completed in background for user {user_id}.")
    except Exception as e:
        logger.error(f"Failed to compress memory in background for user {user_id}: {e}")
