# LLM Integration, Schemas & Centralized Audit Trail

This document describes how ThinkMate integrates with LLM backends, leveraging **Pydantic** structures for OpenAI Structured Outputs, and provides a detailed look at the centralized MongoDB-backed audit log architecture.

---

## ⚡ Structured Outputs with Pydantic

Structured outputs guarantee that response payloads conform strictly to Pydantic schemas. By utilizing OpenAI's `beta.chat.completions.parse` method, ThinkMate guarantees that inputs and outputs validate automatically. For local/custom engines (e.g. Ollama, Llama.cpp, LM Studio) that do not support the native parse API, ThinkMate falls back to standard JSON mode and validates the output manually against the Pydantic schemas.

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

---

## 🔎 Centralized Database Audit Trail (`llm_audit_log`)

All three LLM calls (`chat_reply`, `memory_extraction`, and `memory_compression`) execute inside tracing blocks in `LLMService`. This guarantees a complete record of prompt inputs, assistant outputs, raw completions, structured parsed schemas, status strings, and execution errors.

### Logging Mechanism inside `LLMService`
The client executes database writes asynchronously on the `llm_audit_log` collection:

```python
async def _log_llm_call(
    self,
    user_id: int,
    call_type: str,
    inputs: dict,
    outputs: dict | None = None,
    status: str = "success",
    error: str | None = None
):
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
```

Every response generation, extraction, and compression task invokes this tracer. For example, during generation:
* **Success**: Records `status = "success"`, raw completion text, and `error = None`.
* **Failure**: Catches exceptions, captures the complete traceback string, logs `status = "failed"` with the error, and re-raises the exception.

---

## ✍️ Prompt Engineering Templates (`app/prompts/`)

1.  **Extraction Prompt (`extraction_prompt.py`)**: Directs the LLM to process a conversation segment, identifying objective facts, subjective beliefs, and timeline milestones, mapping updates relative to the user's current memory card.
2.  **Compression Prompt (`compression_prompt.py`)**: Instructs the LLM to summarize and trim user details holistically when the memory size breaches the character budget limit (4,000 characters).
3.  **System Prompt Builder (`system_prompt.py`)**: Assembles the agent's core identity guidelines, the active compiled memory block, and communication constraints dynamically.
