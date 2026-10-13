"""Verify model policies on the real SDK's HTTP boundary, without network calls."""

import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI, BadRequestError

from app.config import Config, config
from app.services import llm_service as llm_module
from app.services.schemas import (
    GroupMemoryExtraction,
    MemoryCompression,
    MemoryConsolidation,
    MemoryExtraction,
)

CHAT_OPERATIONS = ("reply", "checkin")
MEMORY_OPERATIONS = (
    "extract_memory",
    "extract_group_memory",
    "compress_memory",
    "consolidate_memory",
)
OPERATIONS = CHAT_OPERATIONS + MEMORY_OPERATIONS
MEMORY_OUTPUTS = {
    "extract_memory": {
        "new_facts": [{"category": "hobby", "content": "Enjoys reading."}]
    },
    "extract_group_memory": {
        "updates": [
            {
                "participant": "Alex",
                "extraction": {
                    "new_facts": [{"category": "hobby", "content": "Enjoys reading."}]
                },
            }
        ]
    },
    "compress_memory": {
        "compressed_facts": [{"category": "hobby", "content": "Enjoys reading."}],
        "compressed_beliefs": [],
        "compressed_events": [],
    },
    "consolidate_memory": {
        "consolidated_facts": [{"category": "hobby", "content": "Enjoys reading."}],
        "consolidated_beliefs": [],
        "consolidated_events": [],
        "insights": [],
    },
}
MEMORY_TYPES = {
    "extract_memory": MemoryExtraction,
    "extract_group_memory": GroupMemoryExtraction,
    "compress_memory": MemoryCompression,
    "consolidate_memory": MemoryConsolidation,
}


def _completion(content):
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-routing-test",
            "object": "chat.completion",
            "created": 0,
            "model": "routing-test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": None,
                    },
                }
            ],
        },
    )


def _success(operation):
    if operation == "reply":
        return _completion('{"reply":"Hello!","reaction":""}')
    if operation == "checkin":
        return _completion("How is your reading going?")
    return _completion(json.dumps(MEMORY_OUTPUTS[operation]))


def _error(status=400):
    return httpx2.Response(
        status,
        json={"error": {"message": "Model unavailable", "type": "model_unavailable"}},
    )


async def _invoke(service, operation):
    if operation == "reply":
        return await service.generate_reply_bundle(123, "system", [])
    if operation == "checkin":
        return await service.generate_checkin(123, "system", "Enjoys reading.")
    return await getattr(service, operation)(123, "system", "Enjoys reading.")


def _assert_success(operation, result):
    if operation == "reply":
        assert result == ("Hello!", None)
    elif operation == "checkin":
        assert result == "How is your reading going?"
    else:
        assert isinstance(result, MEMORY_TYPES[operation])
        for field, expected in MEMORY_OUTPUTS[operation].items():
            assert result.model_dump(exclude_unset=True)[field] == expected


def _models(operation):
    if operation in CHAT_OPERATIONS:
        return "chat-primary", "chat-backup"
    return "extract-primary", "extract-backup"


def _efforts(operation):
    if operation in CHAT_OPERATIONS:
        return "low", "minimal"
    return "high", "medium"


@pytest.fixture
async def routing_service(monkeypatch):
    clients = []
    settings = {
        "LLM_BASE_URL": "https://llm.example.invalid/v1",
        "LLM_API_KEY": "routing-test-placeholder",
        "LLM_MODEL": "legacy-model",
        "LLM_CHAT_MODEL": "chat-primary",
        "LLM_EXTRACTION_MODEL": "extract-primary",
        "LLM_CHAT_FALLBACK_MODEL": "chat-backup",
        "LLM_EXTRACTION_FALLBACK_MODEL": "extract-backup",
        "LLM_CHAT_REASONING_EFFORT": "low",
        "LLM_EXTRACTION_REASONING_EFFORT": "high",
        "LLM_CHAT_FALLBACK_REASONING_EFFORT": "minimal",
        "LLM_EXTRACTION_FALLBACK_REASONING_EFFORT": "medium",
        "LLM_MAX_RETRIES": 1,
        "LLM_RETRY_BASE_DELAY_SECS": 0,
        "LLM_STRUCTURED_MODE": "json_object",
        "LLM_CHAT_STRUCTURED_MODE": "json_object",
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(llm_module.metrics, "record_llm", lambda *args, **kwargs: None)

    def make_service(handler):
        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        monkeypatch.setattr(
            llm_module,
            "AsyncOpenAI",
            lambda **kwargs: AsyncOpenAI(http_client=http_client, **kwargs),
        )
        service = llm_module.LLMService()
        monkeypatch.setattr(service, "_fire_log", lambda *args, **kwargs: None)
        clients.append(service.client)
        return service

    yield make_service
    for client in clients:
        await client.close()


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_primary_routes_each_operation_and_reasoning(routing_service, operation):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _success(operation)

    result = await _invoke(routing_service(handler), operation)

    _assert_success(operation, result)
    assert len(bodies) == 1
    assert bodies[0]["model"] == _models(operation)[0]
    assert bodies[0]["reasoning_effort"] == _efforts(operation)[0]
    if operation != "checkin":
        assert bodies[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("operation", ("reply", "extract_memory"))
@pytest.mark.parametrize("primary_fails", (False, True))
async def test_gemini_proxy_environment_overrides_legacy_version_pin(
    routing_service, monkeypatch, operation, primary_fails
):
    """Send family aliases to the proxy, including after deploying over a legacy pin."""
    settings = {
        "LLM_CHAT_MODEL": "gemini-flash-lite",
        "LLM_CHAT_REASONING_EFFORT": "minimal",
        "LLM_CHAT_FALLBACK_MODEL": "gemma",
        "LLM_CHAT_FALLBACK_REASONING_EFFORT": "minimal",
        "LLM_EXTRACTION_MODEL": "gemini-flash",
        "LLM_EXTRACTION_REASONING_EFFORT": "high",
        "LLM_EXTRACTION_FALLBACK_MODEL": "gemma",
        "LLM_EXTRACTION_FALLBACK_REASONING_EFFORT": "high",
        "LLM_STRUCTURED_MODE": "json_object",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        llm_module,
        "config",
        Config(
            LLM_BASE_URL="https://llm.example.invalid/v1",
            LLM_API_KEY="routing-test-placeholder",
            LLM_MODEL="gemini-3.1-flash-lite",
            LLM_MAX_RETRIES=1,
            LLM_RETRY_BASE_DELAY_SECS=0,
        ),
    )
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if primary_fails and len(bodies) == 1:
            return _error()
        return _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))

    primary = "gemini-flash-lite" if operation == "reply" else "gemini-flash"
    effort = "minimal" if operation == "reply" else "high"
    assert [body["model"] for body in bodies] == (
        [primary, "gemma"] if primary_fails else [primary]
    )
    assert [body["reasoning_effort"] for body in bodies] == [effort] * len(bodies)
    assert all(body["response_format"] == {"type": "json_object"} for body in bodies)


@pytest.mark.parametrize("operation", MEMORY_OPERATIONS)
async def test_native_parse_routes_every_memory_operation_with_reasoning(
    routing_service, monkeypatch, operation
):
    bodies = []
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "native_parse")

    def handler(request):
        bodies.append(json.loads(request.content))
        return _success(operation)

    result = await _invoke(routing_service(handler), operation)

    _assert_success(operation, result)
    assert len(bodies) == 1
    assert bodies[0]["model"] == "extract-primary"
    assert bodies[0]["reasoning_effort"] == "high"
    assert bodies[0]["response_format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    ("chat_model", "extraction_model", "expected_chat", "expected_extraction"),
    [
        ("", "", "legacy-model", "legacy-model"),
        ("custom-chat", "", "custom-chat", "custom-chat"),
        ("", "custom-extraction", "legacy-model", "custom-extraction"),
    ],
)
async def test_blank_models_preserve_legacy_and_chat_inheritance(
    routing_service,
    monkeypatch,
    chat_model,
    extraction_model,
    expected_chat,
    expected_extraction,
):
    bodies = []
    monkeypatch.setattr(config, "LLM_CHAT_MODEL", chat_model)
    monkeypatch.setattr(config, "LLM_EXTRACTION_MODEL", extraction_model)

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        operation = "reply" if len(bodies) == 1 else "extract_memory"
        return _success(operation)

    service = routing_service(handler)
    _assert_success("reply", await _invoke(service, "reply"))
    _assert_success("extract_memory", await _invoke(service, "extract_memory"))
    assert [body["model"] for body in bodies] == [expected_chat, expected_extraction]
    assert [body["reasoning_effort"] for body in bodies] == ["low", "high"]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_nonretryable_error_uses_only_the_operations_own_fallback(
    routing_service, operation
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _error() if len(bodies) == 1 else _success(operation)

    result = await _invoke(routing_service(handler), operation)

    _assert_success(operation, result)
    assert [body["model"] for body in bodies] == list(_models(operation))
    assert [body["reasoning_effort"] for body in bodies] == list(_efforts(operation))


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_transient_retries_are_exhausted_before_fallback_gets_own_budget(
    routing_service, operation
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _success(operation) if len(bodies) == 4 else _error(503)

    result = await _invoke(routing_service(handler), operation)

    _assert_success(operation, result)
    primary, fallback = _models(operation)
    primary_effort, fallback_effort = _efforts(operation)
    assert [body["model"] for body in bodies] == [primary, primary, fallback, fallback]
    assert [body["reasoning_effort"] for body in bodies] == [
        primary_effort,
        primary_effort,
        fallback_effort,
        fallback_effort,
    ]


@pytest.mark.parametrize("operation", ("reply", "extract_memory"))
@pytest.mark.parametrize("blank_setting", ("primary", "fallback"))
async def test_blank_reasoning_is_omitted_independently(
    routing_service, monkeypatch, operation, blank_setting
):
    prefix = "CHAT" if operation == "reply" else "EXTRACTION"
    suffix = "REASONING_EFFORT"
    if blank_setting == "fallback":
        suffix = "FALLBACK_REASONING_EFFORT"
    monkeypatch.setattr(config, f"LLM_{prefix}_{suffix}", "")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _error() if len(bodies) == 1 else _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))
    blank_index = 0 if blank_setting == "primary" else 1
    assert "reasoning_effort" not in bodies[blank_index]
    assert (
        bodies[1 - blank_index]["reasoning_effort"]
        == _efforts(operation)[1 - blank_index]
    )


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fallback_setting", ("", "same-primary"))
async def test_disabled_or_duplicate_fallback_is_never_attempted(
    routing_service, monkeypatch, operation, fallback_setting
):
    prefix = "CHAT" if operation in CHAT_OPERATIONS else "EXTRACTION"
    fallback = _models(operation)[0] if fallback_setting == "same-primary" else ""
    monkeypatch.setattr(config, f"LLM_{prefix}_FALLBACK_MODEL", fallback)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _error()

    service = routing_service(handler)
    if operation == "reply":
        with pytest.raises(BadRequestError):
            await _invoke(service, operation)
    else:
        assert await _invoke(service, operation) is None
    assert [body["model"] for body in bodies] == [_models(operation)[0]]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_exhausting_both_models_keeps_existing_failure_contract(
    routing_service, operation
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _error()

    service = routing_service(handler)
    if operation == "reply":
        with pytest.raises(BadRequestError):
            await _invoke(service, operation)
    else:
        assert await _invoke(service, operation) is None
    assert [body["model"] for body in bodies] == list(_models(operation))


@pytest.mark.parametrize("operation", MEMORY_OPERATIONS)
@pytest.mark.parametrize("invalid_content", ("", "not-json"))
async def test_unparseable_memory_uses_extraction_fallback(
    routing_service, operation, invalid_content
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _completion(invalid_content)
        return _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))
    assert [body["model"] for body in bodies] == ["extract-primary", "extract-backup"]


@pytest.mark.parametrize(
    ("operation", "unsafe_content"),
    [
        (
            "extract_memory",
            '{"new_facts":[{"category":"invalid","content":"Unsafe fact"}]}',
        ),
        ("extract_group_memory", '{"unexpected_field":"Unsafe attribution"}'),
        ("compress_memory", "{}"),
        ("consolidate_memory", "{}"),
    ],
)
async def test_schema_or_required_replacement_fields_trigger_fallback(
    routing_service, operation, unsafe_content
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _completion(unsafe_content)
        return _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))
    assert [body["model"] for body in bodies] == ["extract-primary", "extract-backup"]


@pytest.mark.parametrize("operation", MEMORY_OPERATIONS)
async def test_native_then_json_object_runs_on_each_model_in_order(
    routing_service, monkeypatch, operation
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "native_parse")
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["response_format"]["type"] == "json_schema":
            return _error()
        if body["model"] == "extract-primary":
            return _completion("not-json")
        return _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))
    assert [body["model"] for body in bodies] == [
        "extract-primary",
        "extract-primary",
        "extract-backup",
        "extract-backup",
    ]
    assert [body["response_format"]["type"] for body in bodies] == [
        "json_schema",
        "json_object",
        "json_schema",
        "json_object",
    ]
    assert [body["reasoning_effort"] for body in bodies] == [
        "high",
        "high",
        "medium",
        "medium",
    ]


@pytest.mark.parametrize("operation", ("compress_memory", "consolidate_memory"))
async def test_native_replacement_defaults_cannot_skip_missing_field_guard(
    routing_service, monkeypatch, operation
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "native_parse")
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["model"] == "extract-primary":
            return _completion("{}")
        return _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))
    assert [body["model"] for body in bodies] == [
        "extract-primary",
        "extract-primary",
        "extract-backup",
    ]
    assert [body["response_format"]["type"] for body in bodies] == [
        "json_schema",
        "json_object",
        "json_schema",
    ]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("A plain text answer.", ("A plain text answer.", None)),
        ("{}", ("", None)),
        ('{"reply":"   ","reaction":"👍"}', ("", None)),
        ('{"reply":null}', ("", None)),
    ],
)
@pytest.mark.parametrize("chat_mode", ("json_object", "json_schema"))
async def test_reply_output_policies_do_not_call_fallback(
    routing_service, monkeypatch, content, expected, chat_mode
):
    monkeypatch.setattr(config, "LLM_CHAT_STRUCTURED_MODE", chat_mode)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _completion(content)

    assert await _invoke(routing_service(handler), "reply") == expected
    assert len(bodies) == 1


async def test_chat_fallback_preserves_group_affinity_contract(routing_service):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _error()
        return _completion('{"reply":"Hello!","reaction":"","affinity_delta":0.05}')

    result = await routing_service(handler).generate_reply_bundle(
        -123, "system", [], with_affinity=True
    )
    assert result == ("Hello!", None, 0.05)
    assert [body["model"] for body in bodies] == ["chat-primary", "chat-backup"]


@pytest.mark.parametrize("with_affinity", (False, True))
@pytest.mark.parametrize("primary_fails", (False, True))
async def test_chat_json_schema_is_preserved_for_each_profile(
    routing_service, monkeypatch, with_affinity, primary_fails
):
    monkeypatch.setattr(config, "LLM_CHAT_STRUCTURED_MODE", "json_schema")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if primary_fails and len(bodies) == 1:
            return _error()
        content = {"reply": "Hello!", "reaction": ""}
        if with_affinity:
            content["affinity_delta"] = None
        return _completion(json.dumps(content))

    result = await routing_service(handler).generate_reply_bundle(
        123,
        "Voice example: hey! what's up?",
        [
            {"role": "assistant", "content": "An earlier plain-text reply."},
            {"role": "user", "content": "Hi"},
        ],
        with_affinity=with_affinity,
    )

    assert result == (("Hello!", None, None) if with_affinity else ("Hello!", None))
    assert [body["model"] for body in bodies] == (
        ["chat-primary", "chat-backup"] if primary_fails else ["chat-primary"]
    )
    response_format = bodies[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    expected_fields = {"reply", "reaction"}
    if with_affinity:
        expected_fields.add("affinity_delta")
        assert schema["properties"]["affinity_delta"] == {
            "type": ["number", "null"],
            "minimum": -0.2,
            "maximum": 0.2,
        }
    assert set(schema["properties"]) == set(schema["required"]) == expected_fields
    assert all(body["response_format"] == response_format for body in bodies)
    system_text = bodies[0]["messages"][0]["content"]
    assert 'belong inside "reply"' in system_text
    if with_affinity:
        assert "Use null when neutral or unknown." in system_text


@pytest.mark.parametrize("operation", ("checkin", "extract_memory"))
async def test_chat_schema_mode_does_not_change_checkin_or_extraction(
    routing_service, monkeypatch, operation
):
    monkeypatch.setattr(config, "LLM_CHAT_STRUCTURED_MODE", "json_schema")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _success(operation)

    _assert_success(operation, await _invoke(routing_service(handler), operation))
    if operation == "checkin":
        assert "response_format" not in bodies[0]
    else:
        assert bodies[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("operation", ("extract_memory", "extract_group_memory"))
@pytest.mark.parametrize("mode", ("json_object", "native_parse"))
async def test_valid_empty_extraction_does_not_call_model_fallback(
    routing_service, monkeypatch, operation, mode
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", mode)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _completion("{}")

    result = await _invoke(routing_service(handler), operation)
    assert isinstance(result, MEMORY_TYPES[operation])
    assert len(bodies) == 1


@pytest.mark.parametrize("content", ("", "   ", "NOTHING", '"nothing!"', "n/a"))
async def test_checkin_declines_are_successful_without_model_fallback(
    routing_service, content
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _completion(content)

    assert await _invoke(routing_service(handler), "checkin") is None
    assert len(bodies) == 1


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_cancellation_propagates_without_trying_fallback(
    routing_service, operation
):
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _invoke(routing_service(handler), operation)
    assert len(bodies) == 1


@pytest.mark.parametrize("operation", MEMORY_OPERATIONS)
async def test_native_parse_cancellation_does_not_try_json_or_fallback(
    routing_service, monkeypatch, operation
):
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "native_parse")
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _invoke(routing_service(handler), operation)
    assert len(bodies) == 1
    assert bodies[0]["response_format"]["type"] == "json_schema"
