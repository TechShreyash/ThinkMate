"""Tests for LOGS_CHANNEL_ID environment configurability and its impact on logging behavior."""
import pytest
import os
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import config
from app.services import log_forwarder
from app.services.error_log_sink import make_error_log_sink

def test_logs_channel_id_reads_from_env_or_defaults_to_none():
    """Assert LOGS_CHANNEL_ID reads from environment, or defaults to None if not set."""
    import app.config as config_module
    
    had_var = "LOGS_CHANNEL_ID" in os.environ
    prior = os.environ.get("LOGS_CHANNEL_ID")
    try:
        # 1. Unset env var -> should reload as None
        os.environ.pop("LOGS_CHANNEL_ID", None)
        reloaded = importlib.reload(config_module)
        assert reloaded.config.LOGS_CHANNEL_ID is None

        # 2. Set env var -> should reload with configured integer value
        os.environ["LOGS_CHANNEL_ID"] = "-999123456"
        reloaded = importlib.reload(config_module)
        assert reloaded.config.LOGS_CHANNEL_ID == -999123456

        # 3. Empty env var -> should default to None
        os.environ["LOGS_CHANNEL_ID"] = "  "
        reloaded = importlib.reload(config_module)
        assert reloaded.config.LOGS_CHANNEL_ID is None
    finally:
        if had_var:
            os.environ["LOGS_CHANNEL_ID"] = prior
        else:
            os.environ.pop("LOGS_CHANNEL_ID", None)
        importlib.reload(config_module)

@pytest.mark.asyncio
async def test_log_forwarder_noop_when_channel_unset():
    """log_forwarder.send and send_document do not send message if LOGS_CHANNEL_ID is unset (None)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()

    original = config.LOGS_CHANNEL_ID
    config.LOGS_CHANNEL_ID = None
    try:
        await log_forwarder.send(bot, source_chat_id=123, text="test log")
        await log_forwarder.send_document(bot, source_chat_id=123, filename="test.json", content=b"{}")
    finally:
        config.LOGS_CHANNEL_ID = original

    bot.send_message.assert_not_called()
    bot.send_document.assert_not_called()

def test_error_log_sink_noop_when_channel_unset():
    """make_error_log_sink does not schedule delivery if LOGS_CHANNEL_ID is unset (None)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    
    # Create the sink
    loop = MagicMock()
    sink = make_error_log_sink(bot, loop)

    # Message mock
    message = MagicMock()
    message.record = {
        "level": MagicMock(no=40, name="ERROR"),  # ERROR is 40 (>= 30 warning)
        "name": "test_module",
        "function": "test_func",
        "message": "test error message",
        "extra": {}
    }

    original = config.LOGS_CHANNEL_ID
    config.LOGS_CHANNEL_ID = None
    try:
        sink(message)
    finally:
        config.LOGS_CHANNEL_ID = original

    # Loop should not have been called because sink returns early
    loop.call_soon_threadsafe.assert_not_called()
