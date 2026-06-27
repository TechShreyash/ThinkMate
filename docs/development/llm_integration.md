# LLM Integration, Schemas & Centralized Audit Trail

This document describes how ThinkMate integrates with LLM backends, the structured-output
strategy, the single combined reply call, retry behavior, and the MongoDB-backed audit log.
In other words, it covers the **LLM layer** — the seam between the bot and whichever language
model provider it talks to, plus everything that keeps those calls reliable, observable, and
provider-agnostic.

All LLM access goes through one shared `LLMService` instance (`app/services/llm_service.py`,
exported as `llm_service`) so the whole process reuses a single client and connection pool.
Routing every call through one service is what makes the patterns below — uniform retries,
provider-portable JSON, and a single audit trail — possible to enforce in one place.

A few terms used throughout this guide:

* **Structured output** — an LLM response that must conform to a fixed JSON shape so the code
  can parse it deterministically, rather than free-form prose.
* **Audit trail** — a stored, after-the-fact record of every LLM call (its inputs, outputs, and
  status) used for debugging and observability, kept in the `llm_audit_log` MongoDB collection.
* **Hot path** — the latency-sensitive code that runs while a user is waiting for a reply; work
  that would slow it down is deferred to the background.

What's in this doc:

* **Structured outputs** — how JSON is coaxed out of the model and validated, and why
  `json_object` is the default.
* **The single combined reply call** — how one call produces both the conversational reply and
  the optional emoji reaction (and the group-chat affinity signal).
* **Schema definitions** — the Pydantic models for memory extraction, compression, and the
  group-chat additions.
* **The centralized audit trail** — how every LLM call is traced without slowing the user down.
* **Prompt engineering templates** — the prompt builders under `app/prompts/`.

---

## ⚡ Structured Outputs: `json_object` by default

Every structured call is validated against a **Pydantic** schema — Pydantic being the Python
library that parses and type-checks JSON into typed model objects, rejecting anything malformed.
The *mechanism* used to get JSON from the model is controlled by `LLM_STRUCTURED_MODE`:

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
like `400` are not retried, because a malformed request will fail the same way on every attempt.

`_with_retries` is the **only** retry layer: the `AsyncOpenAI` client is constructed with
`max_retries=0` so the SDK's own (default 2) retries don't stack a second, hidden layer on top
of ours. Two independent layers would each retry timeouts and multiply concurrent generations
on the backend.

### Request timeout and the proxy-ordering rule
Every call passes a per-request timeout sourced from a single setting,
`LLM_REQUEST_TIMEOUT_SECS` (default `610`). It is deliberately generous — reasoning ("thinking")
models can legitimately run for minutes, and a short ceiling would abort healthy in-flight
generations.

The critical rule: **keep the client timeout ABOVE the proxy/server's own request ceiling.**
The two timeouts are ordered, and the order decides what a timeout *means*:

- **Client < proxy** (the old `30/45/60s` vs a `180s` proxy): the client always gives up first
  while the proxy keeps generating. Every slow-but-healthy request looks like a failure, gets
  retried, and the retry spawns a *fresh* upstream generation while the abandoned one is still
  running — duplicate work that compounds under load.
- **Client > proxy** (today's `610s` vs the proxy's `600s` non-streaming cap): the proxy is
  always the first to give up and returns a definitive error. The client never abandons work
  that's still in flight, so a client-side timeout now only fires when something is genuinely
  hung past the proxy's ceiling.

If you change the proxy's `REQUEST_TIMEOUT`, raise `LLM_REQUEST_TIMEOUT_SECS` to stay above it.
(ThinkMate's calls are all non-streaming, so the proxy's non-streaming cap is the one that
matters; streaming idle-timeouts don't apply here.)

---

## 💬 Single combined reply call (`generate_reply_bundle`)

The conversational reply and the optional Telegram emoji reaction are produced in **one**
LLM call (not two). Folding both into a single call halves the per-message LLM cost and latency.
`generate_reply_bundle` requests a strict JSON object:

```json
{"reply": "<natural conversational message>", "reaction": "<single emoji or empty>"}
```

The `reply` and the `reaction` are **independent choices** the model makes in the same call:
the `reply` is plain conversational text (emojis within it are fine, per the persona), while
the `reaction` is a separate Telegram reaction applied to the *user's* message. The reaction is
normalized against Telegram's accepted emoji set (`app/services/reactions.py`, tolerant of
variation selectors like `❤️` → `❤`) and dropped if invalid or if `ENABLE_MESSAGE_REACTIONS=False`.
If the model returns non-JSON, the raw text is used as the reply and no reaction is sent — the
user always gets an answer. If that raw fallback is itself empty/blank, the batch processor
substitutes a short graceful line so Telegram never rejects an empty send (see
[telegram_bot.md](telegram_bot.md)).

> **No `max_tokens` cap.** The LLM request does not derive a token ceiling from
> `MAX_RESPONSE_CHARS / CHARS_PER_TOKEN`; doing that risked cutting the JSON envelope off
> mid-string (invalid JSON → raw-text fallback, and occasionally an empty send). Instead,
> `MAX_RESPONSE_CHARS` is applied after generation as an app-level cap for LLM replies and
> proactive check-ins, then the Telegram sender splits delivery into chunks below Telegram's
> 4096-character message limit. Set `MAX_RESPONSE_CHARS <= 0` to disable the app cap while
> keeping Telegram-safe chunking.

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
* **`GroupMemoryExtraction`** — `{ group_extraction?: MemoryExtraction, updates: list[GroupMemoryUpdate] }`,
  the result of the multi-party extraction call. `group_extraction` is for shared group context;
  `updates` is for participant-specific memory.

`extract_group_memory(chat_id_or_user_id, system_prompt, user_history_text)` mirrors
`extract_memory` exactly — same model selection, retry / `json_object` / `native_parse` handling,
temperature, timeout, and `None`-on-failure contract — but validates against
`GroupMemoryExtraction`. The first argument is also the key for shared group memory on the group
path; participant attribution is resolved by the caller from the segment's own name→id map (see
[memory_engine.md](memory_engine.md)).

---

## 🔎 Centralized Database Audit Trail (`llm_audit_log`)

The LLM call types — `chat_reply` (reply **and** reaction), `proactive_checkin`,
`memory_extraction`, `group_memory_extraction` (multi-party group extraction),
`memory_compression`, and `memory_consolidation` — are traced in `LLMService`, recording
input/output lengths, status, and error tracebacks.
Centralizing this trace in one service means every call type is logged the same way, with no
per-caller bookkeeping.

Three properties keep this safe at scale:

1. **Off the hot path.** `_fire_log` schedules the write as a background task, so a user's
   reply is never delayed by an audit insert.
2. **TTL-friendly timestamp.** `timestamp` is stored as a real `datetime` (not an ISO string)
   so the TTL index in `init_db` (`AUDIT_LOG_RETENTION_DAYS`) actually expires old entries.
   (TTL — "time to live" — is MongoDB's mechanism for auto-deleting documents after an age.)
3. **Bounded size and better privacy.** Prompts, histories, model completions, and parsed JSON are
   summarized as string lengths before insertion, so a single document can't balloon and routine
   audit rows do not retain conversation text.

```python
def _fire_log(self, *args, **kwargs):
    """Schedule an audit write without blocking the caller (keeps it off the hot path)."""
    task = asyncio.get_running_loop().create_task(self._log_llm_call(*args, **kwargs))
    self._bg_tasks.add(task)
    task.add_done_callback(self._bg_tasks.discard)
```

* **Success**: `status = "success"`, input/output length metadata, `error = None`.
* **Failure**: captures the traceback, logs `status = "failed"`. Chat replies re-raise (the
  batch processor sends a friendly fallback message); **`extract_memory` and `compress_memory`
  return `None`** so the caller can tell a failed call from a legitimately empty one. The
  extractor retries on `None` (see [memory_engine.md](memory_engine.md)); the compressor
  **skips the replace** on `None`, so a failed compression never wipes a user's memory.

> The emoji reaction is part of the `chat_reply` call — there is no separate reaction LLM call.

---


## ✍️ Prompt Engineering Templates (`app/prompts/`)

Each prompt builder lives in its own module under `app/prompts/` and assembles the text sent to
the model for a specific job:

1.  **Extraction Prompt (`extraction_prompt.py`)**: Directs the LLM to process a conversation segment, identifying objective facts, subjective beliefs, and timeline milestones, mapping updates relative to the user's current memory card.
2.  **Compression Prompt (`compression_prompt.py`)**: Instructs the LLM to summarize and trim user details holistically when the memory size breaches the character budget limit (4,000 characters).
3.  **System Prompt Builder (`system_prompt.py`)**: Assembles the agent's core identity guidelines, the active compiled memory block, and communication constraints dynamically.
