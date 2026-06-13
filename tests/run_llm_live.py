"""Manual live check against the configured LLM endpoint (reads .env via app.config).

Not part of the automated suite — it makes real API calls. Run with:
    set PYTHONIOENCODING=utf-8 && PYTHONPATH=. .venv/Scripts/python.exe tests/run_llm_live.py
"""
import asyncio
from loguru import logger
from app.services.llm_service import llm_service
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT


async def main():
    logger.info("1) generate_reply_bundle (reply + reaction in one call)")
    reply, reaction = await llm_service.generate_reply_bundle(
        user_id=999,
        system_prompt="You are ThinkMate, a warm, witty companion. Reply in 1-2 casual sentences.",
        chat_history=[{"role": "user", "content": "i just adopted a kitten named Miso!"}],
    )
    logger.info(f"   reply={reply!r}")
    logger.info(f"   reaction={reaction!r}")
    assert reply

    logger.info("2) extract_memory (structured json_object path)")
    chat_log = (
        "User: yeah, I have a younger brother named Sid.\n"
        "Assistant: oh nice, are you two close?\n"
        "User: super close. also I just started as a backend engineer at Google Seattle."
    )
    instruction = f"{SYSTEM_EXTRACTION_PROMPT}\n\n=== CURRENT MEMORIES ===\nNo memories recorded yet.\n"
    extraction = await llm_service.extract_memory(999, instruction, chat_log)
    assert extraction is not None, "extract_memory returned None (the call failed)"
    logger.info(f"   new_facts={extraction.new_facts}")
    logger.info(f"   new_events={extraction.new_events}")
    logger.info(f"   emotional_state={extraction.emotional_state}")
    assert extraction.new_facts or extraction.new_events or extraction.emotional_state is not None

    logger.info("Live LLM check passed.")


if __name__ == "__main__":
    asyncio.run(main())
