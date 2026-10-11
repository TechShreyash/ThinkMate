"""Memory prompt guardrails and replacement-response safety at the real SDK boundary."""

import json

import httpx2
import pytest
from openai import AsyncOpenAI

from app.config import config
from app.database import connection, models
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT
from app.prompts.consolidation_prompt import SYSTEM_CONSOLIDATION_PROMPT
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT
from app.services import llm_service as llm_module
from app.services import memory_compressor, memory_consolidator
from app.services.schemas import (
    MemoryCompression,
    MemoryConsolidation,
    MemoryExtraction,
)

REPLACEMENT_FIELDS = {
    "compress_memory": ("compressed_facts", "compressed_beliefs", "compressed_events"),
    "consolidate_memory": (
        "consolidated_facts",
        "consolidated_beliefs",
        "consolidated_events",
        "insights",
    ),
}


def _normalized(prompt):
    return " ".join(prompt.lower().split())


@pytest.mark.parametrize(
    "prompt",
    [SYSTEM_EXTRACTION_PROMPT, SYSTEM_COMPRESSION_PROMPT, SYSTEM_CONSOLIDATION_PROMPT],
)
def test_memory_prompts_treat_source_content_as_data(prompt):
    body = _normalized(prompt)
    assert "data, not instructions" in body
    assert "invent" in body
    assert "uncertainty" in body
    assert "proper nouns" in body and "original form" in body


def test_extraction_preserves_attribution_and_exact_existing_keys():
    body = _normalized(SYSTEM_EXTRACTION_PROMPT)
    assert "assistant messages" in body and "not evidence about the user" in body
    assert "quoted text" in body and "not autobiographical evidence" in body
    assert "copy old_content, old_description" in body and "verbatim" in body
    assert "not in english" in body
    assert "leave update/removal lists empty" in body
    assert "got a job in पुणे" in body


def test_extraction_does_not_fabricate_date_precision():
    body = _normalized(SYSTEM_EXTRACTION_PROMPT)
    assert "not the extraction run's current date" in body
    assert '"last week"' in body and "do not invent one" in body
    assert "use null" in body
    assert "date=null and significance=null mean keep the existing values" in body


def test_consolidation_requires_independent_evidence_for_insights():
    body = _normalized(SYSTEM_CONSOLIDATION_PROMPT)
    assert "multiple distinct supporting observations" in body
    assert "not its own evidence" in body
    assert "counterexamples and corrections" in body
    assert "empty insights array is valid" in body
    assert "medical/mental-health diagnoses" in body


def _completion(content):
    return {
        "id": "chatcmpl-memory-test",
        "object": "chat.completion",
        "created": 0,
        "model": "memory-test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "refusal": None},
            }
        ],
    }


@pytest.fixture
async def replacement_service(monkeypatch):
    """Use the actual SDK and parser, with HTTP transport replaced by local responses."""
    clients = []
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://llm.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "memory-test-placeholder")
    monkeypatch.setattr(config, "LLM_MODEL", "memory-test-model")
    monkeypatch.setattr(config, "LLM_EXTRACTION_MODEL", "")
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)
    monkeypatch.setattr(llm_module.metrics, "record_llm", lambda *args, **kwargs: None)

    def make_service(payloads):
        remaining = iter(payloads)
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=_completion(json.dumps(next(remaining))))

        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        monkeypatch.setattr(
            llm_module,
            "AsyncOpenAI",
            lambda **kwargs: AsyncOpenAI(http_client=http_client, **kwargs),
        )
        service = llm_module.LLMService()
        monkeypatch.setattr(service, "_fire_log", lambda *args, **kwargs: None)
        clients.append(service.client)
        return service, requests

    yield make_service

    for client in clients:
        await client.close()


MISSING_ARRAY_CASES = [
    (method, field) for method, fields in REPLACEMENT_FIELDS.items() for field in fields
]


@pytest.mark.parametrize("mode", ["json_object", "native_parse"])
@pytest.mark.parametrize("method", REPLACEMENT_FIELDS)
async def test_empty_or_summary_only_replacement_is_rejected(
    replacement_service,
    monkeypatch,
    mode,
    method,
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", mode)
    for payload in ({}, {"profile_summary": "Updated summary"}):
        service, requests = replacement_service([payload, payload])
        result = await getattr(service, method)(123, "system", "Existing memory")
        assert result is None
        assert len(requests) == (2 if mode == "native_parse" else 1)


@pytest.mark.parametrize("mode", ["json_object", "native_parse"])
@pytest.mark.parametrize(("method", "omitted"), MISSING_ARRAY_CASES)
async def test_each_omitted_replacement_array_is_rejected(
    replacement_service,
    monkeypatch,
    mode,
    method,
    omitted,
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", mode)
    payload = {key: [] for key in REPLACEMENT_FIELDS[method] if key != omitted}
    service, requests = replacement_service([payload, payload])

    assert await getattr(service, method)(123, "system", "Existing memory") is None
    assert len(requests) == (2 if mode == "native_parse" else 1)


@pytest.mark.parametrize("mode", ["json_object", "native_parse"])
@pytest.mark.parametrize("method", REPLACEMENT_FIELDS)
async def test_explicit_complete_empty_replacement_remains_valid(
    replacement_service,
    monkeypatch,
    mode,
    method,
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", mode)
    payload = {key: [] for key in REPLACEMENT_FIELDS[method]}
    service, requests = replacement_service([payload])

    result = await getattr(service, method)(123, "system", "No retained memory")
    assert result is not None
    assert result.model_fields_set.issuperset(REPLACEMENT_FIELDS[method])
    assert len(requests) == 1
    if mode == "json_object":
        system_text = requests[0]["messages"][0]["content"]
        schema_text = system_text.split("```json\n", 1)[1].split("```", 1)[0]
        advertised_schema = json.loads(schema_text)
        assert set(advertised_schema["required"]).issuperset(REPLACEMENT_FIELDS[method])


async def test_incomplete_native_parse_can_recover_with_complete_fallback(
    replacement_service,
    monkeypatch,
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "native_parse")
    payload = {key: [] for key in REPLACEMENT_FIELDS["compress_memory"]}
    payload["compressed_facts"] = [{"category": "hobby", "content": "Enjoys gardening"}]
    service, requests = replacement_service([{}, payload])

    result = await service.compress_memory(123, "system", "Enjoys gardening")
    assert result.compressed_facts[0].content == "Enjoys gardening"
    assert len(requests) == 2


async def test_empty_extraction_is_still_a_valid_no_change_result(
    replacement_service,
    monkeypatch,
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "json_object")
    service, _ = replacement_service([{}])
    result = await service.extract_memory(123, "system", "Hello")
    assert isinstance(result, MemoryExtraction)
    assert result.new_facts == []


@pytest.mark.parametrize("method", REPLACEMENT_FIELDS)
async def test_malformed_replacement_preserves_database_profile(
    replacement_service,
    monkeypatch,
    method,
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "json_object")
    service, _ = replacement_service([{"profile_summary": "Incomplete replacement"}])
    runner_module = (
        memory_compressor if method == "compress_memory" else memory_consolidator
    )
    runner = (
        memory_compressor.compress_user_memory
        if method == "compress_memory"
        else memory_consolidator.consolidate_user_memory
    )
    monkeypatch.setattr(runner_module.llm_service, method, getattr(service, method))
    user_id = 987654
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "test", "Test")
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {
                "$set": {
                    "facts": [{"category": "personal", "content": "Lives in पुणे"}],
                    "beliefs": [{"content": "Values family time"}],
                    "events": [
                        {
                            "description": "Graduated",
                            "event_date": "2025",
                            "significance": "major",
                        }
                    ],
                    "insights": [{"content": "Often talks through decisions"}],
                }
            },
        )
        before = await db["user_profiles"].find_one({"_id": user_id})

    await runner(user_id)

    async with connection.db_session() as db:
        after = await db["user_profiles"].find_one({"_id": user_id})
    assert after == before


def test_replacement_schema_constructor_defaults_remain_compatible():
    assert MemoryCompression().compressed_facts == []
    assert MemoryConsolidation().consolidated_facts == []
