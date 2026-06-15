"""Instrumentation tests: hot-path metrics increment the expected names.

Covers Requirements 2.1-2.6 and 7.5 of the observability spec: a throttle drop bumps
``throttle.drops``, a queue-cap drop bumps ``queue.drops``, the ``conversations.active``
gauge tracks ``len(_states)``, an LLM call bumps its per-type counter + latency timer, and
background extraction/compression bump their run counters.

All tests use mongomock + pytest-asyncio per ``tests/conftest.py``. The LLM is patched (no
network); an autouse fixture resets the registry around each test for isolation.
"""
import asyncio

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message

from app.config import config
from app.database import connection, models
from app.services.metrics import metrics


@pytest.fixture(autouse=True)
def reset_metrics():
    """Isolate metric state from other tests (Requirement 7.5)."""
    metrics.reset()
    yield
    metrics.reset()


@pytest_asyncio.fixture
async def temp_db():
    await connection.init_db()
    yield


# --------------------------------------------------------------------------- #
# 1. Throttle drop increments throttle.drops (Requirement 2.3)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_throttle_drop_increments_counter():
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
        mock_message.from_user.id = 12345
        mock_message.from_user.is_bot = False
        mock_message.date = None
        mock_message.chat = MagicMock()
        mock_message.chat.type = "private"
        mock_message.answer = AsyncMock()

        # First two requests pass; the next three are dropped past the limit.
        for _ in range(2):
            await middleware(mock_handler, mock_message, {})
        assert mock_handler.call_count == 2
        assert "throttle.drops" not in metrics.snapshot()["counters"]

        expected_drops = 3
        for _ in range(expected_drops):
            await middleware(mock_handler, mock_message, {})

        # Handler never ran again, and every drop was counted.
        assert mock_handler.call_count == 2
        assert metrics.snapshot()["counters"]["throttle.drops"] == expected_drops
    finally:
        config.RATE_LIMIT_MAX_REQUESTS = original_requests
        config.RATE_LIMIT_WINDOW_SECS = original_window


# --------------------------------------------------------------------------- #
# 2. Queue-cap drop increments queue.drops (Requirement 2.4)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_queue_drop_increments_counter(temp_db):
    from app.services.user_task_manager import UserTaskManager

    user_id = 44444
    original_max_queued = config.MAX_QUEUED_MESSAGES
    original_batch_delay = config.MESSAGE_BATCH_DELAY_SECS
    original_max_batch_delay = config.MAX_BATCH_DELAY_SECS
    config.MAX_QUEUED_MESSAGES = 2
    # Keep the batch from draining the queue mid-test so drops are deterministic.
    config.MESSAGE_BATCH_DELAY_SECS = 30.0
    config.MAX_BATCH_DELAY_SECS = 30.0

    mgr = UserTaskManager()
    state = None
    try:
        mock_bot = MagicMock()
        mock_message = MagicMock()
        mock_message.chat.id = 123
        mock_message.answer = AsyncMock()

        with patch(
            "app.services.user_task_manager.handle_message", new_callable=AsyncMock
        ) as mock_handle:
            mock_handle.return_value = ("Mocked Response", None)

            # Two messages fill the queue; the next three breach the cap and drop.
            for i in range(2):
                await mgr.enqueue_message(mock_bot, user_id, f"Fill {i}", mock_message)
            state = await mgr.get_state(user_id)
            assert len(state.pending_messages) == 2
            assert "queue.drops" not in metrics.snapshot()["counters"]

            expected_drops = 3
            for i in range(expected_drops):
                await mgr.enqueue_message(mock_bot, user_id, f"Drop {i}", mock_message)

            assert len(state.pending_messages) == 2  # never grew past the cap
            assert metrics.snapshot()["counters"]["queue.drops"] == expected_drops
    finally:
        if state is not None:
            state.pending_messages.clear()
            if state.batch_task:
                state.batch_task.cancel()
            if state.typing_task:
                state.typing_task.cancel()
        config.MAX_QUEUED_MESSAGES = original_max_queued
        config.MESSAGE_BATCH_DELAY_SECS = original_batch_delay
        config.MAX_BATCH_DELAY_SECS = original_max_batch_delay


# --------------------------------------------------------------------------- #
# 3. Active-conversation gauge tracks len(_states) (Requirement 2.5)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_active_gauge_tracks_state():
    from app.services.user_task_manager import UserTaskManager

    mgr = UserTaskManager()

    # Creating a state sets the gauge to the live state count.
    await mgr.get_state(123)
    assert metrics.snapshot()["gauges"]["conversations.active"] == len(mgr._states) == 1

    # A second conversation moves the gauge with it.
    await mgr.get_state(456)
    assert metrics.snapshot()["gauges"]["conversations.active"] == len(mgr._states) == 2

    # Idle + stale states are evicted, and the gauge drops to match.
    for st in mgr._states.values():
        st.last_active = 0.0  # far enough in the past to be stale
    await mgr._evict_idle()
    assert metrics.snapshot()["gauges"]["conversations.active"] == len(mgr._states) == 0


# --------------------------------------------------------------------------- #
# 4. LLM call increments per-type counter + latency timer (Requirements 2.1, 2.2)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_llm_reply_call_counts_and_times(temp_db):
    """Exercise the REAL ``generate_reply_bundle`` instrumentation.

    We patch only the underlying OpenAI client call
    (``llm_service.client.chat.completions.create``) so the real method body runs and calls
    ``metrics.record_llm("chat_reply", ...)`` on its success path. ``record_llm`` maps
    ``chat_reply`` -> the ``llm.reply.*`` metric family.
    """
    from app.services.llm_service import llm_service

    fake_message = MagicMock()
    fake_message.content = '{"reply": "hi", "reaction": ""}'
    fake_choice = MagicMock()
    fake_choice.message = fake_message
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]

    with patch.object(
        llm_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = fake_response

        reply, reaction = await llm_service.generate_reply_bundle(1, "sys", [])

    assert reply == "hi"
    assert reaction is None  # reactions disabled by the conftest fixture

    snap = metrics.snapshot()
    assert snap["counters"]["llm.reply.calls"] == 1
    assert snap["counters"]["llm.reply.success"] == 1
    assert "llm.reply.failure" not in snap["counters"]
    assert snap["timers"]["llm.reply.latency"]["count"] == 1

    # Let the fire-and-forget audit-log task settle so it doesn't outlive the test.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_llm_reply_failure_counts_failure(temp_db):
    """A raised LLM call records the failure split, not success."""
    from app.services.llm_service import llm_service

    with patch.object(
        llm_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.side_effect = ValueError("boom")

        with pytest.raises(ValueError):
            await llm_service.generate_reply_bundle(1, "sys", [])

    snap = metrics.snapshot()
    assert snap["counters"]["llm.reply.calls"] == 1
    assert snap["counters"]["llm.reply.failure"] == 1
    assert "llm.reply.success" not in snap["counters"]
    assert snap["timers"]["llm.reply.latency"]["count"] == 1

    await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# 5. Extraction / compression run counters (Requirement 2.6)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_extraction_run_increments_counter(temp_db):
    from app.services.llm_service import llm_service
    from app.services.memory_extractor import extract_and_trim
    from app.services.schemas import MemoryExtraction

    user_id = 33333
    original_trim = config.CHAT_BUFFER_TRIM
    config.CHAT_BUFFER_TRIM = 3

    try:
        async with connection.db_session() as db:
            await models.ensure_user(db, user_id, "extractuser", "Extract User")
            # Seed a single-party (DM) buffer with more than keep_count messages.
            for i in range(8):
                role = "user" if i % 2 == 0 else "assistant"
                await models.add_message_to_buffer(db, user_id, role, f"Msg {i}")

        with patch.object(
            llm_service, "extract_memory", new_callable=AsyncMock
        ) as mock_extract:
            mock_extract.return_value = MemoryExtraction()

            await extract_and_trim(user_id)

        assert metrics.snapshot()["counters"]["extraction.runs"] == 1
    finally:
        config.CHAT_BUFFER_TRIM = original_trim


@pytest.mark.asyncio
async def test_compression_run_increments_counter(temp_db):
    from app.services.llm_service import llm_service
    from app.services.memory_compressor import compress_user_memory

    user_id = 22222

    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "compressuser", "Compress User")

    with patch.object(
        llm_service, "compress_memory", new_callable=AsyncMock
    ) as mock_compress:
        # None => failed/short-circuit compression; the run still counts (Requirement 2.6).
        mock_compress.return_value = None

        await compress_user_memory(user_id)

    assert metrics.snapshot()["counters"]["compression.runs"] == 1
