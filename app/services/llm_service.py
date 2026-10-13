"""LLM client wrapper.

Wraps an OpenAI-compatible endpoint and centralizes: structured-output handling
(``json_object`` by default; ``native_parse`` for supported deployments), separate
chat/memory model profiles with reasoning effort and optional fallback models,
bounded retries with backoff for transient failures, and
audit logging to ``llm_audit_log`` (off the chat hot path, fire-and-forget).

See docs/development/llm_integration.md and docs/development/hardening_plan.md.
"""

import asyncio
import json
import math
import time
import traceback
from datetime import UTC, datetime
from typing import TypeVar

from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from app.config import config
from app.database import get_db
from app.prompts.checkin_prompt import SYSTEM_CHECKIN_PROMPT
from app.services.metrics import metrics
from app.services.reactions import ALLOWED_REACTIONS, normalize_reaction
from app.services.schemas import (
    GroupMemoryExtraction,
    MemoryCompression,
    MemoryConsolidation,
    MemoryExtraction,
)

T = TypeVar("T", bound=BaseModel)

# Transient errors worth retrying; 4xx (e.g. BadRequest) are not retried.
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


# To prevent MongoDB bloat and reduce sensitive-content retention, audit logs store
# input/output length metadata instead of full prompt or completion text.
class LLMService:
    def __init__(self):
        # max_retries=0: our _with_retries is the single, intentional retry policy.
        # The SDK's default (2) would stack a second hidden layer that also retries
        # timeouts, multiplying in-flight generations on the proxy under load.
        self.client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, max_retries=0
        )
        self._bg_tasks: set[asyncio.Task] = set()

    async def close(self, *, timeout: float = 5.0) -> None:
        """Drain audit writes before closing the shared HTTP client."""
        try:
            tasks = set(self._bg_tasks)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=timeout)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.client.close()

    # ------------------------------------------------------------------ #
    # Audit logging
    # ------------------------------------------------------------------ #
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

            input_summary = {
                k: (len(str(v)) if v is not None else 0) for k, v in inputs.items()
            }

            output_summary = {"raw_text": 0, "parsed_json": 0}
            if outputs:
                if "raw_text" in outputs and outputs["raw_text"] is not None:
                    output_summary["raw_text"] = len(str(outputs["raw_text"]))
                if "parsed_json" in outputs and outputs["parsed_json"] is not None:
                    output_summary["parsed_json"] = len(str(outputs["parsed_json"]))

            await db["llm_audit_log"].insert_one(
                {
                    "user_id": user_id,
                    "call_type": call_type,
                    "inputs": input_summary,
                    "outputs": output_summary,
                    "status": status,
                    "error": error,
                    "timestamp": datetime.now(UTC),
                }
            )
        except Exception as e:  # noqa: BLE001 - logging must never break the caller
            logger.error(f"Failed to log LLM call for user {user_id}: {e}")

    def _fire_log(self, *args, **kwargs):
        """Schedule an audit write without blocking the caller (keeps it off the hot path)."""
        try:
            task = asyncio.get_running_loop().create_task(
                self._log_llm_call(*args, **kwargs)
            )
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
                delay = config.LLM_RETRY_BASE_DELAY_SECS * (2**attempt)
                logger.warning(
                    f"{what}: transient {type(e).__name__}, retry {attempt + 1}/{config.LLM_MAX_RETRIES} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
        if last_exc is None:
            raise ValueError("LLM_MAX_RETRIES must be nonnegative")
        raise last_exc

    @staticmethod
    def _model_kwargs(model: str, reasoning_effort: str) -> dict[str, str]:
        """Leave reasoning to the endpoint unless the profile explicitly sets it."""
        kwargs = {"model": model}
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        return kwargs

    async def _with_model_fallback(
        self,
        make_call,
        *,
        model: str,
        reasoning_effort: str,
        fallback_model: str,
        fallback_reasoning_effort: str,
        what: str,
    ):
        """Try the primary profile, then at most one distinct fallback profile.

        ``make_call`` owns transport retries and output validation for each model.
        Cancellation propagates without issuing a fallback request.
        """
        try:
            return await make_call(model, reasoning_effort)
        except Exception as exc:
            if not fallback_model or fallback_model == model:
                raise
            logger.warning(
                "{}: model {} failed ({}); trying fallback model {}",
                what,
                model,
                type(exc).__name__,
                fallback_model,
            )
        return await make_call(fallback_model, fallback_reasoning_effort)

    async def _chat_completion(self, *, messages: list[dict], what: str, **kwargs):
        """Use the chat profile for replies and proactive check-ins."""

        async def make_call(model: str, reasoning_effort: str):
            return await self._with_retries(
                lambda: self.client.chat.completions.create(
                    **self._model_kwargs(model, reasoning_effort),
                    messages=messages,
                    timeout=config.LLM_REQUEST_TIMEOUT_SECS,
                    **kwargs,
                ),
                what=f"{what} model={model}",
            )

        return await self._with_model_fallback(
            make_call,
            model=config.chat_model,
            reasoning_effort=config.LLM_CHAT_REASONING_EFFORT,
            fallback_model=config.LLM_CHAT_FALLBACK_MODEL,
            fallback_reasoning_effort=config.LLM_CHAT_FALLBACK_REASONING_EFFORT,
            what=what,
        )

    @staticmethod
    def _strip_fences(raw: str | None) -> str:
        """Strip ```json ... ``` fences some models add around JSON output."""
        if not raw:
            return ""
        s = raw.strip()
        if s.startswith("```"):
            s = s[3:]
            if s[:4].lower() == "json":
                s = s[4:]
            s = s.strip()
            s = s.removesuffix("```")
        return s.strip()

    # ------------------------------------------------------------------ #
    # Conversational reply (+ optional reaction) — one call
    # ------------------------------------------------------------------ #
    async def generate_reply_bundle(
        self,
        user_id: int,
        system_prompt: str,
        chat_history: list[dict],
        *,
        with_affinity: bool = False,
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
                "Telegram text only. Minimal bullets, numbered steps, or short code snippets are "
                "okay when the user asks or the task clearly needs them; avoid heavy markdown. "
                "Emojis within the reply are fine if they fit your persona.\n"
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
                "Telegram text only. Minimal bullets, numbered steps, or short code snippets are "
                "okay when the user asks or the task clearly needs them; avoid heavy markdown. "
                "Emojis within the reply are fine if they fit your persona.\n"
                '- "reaction": INDEPENDENTLY of the reply, optionally pick a SINGLE emoji to react to '
                "the user's latest message with (this is applied as a Telegram reaction on THEIR "
                "message, separate from your reply). Choose ONLY from this list, or an empty string "
                "when no reaction fits (most messages need none):\n"
                f"{' '.join(sorted(ALLOWED_REACTIONS))}\n"
                "Output the raw JSON object only — no preamble, no code fences around it."
            )
        messages = [
            {"role": "system", "content": system_prompt + format_clause}
        ] + chat_history
        # No max_tokens cap: response length is governed by the persona/style rules in
        # the system prompt, not a hard token ceiling. Capping here risked truncating the
        # JSON envelope mid-string (invalid JSON -> raw-text fallback). See
        # docs/development/llm_integration.md.
        inputs = {"system_prompt": system_prompt, "messages": messages}

        try:
            start = time.perf_counter()
            response = await self._chat_completion(
                messages=messages,
                response_format={"type": "json_object"},
                what=f"reply_bundle u{user_id}",
            )
            raw = (response.choices[0].message.content or "").strip()
            reply, reaction, affinity_delta = self._parse_reply_bundle(raw)
            parsed_json = {"reply": reply, "reaction": reaction}
            if with_affinity:
                parsed_json["affinity_delta"] = affinity_delta
            self._fire_log(
                user_id,
                "chat_reply",
                inputs,
                {"raw_text": raw, "parsed_json": parsed_json},
                "success",
            )
            metrics.record_llm(
                "chat_reply", ok=True, latency=time.perf_counter() - start
            )
            if with_affinity:
                return reply, reaction, affinity_delta
            return reply, reaction
        except Exception as e:
            metrics.record_llm(
                "chat_reply", ok=False, latency=time.perf_counter() - start
            )
            self._fire_log(
                user_id,
                "chat_reply",
                inputs,
                status="failed",
                error=traceback.format_exc(),
            )
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
            {
                "role": "system",
                "content": system_prompt + "\n\n" + SYSTEM_CHECKIN_PROMPT,
            },
            {
                "role": "user",
                "content": "Write the check-in opener in the user's recorded preferred language and script, or English if unknown (or reply with NOTHING).",
            },
        ]
        # No max_tokens cap: the check-in length is governed by the persona/check-in
        # prompt, not a hard token ceiling.
        inputs = {"system_prompt": system_prompt, "messages": messages}
        start = time.perf_counter()
        try:
            response = await self._chat_completion(
                messages=messages,
                what=f"proactive_checkin u{user_id}",
            )
            raw = (response.choices[0].message.content or "").strip()
            self._fire_log(
                user_id,
                "proactive_checkin",
                inputs,
                {"raw_text": raw, "parsed_json": None},
                "success",
            )
            metrics.record_llm(
                "proactive_checkin", ok=True, latency=time.perf_counter() - start
            )
            # Normalize wrappers only to recognize declines; keep real openers intact.
            decline_candidate = self._strip_fences(raw)
            if raw.startswith("```") and raw.endswith("```"):
                language, separator, fenced_body = decline_candidate.partition("\n")
                if separator and language.strip().lower() in ("text", "plaintext"):
                    decline_candidate = fenced_body
            decline_candidate = decline_candidate.strip().strip(".!").strip()
            quote_pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
            while (
                len(decline_candidate) >= 2
                and quote_pairs.get(decline_candidate[0]) == decline_candidate[-1]
            ):
                decline_candidate = decline_candidate[1:-1].strip().strip(".!").strip()
            if not raw or decline_candidate.lower() in (
                "nothing",
                "none",
                "n/a",
                "na",
            ):
                return None
            return raw
        # Best-effort instrumentation must not discard an LLM reply.
        except Exception as e:  # noqa: BLE001
            metrics.record_llm(
                "proactive_checkin", ok=False, latency=time.perf_counter() - start
            )
            self._fire_log(
                user_id,
                "proactive_checkin",
                inputs,
                status="failed",
                error=traceback.format_exc(),
            )
            logger.error(
                f"Proactive check-in generation failed for user {user_id}: {e}"
            )
            return None

    def _parse_reply_bundle(self, raw: str) -> tuple[str, str | None, float | None]:
        """Parse the {reply, reaction, affinity_delta} JSON; degrade gracefully on failure.

        Returns a 3-tuple ``(reply, reaction, affinity_delta)`` internally. ``affinity_delta``
        is parsed only as a finite number, bounded to the prompt's [-0.2, 0.2] range,
        and defaults to ``None`` (so the DM-facing
        path, which ignores the 3rd value, is unaffected). The graceful JSON fallback to
        plain text is preserved.
        """
        cleaned = self._strip_fences(raw)
        try:
            data = json.loads(cleaned)
            if (
                isinstance(data, dict)
                and isinstance(data.get("reply"), str)
                and data["reply"].strip()
            ):
                reaction = (
                    normalize_reaction(data.get("reaction"))
                    if config.ENABLE_MESSAGE_REACTIONS
                    else None
                )
                affinity_delta = None
                ad = data.get("affinity_delta")
                # Bound before float conversion so arbitrarily large JSON integers
                # cannot overflow and discard an otherwise valid reply.
                if (
                    isinstance(ad, (int, float))
                    and not isinstance(ad, bool)
                    and (isinstance(ad, int) or math.isfinite(ad))
                ):
                    affinity_delta = float(max(-0.2, min(0.2, ad)))
                return data["reply"].strip(), reaction, affinity_delta
            if isinstance(data, dict):
                logger.warning("Reply bundle contained no usable reply text.")
                return "", None, None
        except json.JSONDecodeError, ValueError:
            pass
        logger.warning("Reply bundle was not valid JSON; using raw text as the reply.")
        # Fall back to whatever text we got (minus any fences), no reaction, no delta.
        return cleaned, None, None

    # ------------------------------------------------------------------ #
    # Structured memory calls (extraction / compression)
    # ------------------------------------------------------------------ #
    async def _structured_call(
        self,
        *,
        user_id: int,
        call_type: str,
        model: str,
        messages: list[dict],
        schema: type[T],
        timeout: float,
        required_fields: tuple[str, ...] = (),
        reasoning_effort: str = "",
        fallback_model: str = "",
        fallback_reasoning_effort: str = "",
    ) -> T | None:
        """Return a validated ``schema`` instance, or ``None`` on failure.

        Honors ``LLM_STRUCTURED_MODE``: ``native_parse`` first (OpenAI structured outputs),
        otherwise/falling back to ``json_object`` with schema instructions appended.
        If both formats fail for the primary model, try its configured model fallback.
        For destructive replacement calls, ``required_fields`` must be explicitly
        supplied by the model; Pydantic defaults must not turn partial output into a wipe.
        """
        inputs = {"system_prompt": messages[0]["content"], "messages": messages}
        start = time.perf_counter()

        # json_object path (default; works with Gemini/local proxies).
        schema_definition = schema.model_json_schema()
        if required_fields:
            schema_definition["required"] = sorted(
                set(schema_definition.get("required", [])) | set(required_fields)
            )
        schema_json = json.dumps(schema_definition, indent=2)
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

        async def make_call(model: str, reasoning_effort: str) -> tuple[T, str | None]:
            model_kwargs = self._model_kwargs(model, reasoning_effort)
            if config.LLM_STRUCTURED_MODE == "native_parse":
                try:
                    completion = await self._with_retries(
                        lambda: self.client.beta.chat.completions.parse(
                            **model_kwargs,
                            messages=messages,
                            response_format=schema,
                            timeout=timeout,
                        ),
                        what=f"{call_type} parse u{user_id} model={model}",
                    )
                    parsed = completion.choices[0].message.parsed
                    if parsed is not None:
                        missing = set(required_fields) - parsed.model_fields_set
                        if missing:
                            raise ValueError(
                                f"Missing replacement fields: {', '.join(sorted(missing))}"
                            )
                        return parsed, completion.choices[0].message.content
                    logger.warning(
                        f"{call_type}: model {model} native parse returned None; "
                        "falling back to json_object."
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"{call_type}: model {model} native parse failed ({exc}); "
                        "falling back to json_object."
                    )

            response = await self._with_retries(
                lambda: self.client.chat.completions.create(
                    **model_kwargs,
                    messages=json_messages,
                    response_format={"type": "json_object"},
                    timeout=timeout,
                ),
                what=f"{call_type} json u{user_id} model={model}",
            )
            raw = response.choices[0].message.content
            cleaned = self._strip_fences(raw)
            if required_fields:
                payload = json.loads(cleaned)
                if not isinstance(payload, dict):
                    raise ValueError("Replacement output must be a JSON object")
                missing = set(required_fields) - payload.keys()
                if missing:
                    raise ValueError(
                        f"Missing replacement fields: {', '.join(sorted(missing))}"
                    )
            parsed = schema.model_validate_json(cleaned)
            return parsed, raw

        try:
            parsed, raw = await self._with_model_fallback(
                make_call,
                model=model,
                reasoning_effort=reasoning_effort,
                fallback_model=fallback_model,
                fallback_reasoning_effort=fallback_reasoning_effort,
                what=f"{call_type} u{user_id}",
            )
            self._fire_log(
                user_id,
                call_type,
                inputs,
                {"raw_text": raw, "parsed_json": parsed.model_dump()},
                "success",
            )
            metrics.record_llm(call_type, ok=True, latency=time.perf_counter() - start)
            return parsed
        except Exception as e:  # noqa: BLE001
            metrics.record_llm(call_type, ok=False, latency=time.perf_counter() - start)
            self._fire_log(
                user_id,
                call_type,
                inputs,
                status="failed",
                error=traceback.format_exc(),
            )
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
        model = config.extraction_model
        return await self._structured_call(
            user_id=user_id,
            call_type="memory_extraction",
            model=model,
            messages=messages,
            schema=MemoryExtraction,
            timeout=config.LLM_REQUEST_TIMEOUT_SECS,
            reasoning_effort=config.LLM_EXTRACTION_REASONING_EFFORT,
            fallback_model=config.LLM_EXTRACTION_FALLBACK_MODEL,
            fallback_reasoning_effort=config.LLM_EXTRACTION_FALLBACK_REASONING_EFFORT,
        )

    async def extract_group_memory(
        self, user_id_or_chat_id: int, system_prompt: str, user_history_text: str
    ) -> GroupMemoryExtraction | None:
        """Multi-party memory extraction over a rendered group segment (one LLM call).

        Mirrors :meth:`extract_memory` exactly — same model selection, retry/json_object/
        native_parse handling via ``_structured_call``, same request timeout, and the same ``None``-on-failure contract — but validates against
        :class:`GroupMemoryExtraction` so the result carries per-participant, name-tagged
        updates. ``user_history_text`` is the multi-party segment rendered as
        JSON entries with role, sender ID/name, timestamp, and content. The first
        argument is used only for audit logging
        (a group chat_id here, a user_id in the DM path); it does not affect attribution,
        which the caller resolves from the segment's own name->id map.

        Returns ``None`` when the call fails (transient errors exhausted, or unparseable
        output) so the caller can retry; a valid — possibly *empty* — result means success.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_history_text},
        ]
        model = config.extraction_model
        return await self._structured_call(
            user_id=user_id_or_chat_id,
            call_type="group_memory_extraction",
            model=model,
            messages=messages,
            schema=GroupMemoryExtraction,
            timeout=config.LLM_REQUEST_TIMEOUT_SECS,
            reasoning_effort=config.LLM_EXTRACTION_REASONING_EFFORT,
            fallback_model=config.LLM_EXTRACTION_FALLBACK_MODEL,
            fallback_reasoning_effort=config.LLM_EXTRACTION_FALLBACK_REASONING_EFFORT,
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
        model = config.extraction_model
        return await self._structured_call(
            user_id=user_id,
            call_type="memory_compression",
            model=model,
            messages=messages,
            schema=MemoryCompression,
            timeout=config.LLM_REQUEST_TIMEOUT_SECS,
            reasoning_effort=config.LLM_EXTRACTION_REASONING_EFFORT,
            fallback_model=config.LLM_EXTRACTION_FALLBACK_MODEL,
            fallback_reasoning_effort=config.LLM_EXTRACTION_FALLBACK_REASONING_EFFORT,
            required_fields=(
                "compressed_facts",
                "compressed_beliefs",
                "compressed_events",
            ),
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
        model = config.extraction_model
        return await self._structured_call(
            user_id=user_id,
            call_type="memory_consolidation",
            model=model,
            messages=messages,
            schema=MemoryConsolidation,
            timeout=config.LLM_REQUEST_TIMEOUT_SECS,
            reasoning_effort=config.LLM_EXTRACTION_REASONING_EFFORT,
            fallback_model=config.LLM_EXTRACTION_FALLBACK_MODEL,
            fallback_reasoning_effort=config.LLM_EXTRACTION_FALLBACK_REASONING_EFFORT,
            required_fields=(
                "consolidated_facts",
                "consolidated_beliefs",
                "consolidated_events",
                "insights",
            ),
        )


# Shared singleton — one client (and connection pool) for the whole process.
llm_service = LLMService()
