"""LLM client wrapper.

Wraps an OpenAI-compatible endpoint and centralizes: structured-output handling
(``json_object`` by default — the only mode our Gemini proxy accepts; ``native_parse``
for true OpenAI deployments), bounded retries with backoff for transient failures, and
audit logging to ``llm_audit_log`` (off the chat hot path, fire-and-forget).

See docs/development/llm_integration.md and docs/development/hardening_plan.md.
"""
import json
import asyncio
import traceback
from datetime import datetime, timezone
from typing import TypeVar
from openai import (
    AsyncOpenAI,
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)
from pydantic import BaseModel
from loguru import logger
from app.config import config
from app.services.schemas import MemoryExtraction, MemoryCompression, ReplyBundle
from app.services.reactions import ALLOWED_REACTIONS, normalize_reaction
from app.database import get_db

T = TypeVar("T", bound=BaseModel)

# Transient errors worth retrying; 4xx (e.g. BadRequest) are not retried.
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)

# Cap on the size of any single string stored in the audit log, to bound storage growth.
_MAX_LOG_FIELD = 4000


class LLMService:
    def __init__(self):
        self.client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # Audit logging
    # ------------------------------------------------------------------ #
    @classmethod
    def _truncate(cls, obj):
        """Recursively cap long strings so audit documents stay small."""
        if isinstance(obj, str):
            if len(obj) <= _MAX_LOG_FIELD:
                return obj
            return obj[:_MAX_LOG_FIELD] + f"...[+{len(obj) - _MAX_LOG_FIELD} chars]"
        if isinstance(obj, dict):
            return {k: cls._truncate(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [cls._truncate(v) for v in obj]
        return obj

    async def _log_llm_call(
        self,
        user_id: int,
        call_type: str,
        inputs: dict,
        outputs: dict | None = None,
        status: str = "success",
        error: str | None = None,
    ):
        """Insert one audit record. ``timestamp`` is a real datetime so a TTL index applies."""
        try:
            db = get_db()
            await db["llm_audit_log"].insert_one({
                "user_id": user_id,
                "call_type": call_type,
                "inputs": self._truncate(inputs),
                "outputs": self._truncate(outputs) if outputs else {"raw_text": None, "parsed_json": None},
                "status": status,
                "error": error,
                "timestamp": datetime.now(timezone.utc),
            })
        except Exception as e:  # noqa: BLE001 - logging must never break the caller
            logger.error(f"Failed to log LLM call for user {user_id}: {e}")

    def _fire_log(self, *args, **kwargs):
        """Schedule an audit write without blocking the caller (keeps it off the hot path)."""
        try:
            task = asyncio.get_running_loop().create_task(self._log_llm_call(*args, **kwargs))
        except RuntimeError:
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------ #
    # Low-level helpers
    # ------------------------------------------------------------------ #
    async def _with_retries(self, make_call, *, what: str):
        """Run ``make_call`` (a coroutine factory), retrying transient errors with backoff."""
        last_exc: Exception | None = None
        for attempt in range(config.LLM_MAX_RETRIES + 1):
            try:
                return await make_call()
            except _RETRYABLE as e:
                last_exc = e
                if attempt >= config.LLM_MAX_RETRIES:
                    break
                delay = config.LLM_RETRY_BASE_DELAY_SECS * (2 ** attempt)
                logger.warning(
                    f"{what}: transient {type(e).__name__}, retry {attempt + 1}/{config.LLM_MAX_RETRIES} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _strip_fences(raw: str | None) -> str:
        """Strip ```json ... ``` fences some models add around JSON output."""
        if not raw:
            return "{}"
        s = raw.strip()
        if s.startswith("```"):
            s = s[3:]
            if s[:4].lower() == "json":
                s = s[4:]
            s = s.strip()
            if s.endswith("```"):
                s = s[:-3]
        return s.strip() or "{}"

    # ------------------------------------------------------------------ #
    # Conversational reply (+ optional reaction) — one call
    # ------------------------------------------------------------------ #
    async def generate_reply_bundle(
        self, user_id: int, system_prompt: str, chat_history: list[dict]
    ) -> tuple[str, str | None]:
        """Generate the conversational reply and an optional emoji reaction in a single call.

        Uses ``json_object`` mode (verified to preserve reply quality on the configured
        Gemini proxy). Falls back to treating the raw output as the reply if JSON parsing
        fails, so the user always gets an answer.
        """
        format_clause = (
            "\n\n---\n## RESPONSE FORMAT (STRICT)\n"
            'Respond with ONLY a JSON object: {"reply": "<message>", "reaction": "<emoji or empty>"}.\n'
            '- "reply": your natural conversational message, obeying every style rule above. '
            "Plain text only — no markdown or code fences. Emojis within the reply are fine if "
            "they fit your persona.\n"
            '- "reaction": INDEPENDENTLY of the reply, optionally pick a SINGLE emoji to react to '
            "the user's latest message with (this is applied as a Telegram reaction on THEIR "
            "message, separate from your reply). Choose ONLY from this list, or an empty string "
            "when no reaction fits (most messages need none):\n"
            f"{' '.join(sorted(ALLOWED_REACTIONS))}\n"
            "Output the raw JSON object only — no preamble, no code fences around it."
        )
        messages = [{"role": "system", "content": system_prompt + format_clause}] + chat_history
        # Budget tokens for the reply text plus the small JSON envelope.
        max_tokens = (config.MAX_RESPONSE_CHARS // config.CHARS_PER_TOKEN) + 80
        inputs = {"system_prompt": system_prompt, "messages": messages}

        try:
            response = await self._with_retries(
                lambda: self.client.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=messages,
                    temperature=config.REPLY_TEMPERATURE,
                    max_tokens=max_tokens,
                    timeout=30.0,
                    response_format={"type": "json_object"},
                ),
                what=f"reply_bundle u{user_id}",
            )
            raw = (response.choices[0].message.content or "").strip()
            reply, reaction = self._parse_reply_bundle(raw)
            self._fire_log(
                user_id, "chat_reply", inputs,
                {"raw_text": raw, "parsed_json": {"reply": reply, "reaction": reaction}},
                "success",
            )
            return reply, reaction
        except Exception as e:
            self._fire_log(user_id, "chat_reply", inputs, status="failed", error=traceback.format_exc())
            logger.error(f"Reply generation failed for user {user_id}: {e}")
            raise

    def _parse_reply_bundle(self, raw: str) -> tuple[str, str | None]:
        """Parse the {reply, reaction} JSON; degrade gracefully to plain text on failure."""
        cleaned = self._strip_fences(raw)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and isinstance(data.get("reply"), str) and data["reply"].strip():
                reaction = normalize_reaction(data.get("reaction")) if config.ENABLE_MESSAGE_REACTIONS else None
                return data["reply"].strip(), reaction
        except (json.JSONDecodeError, ValueError):
            pass
        logger.warning("Reply bundle was not valid JSON; using raw text as the reply.")
        # Fall back to whatever text we got (minus any fences), no reaction.
        fallback = cleaned if cleaned and cleaned != "{}" else raw
        return fallback.strip(), None

    # ------------------------------------------------------------------ #
    # Structured memory calls (extraction / compression)
    # ------------------------------------------------------------------ #
    async def _structured_call(
        self, *, user_id: int, call_type: str, model: str, messages: list[dict],
        schema: type[T], temperature: float, timeout: float,
    ) -> T | None:
        """Return a validated ``schema`` instance, or ``None`` on failure.

        Honors ``LLM_STRUCTURED_MODE``: ``native_parse`` first (OpenAI structured outputs),
        otherwise/falling back to ``json_object`` with schema instructions appended.
        """
        inputs = {"system_prompt": messages[0]["content"], "messages": messages}

        if config.LLM_STRUCTURED_MODE == "native_parse":
            try:
                completion = await self._with_retries(
                    lambda: self.client.beta.chat.completions.parse(
                        model=model, messages=messages, response_format=schema,
                        temperature=temperature, timeout=timeout,
                    ),
                    what=f"{call_type} parse u{user_id}",
                )
                parsed = completion.choices[0].message.parsed
                if parsed is not None:
                    self._fire_log(
                        user_id, call_type, inputs,
                        {"raw_text": completion.choices[0].message.content, "parsed_json": parsed.model_dump()},
                        "success",
                    )
                    return parsed
                logger.warning(f"{call_type}: native parse returned None; falling back to json_object.")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{call_type}: native parse failed ({e}); falling back to json_object.")

        # json_object path (default; works with Gemini/local proxies).
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        json_messages = [dict(m) for m in messages]
        json_messages[0] = {
            **json_messages[0],
            "content": (
                f"{messages[0]['content']}\n\n"
                "IMPORTANT: Respond with ONLY a valid JSON object matching this schema:\n"
                f"```json\n{schema_json}\n```\n"
                "No explanation or preamble. Return ONLY the raw JSON."
            ),
        }
        try:
            response = await self._with_retries(
                lambda: self.client.chat.completions.create(
                    model=model, messages=json_messages,
                    response_format={"type": "json_object"},
                    temperature=temperature, timeout=timeout,
                ),
                what=f"{call_type} json u{user_id}",
            )
            raw = response.choices[0].message.content
            parsed = schema.model_validate_json(self._strip_fences(raw))
            self._fire_log(
                user_id, call_type, inputs,
                {"raw_text": raw, "parsed_json": parsed.model_dump()}, "success",
            )
            return parsed
        except Exception as e:  # noqa: BLE001
            self._fire_log(user_id, call_type, inputs, status="failed", error=traceback.format_exc())
            logger.error(f"{call_type}: structured call failed for user {user_id}: {e}")
            return None

    async def extract_memory(
        self, user_id: int, system_prompt: str, user_history_text: str
    ) -> MemoryExtraction | None:
        """Extract structured memory updates from a conversation segment.

        Returns ``None`` when the call fails (transient errors exhausted, or unparseable
        output) so the caller can retry. A valid — possibly *empty* — ``MemoryExtraction``
        means the call succeeded (an empty result simply means nothing was worth saving).
        Keeping these two cases distinct is what lets the extractor avoid trimming the buffer
        after a genuine failure.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_history_text},
        ]
        model = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        return await self._structured_call(
            user_id=user_id, call_type="memory_extraction", model=model, messages=messages,
            schema=MemoryExtraction, temperature=config.EXTRACTION_TEMPERATURE, timeout=45.0,
        )

    async def compress_memory(
        self, user_id: int, system_prompt: str, raw_memory_text: str
    ) -> MemoryCompression | None:
        """Compress a user's full memory profile to fit the character budget.

        Returns ``None`` when the underlying LLM call fails or its output can't be parsed,
        so the caller can distinguish a genuine failure from a valid result and skip the
        memory-replacing write — a failed compression must never wipe existing memory.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_memory_text},
        ]
        model = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        return await self._structured_call(
            user_id=user_id, call_type="memory_compression", model=model, messages=messages,
            schema=MemoryCompression, temperature=config.EXTRACTION_TEMPERATURE, timeout=60.0,
        )


# Shared singleton — one client (and connection pool) for the whole process.
llm_service = LLMService()
