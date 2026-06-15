import os
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message
from app.config import config
from app.database import connection, models
import app.services.memory_compressor
import app.services.memory_extractor
from app.services.user_task_manager import user_task_manager
from app.services.chat_manager import handle_message

@pytest_asyncio.fixture
async def temp_db():
    await connection.init_db()
    yield

@pytest.mark.asyncio
async def test_message_batching_delay(temp_db):
    user_id = 99999
    
    # Save original config
    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    config.MESSAGE_BATCH_DELAY_SECS = 0.2
    
    try:
        mock_bot = MagicMock()
        mock_message_1 = MagicMock()
        mock_message_1.chat.id = 123
        mock_message_1.answer = AsyncMock()
        
        mock_message_2 = MagicMock()
        mock_message_2.chat.id = 123
        mock_message_2.answer = AsyncMock()
        
        # Mock handle_message
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_handle.return_value = ("Mocked Response", None)

            # Enqueue first message
            await user_task_manager.enqueue_message(mock_bot, user_id, "Hello", mock_message_1)
            await asyncio.sleep(0.05)
            
            # Enqueue second message (resets timer)
            await user_task_manager.enqueue_message(mock_bot, user_id, "World", mock_message_2)
            
            # Wait for batch delay to expire and execute
            await asyncio.sleep(0.3)
            
            # Verify they were batched together
            mock_handle.assert_called_once()
            # The database connection parameter is injected in the batch run, so check second arg
            assert mock_handle.call_args[0][1] == user_id
            assert mock_handle.call_args[0][2] == "Hello\nWorld"
            
            # Answer should be sent to the last message of the batch
            mock_message_2.answer.assert_called_once_with("Mocked Response")
            mock_message_1.answer.assert_not_called()
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay

@pytest.mark.asyncio
async def test_character_count_extraction_trigger(temp_db):
    user_id = 88888
    
    # Lower max buffer chars threshold to trigger memory extraction
    original_max_chars = config.CHAT_BUFFER_MAX_CHARS
    config.CHAT_BUFFER_MAX_CHARS = 100
    
    try:
        async with connection.db_session() as db:
            await models.ensure_user(db, user_id, "testuser", "Test User")
            
            # Insert short messages (should not trigger extraction)
            with patch("app.services.user_task_manager.user_task_manager.run_extractor", new_callable=AsyncMock) as mock_run_extractor:
                with patch("app.services.llm_service.LLMService.generate_reply_bundle", new_callable=AsyncMock) as mock_response:
                    mock_response.return_value = ("Fine, thanks.", None)
                    
                    # 1. Total chars ~ 20 (Hello + Fine, thanks)
                    await handle_message(db, user_id, "Hello")
                    await asyncio.sleep(0.01)
                    mock_run_extractor.assert_not_called()
                    
                    # 2. Insert a very long message to breach 100 char limit
                    long_msg = "This is a very long text message designed to exceed the character limit trigger set in config."
                    await handle_message(db, user_id, long_msg)
                    await asyncio.sleep(0.01)
                    mock_run_extractor.assert_called_once()
    finally:
        config.CHAT_BUFFER_MAX_CHARS = original_max_chars
 
@pytest.mark.asyncio
async def test_memory_extraction_excludes_latest_trim(temp_db):
    user_id = 77777
    
    # Save original config
    original_trim = config.CHAT_BUFFER_TRIM
    config.CHAT_BUFFER_TRIM = 3
    
    try:
        async with connection.db_session() as db:
            await models.ensure_user(db, user_id, "trimuser", "Trim User")
            
            # Add 8 messages to the buffer
            for i in range(8):
                role = "user" if i % 2 == 0 else "assistant"
                await models.add_message_to_buffer(db, user_id, role, f"Msg {i}")
                
            # Perform extraction
            with patch("app.services.llm_service.LLMService.extract_memory", new_callable=AsyncMock) as mock_extract_llm:
                from app.services.schemas import MemoryExtraction
                mock_extract_llm.return_value = MemoryExtraction()
                
                from app.services.memory_extractor import extract_and_trim
                await extract_and_trim(user_id)
                
                # Check what was passed to extract_memory
                # It should take buffer messages except the latest CHAT_BUFFER_TRIM (3 messages).
                # Total 8 messages. Minus latest 3 means it extracts oldest 5 messages ("Msg 0" to "Msg 4").
                called_text = mock_extract_llm.call_args[1]["user_history_text"]
                assert "Msg 0" in called_text
                assert "Msg 4" in called_text
                assert "Msg 5" not in called_text
                assert "Msg 7" not in called_text
                
                # Check that buffer now has exactly keep_count (3) messages left
                remaining_count = await models.get_buffer_count(db, user_id)
                assert remaining_count == 3
                
                # Remaining should be the latest 3: Msg 5, Msg 6, Msg 7
                remaining_messages = await models.get_chat_buffer(db, user_id)
                contents = [m["content"] for m in remaining_messages]
                assert contents == ["Msg 5", "Msg 6", "Msg 7"]
    finally:
        config.CHAT_BUFFER_TRIM = original_trim

@pytest.mark.asyncio
async def test_concurrent_compressor_lock(temp_db):
    user_id = 66666
    
    # Test that run_compressor serializes/ignores concurrent compressor tasks
    with patch("app.services.memory_compressor.compress_user_memory", new_callable=AsyncMock) as mock_compress:
        # Define mock compressor behavior to take some time
        async def slow_compress(uid):
            await asyncio.sleep(0.2)
        mock_compress.side_effect = slow_compress
        
        # Trigger compressor
        task1 = asyncio.create_task(user_task_manager.run_compressor(user_id))
        await asyncio.sleep(0.05)
        
        # Trigger again (should be skipped because task1 is running)
        await user_task_manager.run_compressor(user_id)
        
        await task1
        
        # Only call once since the second one was skipped due to lock
        mock_compress.assert_called_once_with(user_id)

@pytest.mark.asyncio
async def test_max_batch_delay_prevents_infinite_postponement(temp_db):
    user_id = 55555
    
    # Save original configs
    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    original_max_delay = config.MAX_BATCH_DELAY_SECS
    
    config.MESSAGE_BATCH_DELAY_SECS = 0.2
    config.MAX_BATCH_DELAY_SECS = 0.4
    
    try:
        mock_bot = MagicMock()
        mock_message = MagicMock()
        mock_message.chat.id = 123
        mock_message.answer = AsyncMock()
        
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_handle.return_value = ("Mocked Response", None)

            # Message 1 at t=0
            await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 1", mock_message)
            await asyncio.sleep(0.15)
            
            # Message 2 at t=0.15 (would postpone to t=0.35)
            await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 2", mock_message)
            await asyncio.sleep(0.15)
            
            # Message 3 at t=0.30 (would postpone to t=0.50)
            await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 3", mock_message)
            await asyncio.sleep(0.15)
            
            # Message 4 at t=0.45 (would postpone to t=0.65, but exceeds max delay of 0.4)
            # This should trigger immediately
            await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 4", mock_message)
            
            # Give a tiny slice of time for execution
            await asyncio.sleep(0.05)
            
            # Verify it was called immediately upon Msg 4 arrival
            mock_handle.assert_called_once()
            assert mock_handle.call_args[0][2] == "Msg 1\nMsg 2\nMsg 3\nMsg 4"
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay
        config.MAX_BATCH_DELAY_SECS = original_max_delay

@pytest.mark.asyncio
async def test_throttling_middleware():
    from app.handlers.middlewares import ThrottlingMiddleware
    
    # Save original configs
    original_requests = config.RATE_LIMIT_MAX_REQUESTS
    original_window = config.RATE_LIMIT_WINDOW_SECS
    
    config.RATE_LIMIT_MAX_REQUESTS = 2
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
        mock_message.answer = AsyncMock()
        
        # 1st request - should pass
        await middleware(mock_handler, mock_message, {})
        assert mock_handler.call_count == 1
        
        # 2nd request - should pass
        await middleware(mock_handler, mock_message, {})
        assert mock_handler.call_count == 2
        
        # 3rd request - should block and warn
        await middleware(mock_handler, mock_message, {})
        assert mock_handler.call_count == 2  # Handled call count doesn't change
        mock_message.answer.assert_called_once()
        
        # 4th request - should block silently (no more answer warnings)
        mock_message.answer.reset_mock()
        await middleware(mock_handler, mock_message, {})
        assert mock_handler.call_count == 2
        mock_message.answer.assert_not_called()
    finally:
        config.RATE_LIMIT_MAX_REQUESTS = original_requests
        config.RATE_LIMIT_WINDOW_SECS = original_window


@pytest.mark.asyncio
async def test_throttling_middleware_ignores_other_bots():
    """Messages authored by other bots are dropped entirely: no handler, no warning.

    Regression guard for the bot-to-bot loop where a second bot in the same chat got
    rate-limited like a human and flooded the chat with repeated "Slow down!" warnings.
    """
    from app.handlers.middlewares import ThrottlingMiddleware

    original_requests = config.RATE_LIMIT_MAX_REQUESTS
    original_window = config.RATE_LIMIT_WINDOW_SECS
    config.RATE_LIMIT_MAX_REQUESTS = 2
    config.RATE_LIMIT_WINDOW_SECS = 1.0

    try:
        middleware = ThrottlingMiddleware()
        mock_handler = AsyncMock()
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 99999
        mock_message.from_user.is_bot = True
        mock_message.date = None
        mock_message.answer = AsyncMock()

        # Even far past the limit, a bot sender never reaches the handler and is
        # never warned.
        for _ in range(10):
            await middleware(mock_handler, mock_message, {})

        mock_handler.assert_not_called()
        mock_message.answer.assert_not_called()
    finally:
        config.RATE_LIMIT_MAX_REQUESTS = original_requests
        config.RATE_LIMIT_WINDOW_SECS = original_window


@pytest.mark.asyncio
async def test_throttling_middleware_drops_stale_backlog():
    """A message older than STALE_MESSAGE_SECS is dropped: no handler, no warning.

    Regression for the startup flood where a backlog of old messages was processed at
    once, each stamped with the current time, tripping the rate limit for many users
    and spamming "Slow down" warnings.
    """
    from datetime import datetime, timezone, timedelta
    from app.handlers.middlewares import ThrottlingMiddleware

    original_stale = config.STALE_MESSAGE_SECS
    config.STALE_MESSAGE_SECS = 60.0
    try:
        middleware = ThrottlingMiddleware()
        mock_handler = AsyncMock()

        # A clearly-old message (10 minutes ago) must be dropped outright.
        stale = MagicMock(spec=Message)
        stale.from_user = MagicMock()
        stale.from_user.id = 555
        stale.from_user.is_bot = False
        stale.date = datetime.now(timezone.utc) - timedelta(minutes=10)
        stale.answer = AsyncMock()

        await middleware(mock_handler, stale, {})
        mock_handler.assert_not_called()
        stale.answer.assert_not_called()

        # A fresh message (now) passes straight through to the handler.
        fresh = MagicMock(spec=Message)
        fresh.from_user = MagicMock()
        fresh.from_user.id = 555
        fresh.from_user.is_bot = False
        fresh.date = datetime.now(timezone.utc)
        fresh.chat = MagicMock()
        fresh.chat.type = "private"
        fresh.answer = AsyncMock()

        await middleware(mock_handler, fresh, {})
        mock_handler.assert_called_once()
    finally:
        config.STALE_MESSAGE_SECS = original_stale


@pytest.mark.asyncio
async def test_throttling_middleware_limits_groups_silently_without_warning():
    """In a group the per-user limit still drops excess, but NO public warning is sent.

    Regression for the flood where the bot publicly scolded ordinary group chatter. The
    per-user rate limit applies everywhere (a single user can't flood the bot); only the
    user-facing "Slow down" warning is suppressed in groups.
    """
    from app.handlers.middlewares import ThrottlingMiddleware

    original_requests = config.RATE_LIMIT_MAX_REQUESTS
    original_window = config.RATE_LIMIT_WINDOW_SECS
    config.RATE_LIMIT_MAX_REQUESTS = 2
    config.RATE_LIMIT_WINDOW_SECS = 1.0
    try:
        middleware = ThrottlingMiddleware()
        mock_handler = AsyncMock()
        mock_message = MagicMock(spec=Message)
        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 777
        mock_message.from_user.is_bot = False
        mock_message.date = None
        mock_message.chat = MagicMock()
        mock_message.chat.type = "supergroup"
        mock_message.answer = AsyncMock()

        # 10 rapid messages from one user in a group: the first two pass, the rest are
        # dropped silently — and crucially, no "Slow down" warning is ever posted.
        for _ in range(10):
            await middleware(mock_handler, mock_message, {})

        assert mock_handler.call_count == 2, "per-user limit must still drop excess in groups"
        mock_message.answer.assert_not_called()
    finally:
        config.RATE_LIMIT_MAX_REQUESTS = original_requests
        config.RATE_LIMIT_WINDOW_SECS = original_window


@pytest.mark.asyncio
async def test_throttling_middleware_burst_catchup_no_false_positive():
    """Messages SENT seconds apart but processed together must not trip the limiter.

    Simulates a catch-up burst: three messages whose send times are 5s apart, all
    delivered and processed at the same instant. Because the sliding window uses each
    message's real send time (``message.date``) instead of the processing time, they stay
    spread across the window and none is falsely throttled. Under the old processing-time
    logic all three collapsed onto one instant and tripped the "Slow down" warning.
    """
    from datetime import datetime, timezone, timedelta
    from app.handlers.middlewares import ThrottlingMiddleware

    original_requests = config.RATE_LIMIT_MAX_REQUESTS
    original_window = config.RATE_LIMIT_WINDOW_SECS
    original_stale = config.STALE_MESSAGE_SECS
    config.RATE_LIMIT_MAX_REQUESTS = 2
    config.RATE_LIMIT_WINDOW_SECS = 10.0
    config.STALE_MESSAGE_SECS = 300.0  # generous, so the spaced sends aren't dropped as stale
    try:
        middleware = ThrottlingMiddleware()
        mock_handler = AsyncMock()
        base = datetime.now(timezone.utc) - timedelta(seconds=10)
        sent = []
        for i in range(3):  # send times: base, base+5s, base+10s
            msg = MagicMock(spec=Message)
            msg.from_user = MagicMock()
            msg.from_user.id = 333
            msg.from_user.is_bot = False
            msg.chat = MagicMock()
            msg.chat.type = "private"
            msg.date = base + timedelta(seconds=5 * i)
            msg.answer = AsyncMock()
            await middleware(mock_handler, msg, {})
            sent.append(msg)

        # All three reached the handler (none throttled) and no warning was ever sent.
        assert mock_handler.call_count == 3
        for msg in sent:
            msg.answer.assert_not_called()
    finally:
        config.RATE_LIMIT_MAX_REQUESTS = original_requests
        config.RATE_LIMIT_WINDOW_SECS = original_window
        config.STALE_MESSAGE_SECS = original_stale


    user_id = 44444
    
    # Save original configs
    original_max_queued = config.MAX_QUEUED_MESSAGES
    config.MAX_QUEUED_MESSAGES = 2
    
    try:
        mock_bot = MagicMock()
        mock_message = MagicMock()
        mock_message.chat.id = 123
        mock_message.answer = AsyncMock()
        
        # Force a pending task to avoid clearing queue too fast
        state = await user_task_manager.get_state(user_id)
        state.pending_messages.clear()
        
        # Enqueue Msg 1
        await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 1", mock_message)
        # Enqueue Msg 2
        await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 2", mock_message)
        # Enqueue Msg 3 (exceeds limit of 2)
        await user_task_manager.enqueue_message(mock_bot, user_id, "Msg 3", mock_message)
        
        # Verify queue has only 2 messages
        assert len(state.pending_messages) == 2
        assert state.pending_messages[0]["text"] == "Msg 1"
        assert state.pending_messages[1]["text"] == "Msg 2"
        
        # Cleanup batch task to avoid errors during test teardown
        if state.batch_task:
            state.batch_task.cancel()
    finally:
        config.MAX_QUEUED_MESSAGES = original_max_queued


