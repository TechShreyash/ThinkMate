"""Tests for the group self-introduction sent when the bot is added to a group.

Covers ``app/handlers/membership.py``:
- A join into a group/supergroup sends exactly one intro message to that chat.
- A join into a private chat (a user starting the bot) sends nothing.
- The intro references the bot's name and the resolved start-command trigger.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import config
from app.handlers.membership import on_added_to_group


def _make_event(chat_type: str, chat_id: int = -100777, title: str = "Test Group"):
    """Build a mock ChatMemberUpdated-like event with a mocked bot."""
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="thinkmate_bot", first_name="ThinkMate"))
    bot.send_message = AsyncMock()
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=title)
    return SimpleNamespace(chat=chat, bot=bot)


@pytest.mark.asyncio
async def test_intro_sent_on_group_join():
    """Joining a supergroup posts a single intro mentioning the bot and start command."""
    orig_name = config.BOT_NAME
    config.BOT_NAME = "Nova"
    try:
        event = _make_event("supergroup")
        await on_added_to_group(event)

        event.bot.send_message.assert_called_once()
        args, kwargs = event.bot.send_message.call_args
        # Sent to the joined chat...
        assert args[0] == event.chat.id
        sent_text = args[1]
        # ...as HTML, naming the bot and pointing to the DM guide command.
        assert kwargs.get("parse_mode") == "HTML"
        assert "Nova" in sent_text
        start_trigger = config.COMMANDS.get("start", ("start", True))[0]
        assert f"/{start_trigger}" in sent_text
        assert "@thinkmate_bot" in sent_text
    finally:
        config.BOT_NAME = orig_name


@pytest.mark.asyncio
async def test_no_intro_on_private_join():
    """A private-chat 'join' (user pressing start) must not trigger the group intro."""
    event = _make_event("private", chat_id=12345)
    await on_added_to_group(event)
    event.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_intro_send_failure_is_swallowed():
    """A send failure never propagates out of the handler."""
    event = _make_event("group")
    event.bot.send_message.side_effect = Exception("Forbidden: not enough rights")
    # Must not raise.
    await on_added_to_group(event)


@pytest.mark.asyncio
async def test_no_intro_when_disabled_by_config():
    """When GROUP_INTRO_ON_JOIN is False, a group join sends no intro message."""
    orig = config.GROUP_INTRO_ON_JOIN
    config.GROUP_INTRO_ON_JOIN = False
    try:
        event = _make_event("supergroup")
        await on_added_to_group(event)
        event.bot.send_message.assert_not_called()
    finally:
        config.GROUP_INTRO_ON_JOIN = orig


@pytest.mark.asyncio
async def test_intro_sent_when_enabled_by_config():
    """When GROUP_INTRO_ON_JOIN is True (default), a group join sends the intro."""
    orig = config.GROUP_INTRO_ON_JOIN
    config.GROUP_INTRO_ON_JOIN = True
    try:
        event = _make_event("supergroup")
        await on_added_to_group(event)
        event.bot.send_message.assert_called_once()
    finally:
        config.GROUP_INTRO_ON_JOIN = orig


def test_group_intro_on_join_defaults_to_true():
    """The config flag exists and defaults to True."""
    assert hasattr(config, "GROUP_INTRO_ON_JOIN")
    assert config.GROUP_INTRO_ON_JOIN is True
