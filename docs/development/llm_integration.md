# LLM Integration, Schemas & Centralized Audit Trail

This document describes how ThinkMate integrates with LLM backends, the structured-output
strategy, the single combined reply call, retry behavior, and the MongoDB-backed audit log.

All LLM access goes through one shared `LLMService` instance (`app/services/llm_service.py`,
exported as `llm_service`) so the whole process reuses a single client and connection pool.

---

## ⚡ Structured Outputs: `json_object` by default

Every structured call is validated against a **Pydantic** schema. The *mechanism* used to
get JSON from the model is controlled by `LLM_STRUCTURED_MODE`:

* **`json_object` (default)** — sends `response_format={"type": "json_object"}` with the
  schema appended to the system prompt, then validates the result with Pydantic. This is
  the only mode that works across Gemini proxies, Ollama, LM Studio, and OpenRouter.
* **`native_parse`** — uses OpenAI's `beta.chat.completions.parse`. Use this **only** on a
  genuine OpenAI endpoint.

> ⚠️ **Why not native parse everywhere?** The OpenAI SDK injects `additionalProperties: false`
> into the JSON schema. Google's Gemini backend rejects that field with a `400 INVALID_ARGUMENT`,
> so native parsing fails on Gemini-compatible proxies. Defaulting to `json_object` avoids a
> guaranteed failed round-trip on every extraction/compression. (Verified live, 2026-06-13 —
> see `docs/development/hardening_plan.md`.)

### Transient-error retries
`_with_retries` wraps API calls and retries transient failures (timeout, connection, 429,
5xx) with exponential backoff (`LLM_MAX_RETRIES`, `LLM_RETRY_BASE_DELAY_SECS`). Client errors
like `400` are not retried.

---

## 💬 Single combined reply call (`generate_reply_bundle`)

The conversational reply and the optional Telegram emoji reaction are produced in **one**
LLM call (not two). `generate_reply_bundle` requests a strict JSON object:

```json
{"reply": "<natural conversational message>", "reaction": "<single emoji or empty>"}
```

The `reply` and the `reaction` are **independent choices** the model makes in the same call:
the `reply` is plain conversational text (emojis within it are fine, per the persona), while
the `reaction` is a separate Telegram reaction applied to the *user's* message. The reaction is
normalized against Telegram's accepted emoji set (`app/services/reactions.py`, tolerant of
variation selectors like `❤️` → `❤`) and dropped if invalid or if `ENABLE_MESSAGE_REACTIONS=False`.
If the model returns non-JSON, the raw text is used as the reply and no reaction is sent — the
user always gets an answer.

> **Group chats (Phase 9, implemented):** calling `generate_reply_bundle(..., with_affinity=True)`
> (the group path) switches the return contract to `(reply, reaction, affinity_delta)` and asks the
> model for an optional `affinity_delta` number in `[-0.2, 0.2]` inside the same reply JSON — so the
> bot's read on the relationship rides along at no extra LLM cost. With `with_affinity=False`
> (default, DM/addressed path) the prompt and the `(reply, reaction)` return value are byte-for-byte
> unchanged. `affinity_delta` is `None`/ignored in DMs. See [group_chat.md](group_chat.md).

---

## 📐 Schema Definitions (`app/services/schemas.py`)

The unified schemas define memory extraction and compression objects. We maintain a separation between objective **facts** (concrete details) and subjective **beliefs** (user opinions, values, and thoughts).

```python
# app/services/schemas.py
from pydantic import BaseModel, Field
from typing import Literal, Optional

# --- FACTS ---
class FactExtract(BaseModel):
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"]
    content: str

class FactUpdate(BaseModel):
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"]
    old_content: str
    new_content: str

class FactRemoval(BaseModel):
    content: str

# --- SUBJECTIVE BELIEFS ---
class BeliefExtract(BaseModel):
    content: str  # e.g., "Believes that remote work increases productivity"

class BeliefUpdate(BaseModel):
    old_content: str
    new_content: str

class BeliefRemoval(BaseModel):
    content: str

# --- LIFE EVENTS ---
class EventExtract(BaseModel):
    description: str
    date: Optional[str] = None
    significance: Literal["major", "minor", "routine"]
    emotion: Optional[str] = None

class EventUpdate(BaseModel):
    old_description: str
    new_description: str
    date: Optional[str] = None
    significance: Optional[Literal["major", "minor", "routine"]] = None

class EventRemoval(BaseModel):
    description: str

# --- EMOTIONAL STATE ---
class EmotionLog(BaseModel):
    mood: str
    intensity: float = 0.5
    trigger: Optional[str] = None

# --- COMPREHENSIVE EXTRACTION SCHEMA ---
class MemoryExtraction(BaseModel):
    profile_updates: Optional[ProfileUpdate] = None
    new_facts: list[FactExtract] = Field(default_factory=list)
    updated_facts: list[FactUpdate] = Field(default_factory=list)
    removed_facts: list[FactRemoval] = Field(default_factory=list)
    new_beliefs: list[BeliefExtract] = Field(default_factory=list)
    updated_beliefs: list[BeliefUpdate] = Field(default_factory=list)
    removed_beliefs: list[BeliefRemoval] = Field(default_factory=list)
    new_events: list[EventExtract] = Field(default_factory=list)
    updated_events: list[EventUpdate] = Field(default_factory=list)
    removed_events: list[EventRemoval] = Field(default_factory=list)
    emotional_state: Optional[EmotionLog] = None

# --- COMPRESSION SCHEMA ---
class MemoryCompression(BaseModel):
    profile_summary: Optional[str] = None
    communication_style: Optional[str] = None
    compressed_facts: list[CompressedFact] = Field(default_factory=list)
    compressed_beliefs: list[CompressedBelief] = Field(default_factory=list)
    compressed_events: list[CompressedEvent] = Field(default_factory=list)
    emotional_state: Optional[EmotionLog] = None
```

### Group-chat schemas *(Phase 9, implemented)*

Three additions support group chat without disturbing the DM contract:

* **`ReplyBundle.affinity_delta`** — an optional signed `float` on the combined reply schema,
  populated only on the group path (`with_affinity=True`) and ignored in DMs.
* **`GroupMemoryUpdate`** — pairs a `participant` name (exactly as rendered in the group segment)
  with a standard `MemoryExtraction` attributed to that person.
* **`GroupMemoryExtraction`** — `{ updates: list[GroupMemoryUpdate] }`, the result of the
  multi-party extraction call.

`extract_group_memory(chat_id_or_user_id, system_prompt, user_history_text)` mirrors
`extract_memory` exactly — same model selection, retry / `json_object` / `native_parse` handling,
temperature, timeout, and `None`-on-failure contract — but validates against
`GroupMemoryExtraction`. Its first argument is used only for audit logging; attribution is resolved
by the caller from the segment's own name→id map (see [memory_engine.md](memory_engine.md)).

---

## 🔎 Centralized Database Audit Trail (`llm_audit_log`)

The four LLM call types — `chat_reply` (reply **and** reaction), `memory_extraction`,
`group_memory_extraction` (multi-party group extraction), and `memory_compression` — are traced
in `LLMService`, recording prompt inputs, outputs, parsed JSON, status, and error tracebacks.

Three properties keep this safe at scale:

1. **Off the hot path.** `_fire_log` schedules the write as a background task, so a user's
   reply is never delayed by an audit insert.
2. **TTL-friendly timestamp.** `timestamp` is stored as a real `datetime` (not an ISO string)
   so the TTL index in `init_db` (`AUDIT_LOG_RETENTION_DAYS`) actually expires old entries.
3. **Bounded size.** Long strings (prompts, histories) are truncated via `_truncate` before
   insertion, so a single document can't balloon.

```python
def _fire_log(self, *args, **kwargs):
    """Schedule an audit write without blocking the caller (keeps it off the hot path)."""
    task = asyncio.get_running_loop().create_task(self._log_llm_call(*args, **kwargs))
    self._bg_tasks.add(task)
    task.add_done_callback(self._bg_tasks.discard)
```

* **Success**: `status = "success"`, raw completion text, parsed JSON, `error = None`.
* **Failure**: captures the traceback, logs `status = "failed"`. Chat replies re-raise (the
  batch processor sends a friendly fallback message); **`extract_memory` and `compress_memory`
  return `None`** so the caller can tell a failed call from a legitimately empty one. The
  extractor retries on `None` (see [memory_engine.md](memory_engine.md)); the compressor
  **skips the replace** on `None`, so a failed compression never wipes a user's memory.

> The emoji reaction is part of the `chat_reply` call — there is no separate reaction LLM call.

---


## ✍️ Prompt Engineering Templates (`app/prompts/`)

1.  **Extraction Prompt (`extraction_prompt.py`)**: Directs the LLM to process a conversation segment, identifying objective facts, subjective beliefs, and timeline milestones, mapping updates relative to the user's current memory card.
2.  **Compression Prompt (`compression_prompt.py`)**: Instructs the LLM to summarize and trim user details holistically when the memory size breaches the character budget limit (4,000 characters).
3.  **System Prompt Builder (`system_prompt.py`)**: Assembles the agent's core identity guidelines, the active compiled memory block, and communication constraints dynamically.
