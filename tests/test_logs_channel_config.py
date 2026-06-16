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


@pytest.mark.asyncio
async def test_diagnostic_respects_forward_flag():
    """log_forwarder.diagnostic forwards only when FORWARD_DIAGNOSTICS is enabled."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    orig_channel = config.LOGS_CHANNEL_ID
    orig_flag = config.FORWARD_DIAGNOSTICS
    config.LOGS_CHANNEL_ID = -100555  # channel set so send() itself is not a no-op
    try:
        # Flag OFF -> no forward, even with a channel configured.
        config.FORWARD_DIAGNOSTICS = False
        await log_forwarder.diagnostic(bot, source_chat_id=123, text="route trace")
        bot.send_message.assert_not_called()

        # Flag ON -> forwarded to the channel.
        config.FORWARD_DIAGNOSTICS = True
        await log_forwarder.diagnostic(bot, source_chat_id=123, text="route trace")
        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs["chat_id"] == -100555
    finally:
        config.LOGS_CHANNEL_ID = orig_channel
        config.FORWARD_DIAGNOSTICS = orig_flag


@pytest.mark.asyncio
async def test_diagnostic_noop_when_channel_unset_even_if_flag_on():
    """diagnostic stays a no-op when the channel is unset, regardless of the flag."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    orig_channel = config.LOGS_CHANNEL_ID
    orig_flag = config.FORWARD_DIAGNOSTICS
    config.LOGS_CHANNEL_ID = None
    config.FORWARD_DIAGNOSTICS = True
    try:
        await log_forwarder.diagnostic(bot, source_chat_id=123, text="route trace")
    finally:
        config.LOGS_CHANNEL_ID = orig_channel
        config.FORWARD_DIAGNOSTICS = orig_flag

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_log_forwarder_clubbing_low_load():
    """Assert log_forwarder sends immediately in low load mode (< 10 logs/minute)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    
    orig_channel = config.LOGS_CHANNEL_ID
    config.LOGS_CHANNEL_ID = -100555
    
    # Reset state
    log_forwarder._buffer = []
    log_forwarder._window_count = 0
    import time
    log_forwarder._window_start = time.time()
    
    try:
        await log_forwarder.send(bot, source_chat_id=123, text="log msg 1")
        await log_forwarder.send(bot, source_chat_id=123, text="log msg 2")
        
        assert len(log_forwarder._buffer) == 0
        assert bot.send_message.call_count == 2
    finally:
        config.LOGS_CHANNEL_ID = orig_channel


@pytest.mark.asyncio
async def test_log_forwarder_clubbing_high_load():
    """Assert log_forwarder buffers messages when count exceeds 10 logs/minute."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = MagicMock()  # Mock bot or _bot for flush
    
    orig_channel = config.LOGS_CHANNEL_ID
    config.LOGS_CHANNEL_ID = -100555
    
    # Reset state
    log_forwarder._buffer = []
    log_forwarder._window_count = 0
    import time
    log_forwarder._window_start = time.time()
    
    # Temporarily override global _bot and burst limit to avoid side effects
    orig_bot = log_forwarder._bot
    log_forwarder._bot = bot
    orig_burst_count = log_forwarder.BURST_LIMIT_COUNT
    log_forwarder.BURST_LIMIT_COUNT = 99
        
    try:
        # Send 12 logs
        for i in range(12):
            await log_forwarder.send(bot, source_chat_id=123, text=f"log msg {i}")
            
        # First 10 sent immediately, last 2 buffered
        assert bot.send_message.call_count == 10
        assert len(log_forwarder._buffer) == 2
        
        # Flush buffer manually
        await log_forwarder.flush_buffer()
        bot.send_document.assert_called_once()
        assert len(log_forwarder._buffer) == 0
    finally:
        log_forwarder.BURST_LIMIT_COUNT = orig_burst_count
        log_forwarder._bot = orig_bot
        config.LOGS_CHANNEL_ID = orig_channel


@pytest.mark.asyncio
async def test_log_forwarder_clubbing_burst_load():
    """Assert log_forwarder buffers messages when a burst of > 3 logs in 5s occurs."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    
    orig_channel = config.LOGS_CHANNEL_ID
    config.LOGS_CHANNEL_ID = -100555
    
    # Reset state
    log_forwarder._buffer = []
    log_forwarder._window_count = 0
    import time
    log_forwarder._window_start = time.time()
    
    try:
        # Send 5 logs in rapid succession
        for i in range(5):
            await log_forwarder.send(bot, source_chat_id=123, text=f"log msg {i}")
            
        # First 3 sent immediately, remaining 2 buffered (burst limit is 3)
        assert bot.send_message.call_count == 3
        assert len(log_forwarder._buffer) == 2
        assert log_forwarder._clubber_activated is True
    finally:
        config.LOGS_CHANNEL_ID = orig_channel


def test_error_log_sink_filters_permission_errors():
    """Assert make_error_log_sink ignores logs containing permission-related keywords."""
    bot = MagicMock()
    loop = MagicMock()
    
    sink = make_error_log_sink(bot, loop)
    
    orig_channel = config.LOGS_CHANNEL_ID
    config.LOGS_CHANNEL_ID = -100555
    
    try:
        # A normal error: should be processed
        msg_normal = MagicMock()
        msg_normal.record = {
            "level": MagicMock(no=40, name="ERROR"),
            "name": "test_module",
            "function": "test_func",
            "message": "Generic DB connection issue",
            "extra": {}
        }
        sink(msg_normal)
        loop.call_soon_threadsafe.assert_called_once()
        loop.call_soon_threadsafe.reset_mock()
        
        # A forbidden/permission error: should be filtered out
        msg_forbidden = MagicMock()
        msg_forbidden.record = {
            "level": MagicMock(no=40, name="ERROR"),
            "name": "test_module",
            "function": "test_func",
            "message": "Failed to send: Forbidden: bot was blocked by the user",
            "extra": {}
        }
        sink(msg_forbidden)
        loop.call_soon_threadsafe.assert_not_called()
    finally:
        config.LOGS_CHANNEL_ID = orig_channel


@pytest.mark.asyncio
async def test_throttling_middleware_logs_warning():
    """Assert ThrottlingMiddleware logs a warning containing user and chat IDs when throttled."""
    from app.handlers.middlewares import ThrottlingMiddleware
    from aiogram.types import Message

    
    # Save original configs
    original_requests = config.RATE_LIMIT_MAX_REQUESTS
    original_window = config.RATE_LIMIT_WINDOW_SECS
    
    config.RATE_LIMIT_MAX_REQUESTS = 1
    config.RATE_LIMIT_WINDOW_SECS = 1.0
    
    try:
        middleware = ThrottlingMiddleware()
        mock_handler = AsyncMock()
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 12345
        mock_message.from_user.is_bot = False
        mock_message.date = None
        mock_message.chat = MagicMock()
        mock_message.chat.type = "private"
        mock_message.chat.id = 67890
        mock_message.answer = AsyncMock()
        
        # 1st request - should pass
        await middleware(mock_handler, mock_message, {})
        assert mock_handler.call_count == 1
        
        # 2nd request - should block, warn, and log warning
        with patch("app.handlers.middlewares.logger.warning") as mock_log_warning:
            await middleware(mock_handler, mock_message, {})
            assert mock_handler.call_count == 1
            mock_log_warning.assert_called_once()
            log_msg = mock_log_warning.call_args[0][0]
            assert "12345" in log_msg
            assert "67890" in log_msg
            assert "throttled" in log_msg
    finally:
        config.RATE_LIMIT_MAX_REQUESTS = original_requests
        config.RATE_LIMIT_WINDOW_SECS = original_window


def test_format_extraction_summary():
    """Assert _format_extraction_summary formats updates and profiles correctly."""
    from app.services.memory_extractor import _format_extraction_summary
    from app.services.schemas import MemoryExtraction, FactExtract, ProfileUpdate, EmotionLog

    # 1. Empty updates -> empty string
    empty_ext = MemoryExtraction()
    assert _format_extraction_summary(empty_ext) == ""

    # 2. profile updates
    prof_ext = MemoryExtraction(profile_updates=ProfileUpdate(communication_style="chill", gender="male"))
    summary = _format_extraction_summary(prof_ext)
    assert "profile (communication style, gender)" in summary

    # 3. facts and emotional state
    fact_ext = MemoryExtraction(
        new_facts=[FactExtract(category="personal", content="likes tea")],
        emotional_state=EmotionLog(mood="happy", intensity=0.9)
    )
    summary = _format_extraction_summary(fact_ext)
    assert "facts (+1 new)" in summary
    assert "mood: happy (intensity: 0.9)" in summary


