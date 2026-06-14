from pydantic import BaseModel, Field
from typing import Literal, Optional

# --- CONVERSATIONAL OUTPUT SCHEMA ---
class ReplyBundle(BaseModel):
    """Combined output of a single chat call: the reply plus an optional emoji reaction."""
    reply: str = Field(description="The natural, conversational reply text to send to the user.")
    reaction: Optional[str] = Field(
        None,
        description="A single Telegram emoji reaction for the user's message, or null if none fits.",
    )
    affinity_delta: Optional[float] = Field(
        None,
        description="Groups only: a small signed adjustment to the speaker's affinity (e.g. +0.05/-0.05). Ignored/None in DMs.",
    )

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

class FactRemoval(BaseModel):
    content: str = Field(description="The exact text content of the fact to remove from memory.")

# --- BELIEF SCHEMAS ---
class BeliefExtract(BaseModel):
    content: str = Field(description="The user's subjective opinion, value, or belief statement, e.g. 'Believes that family time is more important than career.'")

class BeliefUpdate(BaseModel):
    old_content: str = Field(description="The exact content of the outdated belief currently in memory.")
    new_content: str = Field(description="The replacement belief statement.")

class BeliefRemoval(BaseModel):
    content: str = Field(description="The exact content of the belief to remove from memory.")

# --- EVENT SCHEMAS ---
class EventExtract(BaseModel):
    description: str = Field(description="Short summary of the event.")
    date: Optional[str] = Field(None, description="ISO date (YYYY-MM-DD) or string representation ('last week').")
    significance: Literal["major", "minor", "routine"]
    emotion: Optional[str] = Field(None, description="Dominant emotion linked to this event.")

class EventUpdate(BaseModel):
    old_description: str = Field(description="The exact description text of the event to update.")
    new_description: str = Field(description="The replacement description.")
    date: Optional[str] = Field(None, description="Updated date, or None to keep the original.")
    significance: Optional[Literal["major", "minor", "routine"]] = Field(None, description="Updated significance, or None to keep the original.")

class EventRemoval(BaseModel):
    description: str = Field(description="The exact description text of the event to remove.")

# --- EMOTION SCHEMA ---
class EmotionLog(BaseModel):
    mood: str = Field(description="Single-word tag representing user's current mood, e.g., 'excited'.")
    intensity: float = Field(0.5, description="Intensity score from 0.0 to 1.0.")
    trigger: Optional[str] = Field(None, description="What triggered this mood shift.")

# --- PROFILE SCHEMAS ---
class ProfileUpdate(BaseModel):
    communication_style: Optional[str] = Field(None, description="Updates to the communication style profile.")
    gender: Optional[Literal["male", "female", "non-binary"]] = Field(
        None,
        description=(
            "The user's gender, inferred from the conversation (self-identification, "
            "gendered terms they use about themselves, pronouns, or grammatical gender in "
            "gendered languages). Set ONLY when reasonably confident; leave null when unknown "
            "or ambiguous. Do not guess from name alone."
        ),
    )

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

# --- MULTI-PARTY (GROUP) EXTRACTION SCHEMAS ---
class GroupMemoryUpdate(BaseModel):
    participant: str = Field(description="The participant's sender_name exactly as it appears in the group segment.")
    extraction: MemoryExtraction = Field(description="The memory extraction attributed to this participant.")

class GroupMemoryExtraction(BaseModel):
    updates: list[GroupMemoryUpdate] = Field(default_factory=list)

# --- COMPRESSION SCHEMAS ---
class CompressedFact(BaseModel):
    category: Literal["personal", "work", "preference", "health", "hobby", "relationship"]
    content: str

class CompressedBelief(BaseModel):
    content: str

class CompressedEvent(BaseModel):
    description: str
    date: Optional[str] = None
    significance: Literal["major", "minor"]

class MemoryCompression(BaseModel):
    profile_summary: Optional[str] = Field(None, description="Updated high-level profile summary of the user.")
    communication_style: Optional[str] = Field(None, description="Updated communication preferences.")
    compressed_facts: list[CompressedFact] = Field(default_factory=list)
    compressed_beliefs: list[CompressedBelief] = Field(default_factory=list)
    compressed_events: list[CompressedEvent] = Field(default_factory=list)
    emotional_state: Optional[EmotionLog] = None

# --- CONSOLIDATION SCHEMAS ---
class ConsolidatedInsight(BaseModel):
    content: str = Field(description="A durable, higher-level behavioral/identity observation synthesized across the user's whole history.")

class MemoryConsolidation(BaseModel):
    profile_summary: Optional[str] = Field(None, description="Refreshed high-level profile summary of the user.")
    communication_style: Optional[str] = Field(None, description="Refreshed communication preferences.")
    consolidated_facts: list[CompressedFact] = Field(default_factory=list)
    consolidated_beliefs: list[CompressedBelief] = Field(default_factory=list)
    consolidated_events: list[CompressedEvent] = Field(default_factory=list)
    insights: list[ConsolidatedInsight] = Field(default_factory=list)
    emotional_state: Optional[EmotionLog] = None
