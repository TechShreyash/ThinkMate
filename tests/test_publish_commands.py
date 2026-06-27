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
    """When publishing is enabled, default and group menus are registered."""
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.delete_my_commands = AsyncMock()

    original = config.TELEGRAM_PUBLISH_COMMANDS
    config.TELEGRAM_PUBLISH_COMMANDS = True
    try:
        await setup_bot_commands(bot)
    finally:
        config.TELEGRAM_PUBLISH_COMMANDS = original
        
    assert bot.set_my_commands.call_count == 2
    scopes = [call.kwargs.get("scope") for call in bot.set_my_commands.call_args_list]
    assert any(isinstance(scope, BotCommandScopeDefault) for scope in scopes)
    assert any(isinstance(scope, BotCommandScopeAllGroupChats) for scope in scopes)
    published = {
        type(call.kwargs.get("scope")): [cmd.command for cmd in call.args[0]]
        for call in bot.set_my_commands.call_args_list
    }
    assert {"start", "help", "onboard", "checkins", "profile", "reset", "reactions"}.issubset(
        set(published[BotCommandScopeDefault])
    )
    assert {"start", "help", "quiet", "chatty", "groupbot", "groupmode"}.issubset(
        set(published[BotCommandScopeAllGroupChats])
    )
    bot.delete_my_commands.assert_not_called()

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
