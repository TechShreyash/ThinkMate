import json
import traceback
from datetime import datetime, timezone
from openai import AsyncOpenAI
from loguru import logger
from app.config import config
from app.services.schemas import MemoryExtraction, MemoryCompression
from app.database import get_db

class LLMService:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY
        )

    async def _log_llm_call(
        self,
        user_id: int,
        call_type: str,
        inputs: dict,
        outputs: dict | None = None,
        status: str = "success",
        error: str | None = None
    ):
        """Logs LLM input and output details to the llm_audit_log collection."""
        try:
            db = get_db()
            log_doc = {
                "user_id": user_id,
                "call_type": call_type,
                "inputs": inputs,
                "outputs": outputs or {"raw_text": None, "parsed_json": None},
                "status": status,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            await db["llm_audit_log"].insert_one(log_doc)
        except Exception as e:
            logger.error(f"Failed to log LLM call to database for user {user_id}: {e}")

    async def generate_response(self, user_id: int, system_prompt: str, chat_history: list[dict]) -> str:
        """Standard chat completions for assistant conversational replies."""
        messages = [{"role": "system", "content": system_prompt}] + chat_history
        # Calculate max_tokens using char-to-token ratio
        max_tokens = config.MAX_RESPONSE_CHARS // config.CHARS_PER_TOKEN
        inputs = {
            "system_prompt": system_prompt,
            "messages": messages
        }
        try:
            response = await self.client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=messages,
                temperature=0.7,
                max_tokens=max_tokens,
                timeout=30.0
            )
            raw_text = response.choices[0].message.content.strip()
            await self._log_llm_call(
                user_id=user_id,
                call_type="chat_reply",
                inputs=inputs,
                outputs={"raw_text": raw_text, "parsed_json": None},
                status="success"
            )
            return raw_text
        except Exception as e:
            error_trace = traceback.format_exc()
            await self._log_llm_call(
                user_id=user_id,
                call_type="chat_reply",
                inputs=inputs,
                status="failed",
                error=error_trace
            )
            logger.error(f"Chat generation API call failed for user {user_id}: {e}")
            raise e

    async def extract_memory(self, user_id: int, system_prompt: str, user_history_text: str) -> MemoryExtraction:
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
        inputs = {
            "system_prompt": system_prompt,
            "messages": messages
        }
        
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
                raw_text = completion.choices[0].message.content
                await self._log_llm_call(
                    user_id=user_id,
                    call_type="memory_extraction",
                    inputs=inputs,
                    outputs={"raw_text": raw_text, "parsed_json": parsed.model_dump()},
                    status="success"
                )
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
            parsed = MemoryExtraction.model_validate_json(clean_json)
            await self._log_llm_call(
                user_id=user_id,
                call_type="memory_extraction",
                inputs=inputs,
                outputs={"raw_text": raw_json, "parsed_json": parsed.model_dump()},
                status="success"
            )
            return parsed
            
        except Exception as e:
            error_trace = traceback.format_exc()
            await self._log_llm_call(
                user_id=user_id,
                call_type="memory_extraction",
                inputs=inputs,
                status="failed",
                error=error_trace
            )
            logger.error(f"Structured memory extraction fallback failed for user {user_id}: {e}")
            # Return empty extraction skeleton instead of crashing
            return MemoryExtraction()

    async def compress_memory(self, user_id: int, system_prompt: str, raw_memory_text: str) -> MemoryCompression:
        """Compresses facts and events using similar Pydantic validations."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_memory_text}
        ]
        model_name = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        inputs = {
            "system_prompt": system_prompt,
            "messages": messages
        }
        
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
                raw_text = completion.choices[0].message.content
                await self._log_llm_call(
                    user_id=user_id,
                    call_type="memory_compression",
                    inputs=inputs,
                    outputs={"raw_text": raw_text, "parsed_json": parsed.model_dump()},
                    status="success"
                )
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

            parsed = MemoryCompression.model_validate_json(clean_json)
            await self._log_llm_call(
                user_id=user_id,
                call_type="memory_compression",
                inputs=inputs,
                outputs={"raw_text": raw_json, "parsed_json": parsed.model_dump()},
                status="success"
            )
            return parsed
        except Exception as e:
            error_trace = traceback.format_exc()
            await self._log_llm_call(
                user_id=user_id,
                call_type="memory_compression",
                inputs=inputs,
                status="failed",
                error=error_trace
            )
            logger.error(f"Memory compression fallback failed for user {user_id}: {e}")
            return MemoryCompression()

