"""Tests for the consolidation schema and the LLMService.consolidate_memory call.

Covers Task 2.4 (Requirements 7.1, 7.2, 9.1):
- ``MemoryConsolidation`` validates a representative payload and defaults empty lists.
- ``consolidate_memory`` returns the parsed ``MemoryConsolidation`` on success and
  ``None`` on failure (the never-wipe sentinel), passing ``call_type="memory_consolidation"``
  and ``schema=MemoryConsolidation`` through to ``_structured_call``.

Follows tests/conftest.py conventions (mongomock + pytest-asyncio); the structured LLM
call is patched with ``AsyncMock`` (mirroring tests/test_reactions.py).
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.llm_service import LLMService, llm_service
from app.services.schemas import MemoryConsolidation, ConsolidatedInsight


# --- Representative payload ----------------------------------------------- #
def _representative_payload() -> dict:
    return {
        "profile_summary": "A thoughtful engineer who values steady, calm progress.",
        "communication_style": "Direct but warm; prefers concise answers.",
        "consolidated_facts": [
            {"category": "personal", "content": "Lives in Berlin."},
        ],
        "consolidated_beliefs": [
            {"content": "Believes consistency beats intensity."},
        ],
        "consolidated_events": [
            {"description": "Started a new job.", "significance": "major"},
        ],
        "insights": [
            {"content": "Tends to get stressed near deadlines; values reassurance then."},
        ],
        "emotional_state": {"mood": "calm", "intensity": 0.5},
    }


# --- Schema validation (Requirement 7.1) ---------------------------------- #
def test_memory_consolidation_parses_representative_payload():
    data = _representative_payload()
    model = MemoryConsolidation(**data)

    assert model.profile_summary == data["profile_summary"]
    assert model.communication_style == data["communication_style"]

    assert len(model.consolidated_facts) == 1
    assert model.consolidated_facts[0].category == "personal"
    assert model.consolidated_facts[0].content == "Lives in Berlin."

    assert len(model.consolidated_beliefs) == 1
    assert model.consolidated_beliefs[0].content == "Believes consistency beats intensity."

    assert len(model.consolidated_events) == 1
    assert model.consolidated_events[0].description == "Started a new job."
    assert model.consolidated_events[0].significance == "major"

    assert len(model.insights) == 1
    assert isinstance(model.insights[0], ConsolidatedInsight)
    assert model.insights[0].content.startswith("Tends to get stressed")

    assert model.emotional_state is not None
    assert model.emotional_state.mood == "calm"
    assert model.emotional_state.intensity == 0.5


def test_memory_consolidation_model_validate_equivalent():
    data = _representative_payload()
    model = MemoryConsolidation.model_validate(data)
    assert model == MemoryConsolidation(**data)


def test_memory_consolidation_empty_defaults_to_empty_lists():
    model = MemoryConsolidation()
    assert model.consolidated_facts == []
    assert model.consolidated_beliefs == []
    assert model.consolidated_events == []
    assert model.insights == []
    assert model.profile_summary is None
    assert model.communication_style is None
    assert model.emotional_state is None


# --- consolidate_memory success (Requirements 7.2, 9.1) ------------------- #
@pytest.mark.asyncio
async def test_consolidate_memory_returns_parsed_result_on_success():
    expected = MemoryConsolidation(**_representative_payload())

    with patch.object(
        LLMService, "_structured_call", new_callable=AsyncMock
    ) as mock_call:
        mock_call.return_value = expected
        result = await llm_service.consolidate_memory(123, "sys", "memtext")

    assert result is expected

    mock_call.assert_called_once()
    kwargs = mock_call.call_args.kwargs
    assert kwargs["call_type"] == "memory_consolidation"
    assert kwargs["schema"] is MemoryConsolidation
    assert kwargs["user_id"] == 123


# --- consolidate_memory failure / never-wipe sentinel (Requirement 7.2) --- #
@pytest.mark.asyncio
async def test_consolidate_memory_returns_none_on_failure():
    with patch.object(
        LLMService, "_structured_call", new_callable=AsyncMock
    ) as mock_call:
        mock_call.return_value = None
        result = await llm_service.consolidate_memory(123, "sys", "memtext")

    assert result is None
    mock_call.assert_called_once()
    assert mock_call.call_args.kwargs["call_type"] == "memory_consolidation"
    assert mock_call.call_args.kwargs["schema"] is MemoryConsolidation
