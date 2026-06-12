import asyncio
import openai
from app.services.llm_service import LLMService
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT
from loguru import logger

async def test_live():
    logger.info("Initializing LLMService...")
    service = LLMService()

    # 1. Test conversational generation
    logger.info("Testing generate_response...")
    chat_history = [{"role": "user", "content": "hello! I love coding in python, especially building chatbots."}]
    try:
        response = await service.generate_response("You are ThinkMate, a friendly companion.", chat_history)
        logger.info(f"Conversational Reply:\n{response}\n")
        assert len(response) > 0
        
        # 2. Test memory extraction
        logger.info("Testing extract_memory...")
        chat_log_to_extract = (
            "User: yeah, I have a younger brother named Sid.\n"
            "Assistant: oh cool, how's your relationship with Sid?\n"
            "User: we are super close. also, I recently started working as a backend engineer at Google Seattle."
        )
        current_memories = "No memories recorded yet."
        instruction_prompt = f"{SYSTEM_EXTRACTION_PROMPT}\n\n=== CURRENT MEMORIES ===\n{current_memories}\n"
        
        extraction = await service.extract_memory(instruction_prompt, chat_log_to_extract)
        logger.info(f"Extracted Facts: {extraction.new_facts}")
        logger.info(f"Extracted Events: {extraction.events}")
        logger.info(f"Extracted Emotional State: {extraction.emotional_state}")
        
        logger.info(f"New facts list length: {len(extraction.new_facts)}")
        assert len(extraction.new_facts) > 0 or extraction.emotional_state is not None
        logger.info("Live LLM test completed successfully!")
    except openai.AuthenticationError as e:
        logger.warning("=" * 60)
        logger.warning("LLM client connected successfully, but the API key was rejected.")
        logger.warning(f"Error detail: {e}")
        logger.warning("Please verify that your API key is active and correct.")
        logger.warning("=" * 60)
    except Exception as e:
        logger.error(f"Live LLM test failed with unexpected error: {e}")
        raise e

if __name__ == "__main__":
    asyncio.run(test_live())
