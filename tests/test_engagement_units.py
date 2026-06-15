"""Engagement (Phase 12) unit tests: time_context, mood trend, and generate_checkin.

Covers the leaf primitives of the engagement spec without touching the hot path or any
real network:

* ``build_system_prompt`` backward-compatibility and the additive ``## ⏰ TIME CONTEXT``
  section (Requirements 1.1, 1.2).
* ``compile_memory_text`` mood-trend rendering, present and absent (Requirements 3.4, 3.5).
* ``llm_service.generate_checkin`` short-circuit, normal, decline-sentinel, and error paths
  with the ``llm.proactive_checkin.*`` metric accounting (Requirements 7.2, 7.3, 7.4, 11.4).

All tests use the mongomock + pytest-asyncio harness from ``tests/conftest.py``; the LLM
client is patched with ``AsyncMock`` (no network) and an autouse ``metrics.reset()`` fixture
isolates the metric registry per test.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.prompts.system_prompt import build_system_prompt
from app.services.memory_loader import compile_memory_text
from app.services.llm_service import llm_service
from app.services.metrics import metrics

TIME_SECTION_HEADER = "## ⏰ TIME CONTEXT"


@pytest.fixture(autouse=True)
def reset_metrics():
    """Isolate metric state from other tests (mirrors test_metrics_instrumentation)."""
    metrics.reset()
    yield
    metrics.reset()


# --------------------------------------------------------------------------- #
# 1. build_system_prompt: backward-compat + additive time context section
# --------------------------------------------------------------------------- #
def test_build_system_prompt_empty_time_context_is_backward_compatible():
    """Omitting time_context equals passing the empty default, byte-for-byte (Req 1.1)."""
    assert build_system_prompt("P", "M") == build_system_prompt("P", "M", time_context="")


def test_build_system_prompt_empty_time_context_renders_no_section():
    """The empty default must not introduce the TIME CONTEXT section (Req 1.2)."""
    prompt = build_system_prompt("P", "M")
    assert TIME_SECTION_HEADER not in prompt


def test_build_system_prompt_non_empty_time_context_adds_one_section():
    """A non-empty time_context adds exactly one labelled section that includes the text (Req 1.2)."""
    text = "Current date/time (UTC): 2024-06-01 Saturday, 14:30 UTC\nLast talked: 3 day(s) ago"
    prompt = build_system_prompt("P", "M", time_context=text)
    assert prompt.count(TIME_SECTION_HEADER) == 1
    assert text in prompt


def test_build_system_prompt_speaker_name_anchors_the_reply_target():
    """A group speaker_name renders a clear 'who you are replying to' anchor with the name."""
    prompt = build_system_prompt("P", "M", speaker_name="Shreyash")
    assert "WHO YOU ARE REPLYING TO" in prompt
    assert "Shreyash" in prompt


def test_build_system_prompt_speaker_block_present_without_user_memory():
    """The speaker anchor renders even for a brand-new sender with no stored memories."""
    # No user_memory_text -> the per-user MEMORIES block is absent, but the name anchor
    # must still be there so the model never misattributes the reply.
    prompt = build_system_prompt("P", "M", user_memory_text="", speaker_name="Shreyash")
    assert "WHO YOU ARE REPLYING TO" in prompt
    assert "MEMORIES OF THE PERSON SPEAKING NOW" not in prompt


def test_build_system_prompt_no_speaker_name_is_backward_compatible():
    """Omitting speaker_name (DM path) renders no speaker anchor, byte-for-byte."""
    assert build_system_prompt("P", "M") == build_system_prompt("P", "M", speaker_name="")


def test_build_system_prompt_group_section_explains_multiparty():
    """is_group renders the multi-party section naming the transcript format and the bot."""
    prompt = build_system_prompt("P", "M", is_group=True, bot_name="Nova")
    assert "GROUP CHAT" in prompt
    assert "Nova" in prompt
    # It must explain that the name prefix is attribution, not message text.
    assert "attribution" in prompt.lower() or "who said" in prompt.lower()


def test_build_system_prompt_no_group_section_in_dm():
    """The DM path (is_group default False) never renders the group multi-party section."""
    prompt = build_system_prompt("P", "M")
    assert "GROUP CHAT" not in prompt
    assert build_system_prompt("P", "M") == build_system_prompt("P", "M", is_group=False)


# --------------------------------------------------------------------------- #
# 2. compile_memory_text: mood trend rendering (present / absent)
# --------------------------------------------------------------------------- #
def test_compile_memory_text_renders_mood_trend_with_history():
    """A profile with mood_history renders the trend line with the moods (Req 3.4)."""
    rendered = compile_memory_text(
        {"mood_history": [{"mood": "happy"}, {"mood": "stressed"}]}
    )
    assert "Recent mood trend" in rendered
    assert "happy" in rendered
    assert "stressed" in rendered


def test_compile_memory_text_omits_mood_trend_without_history():
    """A profile with no mood_history renders no trend line and does not raise (Req 3.5)."""
    rendered = compile_memory_text({})
    assert "Recent mood trend" not in rendered


# --------------------------------------------------------------------------- #
# 3. generate_checkin: short-circuit / normal / decline / error
# --------------------------------------------------------------------------- #
def _fake_response(content: str) -> MagicMock:
    """Build the same fake-response structure used in test_metrics_instrumentation."""
    fake_message = MagicMock()
    fake_message.content = content
    fake_choice = MagicMock()
    fake_choice.message = fake_message
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    return fake_response


@pytest.mark.asyncio
async def test_generate_checkin_blank_memory_short_circuits_without_llm_call():
    """Blank memory_text returns None and never calls the client (Req 7.2, 7.3)."""
    with patch.object(
        llm_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create, patch.object(llm_service, "_fire_log"):
        result = await llm_service.generate_checkin(1, "sys", "")
        assert result is None
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_generate_checkin_returns_text_and_records_success():
    """A normal opener is returned and the proactive_checkin metrics are recorded (Req 7.2, 11.4)."""
    opener = "hey, how did the trek go?"
    with patch.object(
        llm_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create, patch.object(llm_service, "_fire_log"):
        mock_create.return_value = _fake_response(opener)
        result = await llm_service.generate_checkin(1, "sys", "some real memory")

    assert result == opener
    snap = metrics.snapshot()
    assert snap["counters"]["llm.proactive_checkin.calls"] == 1
    assert snap["counters"]["llm.proactive_checkin.success"] == 1
    assert "llm.proactive_checkin.failure" not in snap["counters"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["NOTHING", "none", "n/a"])
async def test_generate_checkin_decline_sentinel_returns_none(sentinel):
    """Decline sentinels mean 'send nothing' (Req 7.3)."""
    with patch.object(
        llm_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create, patch.object(llm_service, "_fire_log"):
        mock_create.return_value = _fake_response(sentinel)
        result = await llm_service.generate_checkin(1, "sys", "some real memory")

    assert result is None


@pytest.mark.asyncio
async def test_generate_checkin_error_returns_none_and_records_failure():
    """A raising call returns None (never raises) and records the failure split (Req 7.4, 11.4)."""
    with patch.object(
        llm_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create, patch.object(llm_service, "_fire_log"):
        mock_create.side_effect = RuntimeError("boom")
        result = await llm_service.generate_checkin(1, "sys", "some real memory")

    assert result is None
    snap = metrics.snapshot()
    assert snap["counters"]["llm.proactive_checkin.calls"] == 1
    assert snap["counters"]["llm.proactive_checkin.failure"] == 1
    assert "llm.proactive_checkin.success" not in snap["counters"]
