import json
from openai import AsyncOpenAI
from loguru import logger
from app.config import config
from app.services.schemas import MemoryExtraction, MemoryConsolidation, MemoryCompression

class LLMService:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY
        )

    async def generate_response(self, system_prompt: str, chat_history: list[dict]) -> str:
        """Standard chat completions for assistant conversational replies."""
        messages = [{"role": "system", "content": system_prompt}] + chat_history
        # Calculate max_tokens using char-to-token ratio
        max_tokens = config.MAX_RESPONSE_CHARS // config.CHARS_PER_TOKEN
        try:
            response = await self.client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=messages,
                temperature=0.7,
                max_tokens=max_tokens,
                timeout=30.0
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Chat generation API call failed: {e}")
            raise e

    async def extract_memory(self, system_prompt: str, user_history_text: str) -> MemoryExtraction:
        """
        Extracts structured memory updates.
        Attempts to use OpenAI's native .parse() endpoint.
        Falls back to JSON mode + manual Pydantic validation if using a local/custom LLM engine.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_history_text}
        ]
        
        model_name = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        
        # 1. Attempt using native beta parse endpoint (OpenAI cloud or compatible)
        try:
            logger.debug(f"Attempting native structured output parsing with model {model_name}...")
            completion = await self.client.beta.chat.completions.parse(
                model=model_name,
                messages=messages,
                response_format=MemoryExtraction,
                temperature=0.1,
                timeout=45.0
            )
            parsed = completion.choices[0].message.parsed
            if parsed is not None:
                return parsed
            logger.warning("Native parsing returned None, falling back to manual JSON mode.")
        except Exception as e:
            logger.warning(f"Native beta.chat.completions.parse failed: {e}. Falling back to standard JSON mode...")

        # 2. Local/Custom Fallback (Ollama/LM Studio/OpenRouter/Custom Proxies)
        # Append schema instructions to system prompt
        schema_json = json.dumps(MemoryExtraction.model_json_schema(), indent=2)
        fallback_system_prompt = (
            f"{system_prompt}\n\n"
            f"IMPORTANT: You MUST respond with a valid JSON object matching this JSON schema:\n"
            f"```json\n{schema_json}\n```\n"
            f"Do not include any explanation, code blocks outside json, or preamble. Return ONLY the raw JSON."
        )
        
        fallback_messages = [
            {"role": "system", "content": fallback_system_prompt},
            {"role": "user", "content": user_history_text}
        ]

        try:
            response = await self.client.chat.completions.create(
                model=model_name,
                messages=fallback_messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=45.0
            )
            
            raw_json = response.choices[0].message.content
            logger.debug(f"Raw local JSON output for extraction: {raw_json}")
            
            # Remove possible markdown code block wrappers
            clean_json = raw_json.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            clean_json = clean_json.strip()
            
            # Parse and validate with Pydantic
            return MemoryExtraction.model_validate_json(clean_json)
            
        except Exception as e:
            logger.error(f"Structured memory extraction fallback failed: {e}")
            # Return empty extraction skeleton instead of crashing
            return MemoryExtraction()

    async def consolidate_memory(self, system_prompt: str, raw_facts_json: str) -> MemoryConsolidation:
        """Consolidates facts using similar Pydantic validations."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_facts_json}
        ]
        model_name = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        
        try:
            logger.debug(f"Attempting native structured output consolidation with model {model_name}...")
            completion = await self.client.beta.chat.completions.parse(
                model=model_name,
                messages=messages,
                response_format=MemoryConsolidation,
                temperature=0.1,
                timeout=45.0
            )
            parsed = completion.choices[0].message.parsed
            if parsed is not None:
                return parsed
            logger.warning("Native consolidation returned None, falling back to manual JSON mode.")
        except Exception as e:
            logger.warning(f"Native beta.chat.completions.parse for consolidation failed: {e}. Falling back to standard JSON mode...")

        schema_json = json.dumps(MemoryConsolidation.model_json_schema(), indent=2)
        fallback_system_prompt = (
            f"{system_prompt}\n\n"
            f"IMPORTANT: You MUST respond with a valid JSON object matching this JSON schema:\n"
            f"```json\n{schema_json}\n```\n"
            f"Do not include any explanation, code blocks outside json, or preamble. Return ONLY the raw JSON."
        )
        
        fallback_messages = [
            {"role": "system", "content": fallback_system_prompt},
            {"role": "user", "content": raw_facts_json}
        ]

        try:
            response = await self.client.chat.completions.create(
                model=model_name,
                messages=fallback_messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=45.0
            )
            raw_json = response.choices[0].message.content
            
            clean_json = raw_json.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            clean_json = clean_json.strip()

            return MemoryConsolidation.model_validate_json(clean_json)
        except Exception as e:
            logger.error(f"Memory consolidation fallback failed: {e}")
            return MemoryConsolidation()

    async def compress_memory(self, system_prompt: str, raw_memory_text: str) -> MemoryCompression:
        """Compresses facts and events using similar Pydantic validations."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_memory_text}
        ]
        model_name = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        
        try:
            logger.debug(f"Attempting native structured output compression with model {model_name}...")
            completion = await self.client.beta.chat.completions.parse(
                model=model_name,
                messages=messages,
                response_format=MemoryCompression,
                temperature=0.1,
                timeout=60.0
            )
            parsed = completion.choices[0].message.parsed
            if parsed is not None:
                return parsed
            logger.warning("Native compression returned None, falling back to manual JSON mode.")
        except Exception as e:
            logger.warning(f"Native beta.chat.completions.parse for compression failed: {e}. Falling back to standard JSON mode...")

        schema_json = json.dumps(MemoryCompression.model_json_schema(), indent=2)
        fallback_system_prompt = (
            f"{system_prompt}\n\n"
            f"IMPORTANT: You MUST respond with a valid JSON object matching this JSON schema:\n"
            f"```json\n{schema_json}\n```\n"
            f"Do not include any explanation, code blocks outside json, or preamble. Return ONLY the raw JSON."
        )
        
        fallback_messages = [
            {"role": "system", "content": fallback_system_prompt},
            {"role": "user", "content": raw_memory_text}
        ]

        try:
            response = await self.client.chat.completions.create(
                model=model_name,
                messages=fallback_messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=60.0
            )
            raw_json = response.choices[0].message.content
            
            clean_json = raw_json.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            clean_json = clean_json.strip()

            return MemoryCompression.model_validate_json(clean_json)
        except Exception as e:
            logger.error(f"Memory compression fallback failed: {e}")
            return MemoryCompression()

