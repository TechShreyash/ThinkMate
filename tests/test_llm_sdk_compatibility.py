"""Exercise the real SDK across its HTTPX2 boundary without external requests."""

import json

import httpx2
import pytest
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, BadRequestError

from app.config import config
from app.services import llm_service as llm_module
from app.services.schemas import MemoryExtraction


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-sdk-test",
        "object": "chat.completion",
        "created": 0,
        "model": "sdk-test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "refusal": None},
            }
        ],
    }


@pytest.fixture
async def sdk_service(monkeypatch):
    clients = []
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://llm.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "sdk-test-placeholder")
    monkeypatch.setattr(config, "LLM_MODEL", "sdk-test-model")
    monkeypatch.setattr(config, "LLM_EXTRACTION_MODEL", "")
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY_SECS", 0)
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "json_object")
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


async def test_reply_passes_through_real_sdk_and_httpx2(sdk_service):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(
            200, json=_completion('{"reply":"Hello!","reaction":""}')
        )

    service = sdk_service(handler)
    result = await service.generate_reply_bundle(
        123, "system", [{"role": "user", "content": "Hi"}]
    )

    assert result == ("Hello!", None)
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "sdk-test-model"
    assert body["messages"][-1] == {"role": "user", "content": "Hi"}
    assert body["response_format"] == {"type": "json_object"}
    assert requests[0].extensions["timeout"]["read"] == config.LLM_REQUEST_TIMEOUT_SECS


@pytest.mark.parametrize(
    ("raw_delta", "expected"),
    [
        ("0.05", 0.05),
        ("-0.05", -0.05),
        ("100", 0.2),
        ("-100", -0.2),
        ("9" * 400, 0.2),
        ("NaN", None),
        ("Infinity", None),
        ("-1e309", None),
        ("true", None),
        ('"0.05"', None),
        ("null", None),
    ],
)
async def test_group_affinity_is_bounded_without_losing_reply(
    sdk_service, raw_delta, expected
):
    def handler(request):
        content = '{"reply":"Hello!","reaction":"","affinity_delta":' + raw_delta + "}"
        return httpx2.Response(200, json=_completion(content))

    service = sdk_service(handler)
    result = await service.generate_reply_bundle(
        -123,
        "system",
        [{"role": "user", "content": "Hi"}],
        with_affinity=True,
    )
    assert result == ("Hello!", None, expected)


async def test_native_parse_uses_sdk_schema_without_fallback(sdk_service, monkeypatch):
    requests = []
    content = '{"new_facts":[{"category":"hobby","content":"Enjoys gardening."}]}'

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion(content))

    service = sdk_service(handler)
    monkeypatch.setattr(config, "LLM_STRUCTURED_MODE", "native_parse")
    result = await service.extract_memory(123, "system", "I enjoy gardening.")

    assert isinstance(result, MemoryExtraction)
    assert result.new_facts[0].content == "Enjoys gardening."
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    ("transport_error", "sdk_error"),
    [(httpx2.ConnectError, APIConnectionError), (httpx2.ReadTimeout, APITimeoutError)],
)
async def test_transport_failures_use_only_application_retry_budget(
    sdk_service, transport_error, sdk_error
):
    requests = []

    def handler(request):
        requests.append(request)
        raise transport_error("mock transport failure", request=request)

    service = sdk_service(handler)

    with pytest.raises(sdk_error):
        await service.generate_reply_bundle(123, "system", [])

    assert len(requests) == config.LLM_MAX_RETRIES + 1


async def test_bad_request_is_not_retried(sdk_service):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(
            400,
            json={
                "error": {"message": "Invalid request", "type": "invalid_request_error"}
            },
        )

    service = sdk_service(handler)

    with pytest.raises(BadRequestError):
        await service.generate_reply_bundle(123, "system", [])

    assert len(requests) == 1
