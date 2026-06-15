"""Tests for the TELEGRAM_PUBLISH_COMMANDS configuration and setup_bot_commands behavior."""
import pytest
import os
import importlib
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import BotCommandScopeAllGroupChats, BotCommandScopeDefault

from app.config import config
from app.handlers.commands import setup_bot_commands

def test_telegram_publish_commands_exists_and_defaults_to_true():
    """Assert TELEGRAM_PUBLISH_COMMANDS exists on config and defaults to True."""
    assert hasattr(config, "TELEGRAM_PUBLISH_COMMANDS")
    assert config.TELEGRAM_PUBLISH_COMMANDS is True

def test_telegram_publish_commands_reads_from_env():
    """Assert TELEGRAM_PUBLISH_COMMANDS is read live from the environment."""
    import app.config as config_module
    
    had_var = "TELEGRAM_PUBLISH_COMMANDS" in os.environ
    prior = os.environ.get("TELEGRAM_PUBLISH_COMMANDS")
    try:
        os.environ["TELEGRAM_PUBLISH_COMMANDS"] = "False"
        reloaded = importlib.reload(config_module)
        assert reloaded.config.TELEGRAM_PUBLISH_COMMANDS is False

        os.environ["TELEGRAM_PUBLISH_COMMANDS"] = "True"
        reloaded = importlib.reload(config_module)
        assert reloaded.config.TELEGRAM_PUBLISH_COMMANDS is True
    finally:
        if had_var:
            os.environ["TELEGRAM_PUBLISH_COMMANDS"] = prior
        else:
            os.environ.pop("TELEGRAM_PUBLISH_COMMANDS", None)
        importlib.reload(config_module)

@pytest.mark.asyncio
async def test_setup_bot_commands_when_enabled():
    """When TELEGRAM_PUBLISH_COMMANDS is True, set_my_commands is called."""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.delete_my_commands = AsyncMock()

    original = config.TELEGRAM_PUBLISH_COMMANDS
    config.TELEGRAM_PUBLISH_COMMANDS = True
    try:
        await setup_bot_commands(bot)
    finally:
        config.TELEGRAM_PUBLISH_COMMANDS = original
        
    bot.set_my_commands.assert_called_once()
    args, kwargs = bot.set_my_commands.call_args
    assert isinstance(kwargs.get("scope"), BotCommandScopeDefault)

    # The stale group-scoped menu from earlier versions must be cleared.
    bot.delete_my_commands.assert_called_once()
    _, del_kwargs = bot.delete_my_commands.call_args
    assert isinstance(del_kwargs.get("scope"), BotCommandScopeAllGroupChats)

@pytest.mark.asyncio
async def test_setup_bot_commands_when_disabled():
    """When TELEGRAM_PUBLISH_COMMANDS is False, set_my_commands is NOT called."""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    original = config.TELEGRAM_PUBLISH_COMMANDS
    config.TELEGRAM_PUBLISH_COMMANDS = False
    try:
        await setup_bot_commands(bot)
    finally:
        config.TELEGRAM_PUBLISH_COMMANDS = original
        
    bot.set_my_commands.assert_not_called()

@pytest.mark.asyncio
async def test_setup_bot_commands_handles_exception_gracefully():
    """When set_my_commands raises an error, it is caught and does not block/raise."""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(side_effect=Exception("API limit exceeded"))

    original = config.TELEGRAM_PUBLISH_COMMANDS
    config.TELEGRAM_PUBLISH_COMMANDS = True
    try:
        # Should not raise an exception
        await setup_bot_commands(bot)
    finally:
        config.TELEGRAM_PUBLISH_COMMANDS = original

    bot.set_my_commands.assert_called_once()
