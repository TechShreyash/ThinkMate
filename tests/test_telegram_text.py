"""Tests for Telegram-safe text delivery helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from app.config import config
from app.services.telegram_text import (
    SAFE_CHUNK_CHARS,
    _split_text,
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
    assert all(
        "reply_markup" not in call.kwargs
        for call in bot.send_message.await_args_list[1:]
    )


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


@pytest.mark.asyncio
async def test_html_between_chunk_size_and_telegram_limit_keeps_tags_intact():
    message = MagicMock(answer=AsyncMock(return_value="sent"))
    await send_message_text(message, "<b>" + "x" * 4000 + "</b>", parse_mode="HTML")
    message.answer.assert_awaited_once_with("x" * 4000)


@pytest.mark.asyncio
async def test_oversized_markdown_preserves_literal_comparisons():
    message = MagicMock(answer=AsyncMock(return_value="sent"))
    text = "compare <left> and <right> " * 200
    await send_message_text(message, text, parse_mode="Markdown")
    chunks = [call.args[0] for call in message.answer.await_args_list]
    assert "<left>" in " ".join(chunks)
    assert all(
        "parse_mode" not in call.kwargs for call in message.answer.await_args_list
    )


@pytest.mark.asyncio
async def test_oversized_html_enum_is_delivered_without_literal_tags():
    message = MagicMock(answer=AsyncMock(return_value="sent"))
    await send_message_text(
        message, "<b>" + "x" * 4000 + "</b>", parse_mode=ParseMode.HTML
    )
    message.answer.assert_awaited_once_with("x" * 4000)


def test_text_chunks_preserve_code_indentation_and_content():
    text = "x" * 3998 + "\n    return True\n\nnext paragraph"
    chunks = _split_text(text)
    assert "".join(chunks) == text
    assert chunks[1].startswith("    return True")
    assert all(0 < len(chunk) <= SAFE_CHUNK_CHARS for chunk in chunks)
