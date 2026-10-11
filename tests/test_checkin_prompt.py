"""Proactive prompts must ground callbacks without guessing language or current state."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.prompts.checkin_prompt import SYSTEM_CHECKIN_PROMPT
from app.services.llm_service import llm_service


def test_checkin_rules_reject_ungrounded_and_sensitive_callbacks():
    body = " ".join(SYSTEM_CHECKIN_PROMPT.lower().split())
    assert 'default "calm" mood' in body
    assert "old emotional state" in body
    assert "recorded date" in body
    assert "sensitive disclosures" in body
    assert "asked you to leave alone" in body
    assert "single word nothing" in body


@pytest.mark.asyncio
async def test_checkin_request_preserves_recorded_language_preference():
    supplied_context = (
        "Communication Style Preference: Reply in Hindi using Devanagari. "
        "Core fact: Enjoys hiking."
    )
    message = MagicMock()
    message.content = "अपनी अगली हाइक का प्लान बनाया?"
    response = MagicMock()
    response.choices = [MagicMock(message=message)]

    with (
        patch.object(
            llm_service.client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=response,
        ) as create,
        patch.object(llm_service, "_fire_log"),
    ):
        result = await llm_service.generate_checkin(
            1, supplied_context, "Enjoys hiking."
        )

    assert result == message.content
    sent = create.await_args.kwargs["messages"]
    assert supplied_context in sent[0]["content"]
    assert "explicit language preference" in sent[0]["content"]
    assert "neither is evidence" in sent[0]["content"]
    assert "English if unknown" in sent[1]["content"]
