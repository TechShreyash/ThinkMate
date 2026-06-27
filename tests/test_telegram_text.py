"""Tests for Telegram-safe text delivery helpers."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.config import config
from app.services.telegram_text import (
    SAFE_CHUNK_CHARS,
    apply_response_cap,
    send_bot_text,
    send_message_text,
)


def test_apply_response_cap_adds_truncation_marker():
    original = config.MAX_RESPONSE_CHARS
    config.MAX_RESPONSE_CHARS = 30
    try:
        capped = apply_response_cap("x" * 100)
    finally:
        config.MAX_RESPONSE_CHARS = original

    assert len(capped) <= 30
    assert capped.endswith("[truncated]")


@pytest.mark.asyncio
async def test_send_bot_text_splits_long_messages_and_keeps_markup_on_first_chunk_only():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value="sent")
    long_text = "word " * 1000

    await send_bot_text(bot, 12345, long_text, reply_markup="keyboard")

    assert bot.send_message.await_count > 1
    chunks = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert all(0 < len(chunk) <= SAFE_CHUNK_CHARS for chunk in chunks)
    assert bot.send_message.await_args_list[0].kwargs["reply_markup"] == "keyboard"
    assert all("reply_markup" not in call.kwargs for call in bot.send_message.await_args_list[1:])


@pytest.mark.asyncio
async def test_send_message_text_falls_back_to_answer_when_reply_fails():
    message = MagicMock()
    message.reply = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="reply failed")
    )
    message.answer = AsyncMock(return_value="answered")

    result = await send_message_text(message, "hello", prefer_reply=True)

    assert result == "answered"
    message.reply.assert_awaited_once_with("hello")
    message.answer.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_send_message_text_strips_parse_mode_before_splitting_oversized_html():
    message = MagicMock()
    message.answer = AsyncMock(return_value="sent")
    long_html = "<b>" + ("x" * 4100) + "</b>"

    await send_message_text(message, long_html, parse_mode="HTML")

    assert message.answer.await_count > 1
    for call in message.answer.await_args_list:
        assert "parse_mode" not in call.kwargs
        assert "<b>" not in call.args[0]
        assert len(call.args[0]) <= SAFE_CHUNK_CHARS
