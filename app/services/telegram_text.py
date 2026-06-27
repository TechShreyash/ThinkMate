"""Telegram text delivery helpers.

Telegram text messages have a hard 4096-character limit. These helpers keep all
bot-generated text below that limit, optionally applying ThinkMate's own
``MAX_RESPONSE_CHARS`` cap to LLM-generated replies before chunking.
"""
import html as html_lib
import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app.config import config

TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_CHUNK_CHARS = 4000
_TRUNCATION_SUFFIX = "\n\n[truncated]"
_HTML_TAG_RE = re.compile(r"</?[^>]+>")


def apply_response_cap(text: str, *, max_chars: int | None = None) -> str:
    """Apply the app-level response cap, leaving room for a truncation marker."""
    text = text or ""
    limit = config.MAX_RESPONSE_CHARS if max_chars is None else max_chars
    if limit <= 0 or len(text) <= limit:
        return text
    suffix = _TRUNCATION_SUFFIX
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def _strip_html(text: str) -> str:
    """Best-effort conversion of a long HTML-formatted message to plain text."""
    return html_lib.unescape(_HTML_TAG_RE.sub("", text or ""))


def _split_text(text: str, *, limit: int = SAFE_CHUNK_CHARS) -> list[str]:
    """Split text into Telegram-safe chunks, preferring natural boundaries."""
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind(" "),
        )
        if cut < limit // 2:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            cut = limit
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _prepare_text_and_kwargs(
    text: str,
    kwargs: dict,
    *,
    enforce_app_limit: bool,
) -> tuple[str, dict]:
    send_kwargs = dict(kwargs)
    prepared = apply_response_cap(text) if enforce_app_limit else (text or "")

    # Splitting arbitrary HTML can break tags/entities. For oversized formatted
    # command output, prefer reliable delivery as plain text over a rejected send.
    if send_kwargs.get("parse_mode") and len(prepared) > TELEGRAM_MESSAGE_LIMIT:
        prepared = _strip_html(prepared)
        send_kwargs.pop("parse_mode", None)

    return prepared, send_kwargs


async def send_message_text(
    message: Message,
    text: str,
    *,
    prefer_reply: bool = False,
    enforce_app_limit: bool = False,
    **kwargs,
):
    """Send text from a Message object, splitting into Telegram-safe chunks."""
    prepared, send_kwargs = _prepare_text_and_kwargs(
        text, kwargs, enforce_app_limit=enforce_app_limit
    )
    chunks = _split_text(prepared)
    first_result = None
    for idx, chunk in enumerate(chunks):
        chunk_kwargs = dict(send_kwargs)
        if idx > 0:
            chunk_kwargs.pop("reply_markup", None)
        if prefer_reply and idx == 0:
            try:
                result = await message.reply(chunk, **chunk_kwargs)
            except TelegramBadRequest:
                result = await message.answer(chunk, **chunk_kwargs)
        else:
            result = await message.answer(chunk, **chunk_kwargs)
        if idx == 0:
            first_result = result
    return first_result


async def send_bot_text(
    bot,
    chat_id: int,
    text: str,
    *,
    enforce_app_limit: bool = False,
    **kwargs,
):
    """Send text from a Bot object, splitting into Telegram-safe chunks."""
    prepared, send_kwargs = _prepare_text_and_kwargs(
        text, kwargs, enforce_app_limit=enforce_app_limit
    )
    chunks = _split_text(prepared)
    first_result = None
    for idx, chunk in enumerate(chunks):
        chunk_kwargs = dict(send_kwargs)
        if idx > 0:
            chunk_kwargs.pop("reply_markup", None)
        result = await bot.send_message(chat_id=chat_id, text=chunk, **chunk_kwargs)
        if idx == 0:
            first_result = result
    return first_result
