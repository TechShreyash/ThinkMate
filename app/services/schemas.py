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
