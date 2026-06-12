# LLM Integration & Structured Outputs (Pydantic + OpenAI SDK)

This document describes how ThinkMate integrates with LLM backends, leveraging **Pydantic** and the latest **OpenAI Structured Outputs** engine to guarantee schema conformity for background memory extractions.

---

## ⚡ Why Structured Outputs with Pydantic?

Historically, parsing JSON from LLM outputs required complex prompt engineering, regex parsing, and fragile fallback systems. By combining OpenAI's `beta.chat.completions.parse` method with **Pydantic v2**, the system gains several advantages:
1.  **Guaranteed Schema**: The model is mathematically constrained at the generation stage to output JSON matching your exact schema.
2.  **No Manual Parsing**: The OpenAI SDK automatically parses the JSON string and returns a fully initialized Pydantic model instance.
3.  **Automatic Fallbacks**: We write a robust wrapper that handles cloud APIs (using native `.parse()`) and local engines (such as Ollama or LM Studio) by validating standard JSON outputs against the same Pydantic models.

---

## 📐 Schema Definitions (`app/services/schemas.py`)

Create Pydantic schema models to represent structured memory modifications:

```python
# app/services/schemas.py
from pydantic import BaseModel, Field
from typing import Literal, Optional

# --- FACT SCHEMAS ---
class FactExtract(BaseModel):
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"] = Field(
        description="The categorised classification of the fact."
    )
    content: str = Field(description="The concrete fact content, e.g., 'Has a Golden Retriever named Bruno.'")

class FactUpdate(BaseModel):
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"]
    old_content: str = Field(description="The exact text content of the outdated fact currently in memory.")
    new_content: str = Field(description="The replacement text content representing the updated state.")

# --- EVENT SCHEMA ---
class EventExtract(BaseModel):
    description: str = Field(description="Short summary of the event.")
    date: Optional[str] = Field(None, description="ISO date (YYYY-MM-DD) or string representation ('last week').")
    significance: Literal["major", "minor", "routine"]
    emotion: Optional[str] = Field(None, description="Dominant emotion linked to this event.")

# --- EMOTION SCHEMA ---
class EmotionLog(BaseModel):
    mood: str = Field(description="Single-word tag representing user's current mood, e.g., 'excited'.")
    intensity: float = Field(0.5, description="Intensity score from 0.0 to 1.0.")
    trigger: Optional[str] = Field(None, description="What triggered this mood shift.")

# --- PROFILE SCHEMAS ---
class ProfileUpdate(BaseModel):
    communication_style: Optional[str] = Field(None, description="Updates to the communication style profile.")

# --- COMPREHENSIVE EXTRACTION SCHEMA ---
class MemoryExtraction(BaseModel):
    profile_updates: Optional[ProfileUpdate] = None
    new_facts: list[FactExtract] = Field(default_factory=list)
    updated_facts: list[FactUpdate] = Field(default_factory=list)
    events: list[EventExtract] = Field(default_factory=list)
    emotional_state: Optional[EmotionLog] = None

# --- CONSOLIDATION SCHEMAS ---
class FactConsolidationUpdate(BaseModel):
    id: int = Field(description="Database ID of the fact to modify.")
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"]
    new_content: str = Field(description="The updated consolidated fact text.")

class MemoryConsolidation(BaseModel):
    deactivate_ids: list[int] = Field(
        default_factory=list, 
        description="List of fact IDs that are redundant, outdated, or contradicted."
    )
    update_records: list[FactConsolidationUpdate] = Field(
        default_factory=list, 
        description="List of updates to modify existing fact contents."
    )

# --- COMPRESSION SCHEMAS ---
class CompressedFact(BaseModel):
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"]
    content: str

class CompressedEvent(BaseModel):
    description: str
    date: Optional[str] = None
    significance: Literal["major", "minor"]

class MemoryCompression(BaseModel):
    profile_summary: Optional[str] = Field(None, description="Updated high-level profile summary of the user.")
    communication_style: Optional[str] = Field(None, description="Updated communication preferences.")
    compressed_facts: list[CompressedFact] = Field(default_factory=list)
    compressed_events: list[CompressedEvent] = Field(default_factory=list)
    emotional_state: Optional[EmotionLog] = None
```

---

## 🔌 Robust LLM Service Client Wrapper (`llm_service.py`)

Here is the implementation of [llm_service.py](file:///d:/ThinkMate/app/services/llm_service.py) that handles structured parsing with local fallback functionality:

```python
# app/services/llm_service.py
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
        Falls back to JSON mode + manual Pydantic validation if using a local LLM engine.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_history_text}
        ]
        model_name = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        
        try:
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
        except Exception as e:
            logger.warning(f"Native parse failed: {e}. Falling back to standard JSON mode...")

        schema_json = json.dumps(MemoryExtraction.model_json_schema(), indent=2)
        fallback_system_prompt = (
            f"{system_prompt}\n\n"
            f"IMPORTANT: You MUST respond with a valid JSON object matching this JSON schema:\n"
            f"```json\n{schema_json}\n```\n"
        )
        
        try:
            response = await self.client.chat.completions.create(
                model=model_name,
                messages=[{"role": "system", "content": fallback_system_prompt}, {"role": "user", "content": user_history_text}],
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=45.0
            )
            return MemoryExtraction.model_validate_json(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Fallback extraction failed: {e}")
            return MemoryExtraction()

    async def compress_memory(self, system_prompt: str, raw_memory_text: str) -> MemoryCompression:
        """Compresses memory using Pydantic structured output validation."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_memory_text}
        ]
        model_name = config.LLM_EXTRACTION_MODEL or config.LLM_MODEL
        
        try:
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
        except Exception as e:
            logger.warning(f"Native compression parse failed: {e}. Falling back to JSON mode...")

        schema_json = json.dumps(MemoryCompression.model_json_schema(), indent=2)
        fallback_system_prompt = (
            f"{system_prompt}\n\n"
            f"IMPORTANT: You MUST respond with a valid JSON object matching this JSON schema:\n"
            f"```json\n{schema_json}\n```\n"
        )
        
        try:
            response = await self.client.chat.completions.create(
                model=model_name,
                messages=[{"role": "system", "content": fallback_system_prompt}, {"role": "user", "content": raw_memory_text}],
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=60.0
            )
            return MemoryCompression.model_validate_json(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Fallback compression failed: {e}")
            return MemoryCompression()
```

---

## ✍️ Prompt Engineering Templates (`app/prompts/`)

### 1. Extraction System Prompt (`extraction_prompt.py`)

No longer needs detailed schema descriptions since the model output structure is enforced programmatically. Instead, focus the instructions on extraction logic guidelines:

```python
# app/prompts/extraction_prompt.py

SYSTEM_EXTRACTION_PROMPT = """You are a memory processor. Your task is to analyze the provided conversation log and extract key updates about the user.

You will receive:
1. The user's CURRENT memories (what is already saved).
2. The recent conversation history segment.

GUIDELINES:
- Extract clear, atomic facts (e.g., "Enjoys green tea", "Has a younger brother named Sid").
- If the user contradicts a current memory (e.g., they mention moving to a new city), put the old entry in "updated_facts.old_content" and the new entry in "updated_facts.new_content".
- Extract notable life events for the chronological timeline (e.g., job change, buying a house).
- Identify shifts in mood or emotional state, assigning an intensity score between 0.0 and 1.0.
- Do not extract details that are already present in CURRENT memories.
- Return output strictly matching the expected JSON schema.
"""
```

---

## 🛡️ Robustness Checklist
*   **Schema Enforcement**: Cloud endpoints guarantee schema matching by failing requests before generation if the schema is invalid.
*   **Graceful Parsing Fallback**: If using a local LLM that outputs malformed JSON, Pydantic's `ValidationError` handles failures safely by falling back to empty datasets, preventing crashes.
*   **Timeouts & Retries**: Ensure LLM service operations have active timeout boundaries (e.g., `timeout=15.0`) to prevent blocking operations indefinitely if APIs fail.
