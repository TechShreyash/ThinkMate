"""Regression tests for the production-hardening fixes (see docs/development/hardening_plan.md)."""
import time
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from app.config import config
from app.database import connection, models
from app.services.memory_loader import build_memory_block
from app.services.memory_compressor import _enforce_budget
from app.services.user_task_manager import UserTaskManager
from app.services.schemas import MemoryExtraction, FactExtract


@pytest.mark.asyncio
async def test_atomic_trim_preserves_concurrent_appends():
    """The bug: a stale read-slice-overwrite trim clobbered messages appended during it.

    Here we trim the 3 oldest based on an 8-message snapshot, but 2 more messages arrive
    first. An atomic $pull must keep those new messages (the old code would have lost them).
    """
    async with connection.db_session() as db:
        user_id = 70001
        for i in range(8):
            await models.add_message_to_buffer(db, user_id, "user", f"m{i}")

        snapshot = await models.get_chat_buffer(db, user_id)
        trim_size = len(snapshot) - 5  # keep latest 5 -> trim 3 oldest

        # Concurrent arrivals between the snapshot and the trim.
        await models.add_message_to_buffer(db, user_id, "user", "m8")
        await models.add_message_to_buffer(db, user_id, "user", "m9")

        await models.delete_oldest_buffer_messages(db, user_id, trim_size)

        remaining = [m["content"] for m in await models.get_chat_buffer(db, user_id)]
        assert remaining == ["m3", "m4", "m5", "m6", "m7", "m8", "m9"]


@pytest.mark.asyncio
async def test_buffer_hard_cap():
    """The messages array never grows past CHAT_BUFFER_HARD_CAP."""
    original = config.CHAT_BUFFER_HARD_CAP
    config.CHAT_BUFFER_HARD_CAP = 5
    try:
        async with connection.db_session() as db:
            user_id = 70002
            for i in range(12):
                await models.add_message_to_buffer(db, user_id, "user", f"m{i}")
            buf = await models.get_chat_buffer(db, user_id)
            assert len(buf) == 5
            assert buf[-1]["content"] == "m11"  # newest retained
    finally:
        config.CHAT_BUFFER_HARD_CAP = original


@pytest.mark.asyncio
async def test_normalized_dedup_on_extraction():
    """Facts differing only by case/whitespace are not duplicated."""
    async with connection.db_session() as db:
        user_id = 70003
        await models.ensure_user(db, user_id, "u", "U")
        await models.save_extracted_memories(
            db, user_id, MemoryExtraction(new_facts=[FactExtract(category="preference", content="Enjoys green tea")])
        )
        await models.save_extracted_memories(
            db, user_id, MemoryExtraction(new_facts=[FactExtract(category="preference", content="  ENJOYS   green   tea ")])
        )
        facts = await models.get_active_facts(db, user_id)
        assert len(facts) == 1


@pytest.mark.asyncio
async def test_reset_user_wipes_state():
    async with connection.db_session() as db:
        user_id = 70004
        await models.ensure_user(db, user_id, "u", "U")
        await models.add_message_to_buffer(db, user_id, "user", "hi")
        await models.reset_user(db, user_id)
        assert await db["user_profiles"].find_one({"_id": user_id}) is None
        assert await db["chat_buffers"].find_one({"_id": user_id}) is None


@pytest.mark.asyncio
async def test_enforce_budget_terminates_under_budget():
    # Budget must sit above the empty-template floor (~380 chars of section headers).
    original = config.USER_MEMORY_BUDGET_CHARS
    config.USER_MEMORY_BUDGET_CHARS = 800
    try:
        async with connection.db_session() as db:
            user_id = 70005
            await models.ensure_user(db, user_id, "u", "U")
            facts = [{"category": "personal", "content": f"Fact number {i} about the user here."} for i in range(40)]
            await db["user_profiles"].update_one({"_id": user_id}, {"$set": {"facts": facts}})

            _, over = await build_memory_block(db, user_id)
            assert over  # precondition: over budget

            await _enforce_budget(db, user_id)
            _, still_over = await build_memory_block(db, user_id)
            assert not still_over
    finally:
        config.USER_MEMORY_BUDGET_CHARS = original


@pytest.mark.asyncio
async def test_compression_cooldown_skips_recent():
    """run_compressor must skip when a compression ran within the cooldown window."""
    mgr = UserTaskManager()
    user_id = 70006
    with patch("app.services.memory_compressor.compress_user_memory", new_callable=AsyncMock) as mock_compress:
        state = await mgr.get_state(user_id)
        state.last_compression_time = time.time()  # just compressed
        await mgr.run_compressor(user_id)
        mock_compress.assert_not_called()

        state.last_compression_time = 0.0  # cooldown elapsed
        await mgr.run_compressor(user_id)
        mock_compress.assert_called_once_with(user_id)


@pytest.mark.asyncio
async def test_idle_state_eviction():
    mgr = UserTaskManager()
    original = config.USER_STATE_TTL_SECS
    config.USER_STATE_TTL_SECS = 0.01
    try:
        state = await mgr.get_state(91001)
        state.last_active = time.time() - 1  # idle, past TTL
        await asyncio.sleep(0.02)
        await mgr._evict_idle()
        assert 91001 not in mgr._states
    finally:
        config.USER_STATE_TTL_SECS = original
