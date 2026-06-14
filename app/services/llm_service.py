"""LLM client wrapper.

Wraps an OpenAI-compatible endpoint and centralizes: structured-output handling
(``json_object`` by default — the only mode our Gemini proxy accepts; ``native_parse``
for true OpenAI deployments), bounded retries with backoff for transient failures, and
audit logging to ``llm_audit_log`` (off the chat hot path, fire-and-forget).

See docs/development/llm_integration.md and docs/development/hardening_plan.md.
"""
import json
import time
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
from app.services.schemas import (
    MemoryExtraction,
    MemoryCompression,
    MemoryConsolidation,
    ReplyBundle,
    GroupMemoryExtraction,
)
from app.services.reactions import ALLOWED_REACTIONS, normalize_reaction
from app.services.metrics import metrics
from app.prompts.checkin_prompt import SYSTEM_CHECKIN_PROMPT
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
        self, user_id: int, system_prompt: str, chat_history: list[dict],
        *, with_affinity: bool = False,
    ) -> tuple[str, str | None] | tuple[str, str | None, float | None]:
        """Generate the conversational reply and an optional emoji reaction in a single call.

        Uses ``json_object`` mode (verified to preserve reply quality on the configured
        Gemini proxy). Falls back to treating the raw output as the reply if JSON parsing
        fails, so the user always gets an answer.

        ``with_affinity`` controls the return contract:

        - ``False`` (default, DM/addressed path): the prompt and the returned value are
          byte-for-byte unchanged from the original behavior — returns ``(reply, reaction)``.
        - ``True`` (group path): the format clause additionally asks for an optional
          ``affinity_delta`` number in ``[-0.2, 0.2]``, and the method returns
          ``(reply, reaction, affinity_delta)`` where ``affinity_delta`` is a ``float`` or
          ``None`` when the model omits it.
        """
        if with_affinity:
            # Group variant: same reply/reaction contract, plus an optional affinity_delta.
            format_clause = (
                "\n\n---\n## RESPONSE FORMAT (STRICT)\n"
                'Respond with ONLY a JSON object: {"reply": "<message>", "reaction": "<emoji or empty>", '
                '"affinity_delta": <number or omit>}.\n'
                '- "reply": your natural conversational message, obeying every style rule above. '
                "Plain text only — no markdown or code fences. Emojis within the reply are fine if "
                "they fit your persona.\n"
                '- "reaction": INDEPENDENTLY of the reply, optionally pick a SINGLE emoji to react to '
                "the user's latest message with (this is applied as a Telegram reaction on THEIR "
                "message, separate from your reply). Choose ONLY from this list, or an empty string "
                "when no reaction fits (most messages need none):\n"
                f"{' '.join(sorted(ALLOWED_REACTIONS))}\n"
                '- "affinity_delta": OPTIONAL signed number in [-0.2, 0.2] expressing how much the '
                "latest speaker's warmth toward you should shift based on their message (positive when "
                "they engage warmly or invite you in, negative when they are dismissive or want less). "
                "Omit it or use 0 when neutral.\n"
                "Output the raw JSON object only — no preamble, no code fences around it."
            )
        else:
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
            start = time.perf_counter()
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
            reply, reaction, affinity_delta = self._parse_reply_bundle(raw)
            parsed_json = {"reply": reply, "reaction": reaction}
            if with_affinity:
                parsed_json["affinity_delta"] = affinity_delta
            self._fire_log(
                user_id, "chat_reply", inputs,
                {"raw_text": raw, "parsed_json": parsed_json},
                "success",
            )
            metrics.record_llm("chat_reply", ok=True, latency=time.perf_counter() - start)
            if with_affinity:
                return reply, reaction, affinity_delta
            return reply, reaction
        except Exception as e:
            metrics.record_llm("chat_reply", ok=False, latency=time.perf_counter() - start)
            self._fire_log(user_id, "chat_reply", inputs, status="failed", error=traceback.format_exc())
            logger.error(f"Reply generation failed for user {user_id}: {e}")
            raise

    # ------------------------------------------------------------------ #
    # Proactive check-in (one call, never raises into the scheduler)
    # ------------------------------------------------------------------ #
    async def generate_checkin(
        self, user_id: int, system_prompt: str, memory_text: str
    ) -> str | None:
        """Generate a short, memory-grounded proactive check-in opener, or None to stay silent.

        Returns None when the profile is ungroundable (blank memory_text), when the model
        declines (empty or a NOTHING/none/n-a sentinel), or on any error — so the caller
        sends nothing. One LLM call; never raises into the scheduler.
        """
        if not memory_text or not memory_text.strip():
            return None
        messages = [
            {"role": "system", "content": system_prompt + "\n\n" + SYSTEM_CHECKIN_PROMPT},
            {"role": "user", "content": "Write the check-in opener now (or reply with NOTHING)."},
        ]
        max_tokens = (config.MAX_RESPONSE_CHARS // config.CHARS_PER_TOKEN) + 40
        inputs = {"system_prompt": system_prompt, "messages": messages}
        start = time.perf_counter()
        try:
            response = await self._with_retries(
                lambda: self.client.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=messages,
                    temperature=config.REPLY_TEMPERATURE,
                    max_tokens=max_tokens,
                    timeout=30.0,
                ),
                what=f"proactive_checkin u{user_id}",
            )
            raw = (response.choices[0].message.content or "").strip()
            self._fire_log(user_id, "proactive_checkin", inputs, {"raw_text": raw, "parsed_json": None}, "success")
            metrics.record_llm("proactive_checkin", ok=True, latency=time.perf_counter() - start)
            # Decline sentinels -> stay silent.
            if not raw or raw.strip().lower().strip(".!") in ("nothing", "none", "n/a", "na"):
                return None
            return raw
        except Exception as e:
            metrics.record_llm("proactive_checkin", ok=False, latency=time.perf_counter() - start)
            self._fire_log(user_id, "proactive_checkin", inputs, status="failed", error=traceback.format_exc())
            logger.error(f"Proactive check-in generation failed for user {user_id}: {e}")
            return None

    def _parse_reply_bundle(self, raw: str) -> tuple[str, str | None, float | None]:
        """Parse the {reply, reaction, affinity_delta} JSON; degrade gracefully on failure.

        Returns a 3-tuple ``(reply, reaction, affinity_delta)`` internally. ``affinity_delta``
        is parsed only when present as a number and defaults to ``None`` (so the DM-facing
        path, which ignores the 3rd value, is unaffected). The graceful JSON fallback to
        plain text is preserved.
        """
        cleaned = self._strip_fences(raw)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and isinstance(data.get("reply"), str) and data["reply"].strip():
                reaction = normalize_reaction(data.get("reaction")) if config.ENABLE_MESSAGE_REACTIONS else None
                affinity_delta = None
                ad = data.get("affinity_delta")
                if isinstance(ad, (int, float)) and not isinstance(ad, bool):
                    affinity_delta = float(ad)
                return data["reply"].strip(), reaction, affinity_delta
        except (json.JSONDecodeError, ValueError):
            pass
        logger.warning("Reply bundle was not valid JSON; using raw text as the reply.")
        # Fall back to whatever text we got (minus any fences), no reaction, no delta.
        fallback = cleaned if cleaned and cleaned != "{}" else raw
        return fallback.strip(), None, None

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
        start = time.perf_counter()

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
                    metrics.record_llm(call_type, ok=True, latency=time.perf_counter() - start)
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
            metrics.record_llm(call_type, ok=True, latency=time.perf_counter() - start)
            return parsed
        except Exception as e:  # noqa: BLE001
            metrics.record_llm(call_type, ok=False, latency=time.perf_counter() - start)
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

    async def extract_group_memory(
        self, user_id_or_chat_id: int, system_prompt: str, user_history_text: str
    ) -> GroupMemoryExtraction | None:
        """Multi-party memory extraction over a rendered group segment (one LLM call).

        Mirrors :meth:`extract_memory` exactly — same model selection, retry/json_object/
        native_parse handling via ``_structured_call``, same ``EXTRACTION_TEMPERATURE`` and
        45s timeout, and the same ``None``-on-failure contract — but validates against
        :class:`GroupMemoryExtraction` so the result carries per-participant, name-tagged
        updates. ``user_history_text`` is the multi-party segment rendered as
        ``"SenderName: content"`` lines. The first argument is used only for audit logging
        (a group chat_id here, a user_id in the DM path); it does not affect attribution,
        which the caller resolves from the segment's own name->id map.

        Returns ``None`` when the call fails (transient errors exhausted, or unparseable
        output) so the caller can retry; a valid — possibly *empty* — result means success.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_history_text},
        ]
        model = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        return await self._structured_call(
            user_id=user_id_or_chat_id, call_type="group_memory_extraction", model=model,
            messages=messages, schema=GroupMemoryExtraction,
            temperature=config.EXTRACTION_TEMPERATURE, timeout=45.0,
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

    async def consolidate_memory(
        self, user_id: int, system_prompt: str, raw_memory_text: str
    ) -> MemoryConsolidation | None:
        """Consolidate a user's full profile into a refreshed profile + durable insights.

        Returns ``None`` on failure (mirrors ``compress_memory``) so the caller skips the
        write and never wipes memory.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_memory_text},
        ]
        model = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        return await self._structured_call(
            user_id=user_id, call_type="memory_consolidation", model=model, messages=messages,
            schema=MemoryConsolidation, temperature=config.EXTRACTION_TEMPERATURE, timeout=60.0,
        )


# Shared singleton — one client (and connection pool) for the whole process.
llm_service = LLMService()
