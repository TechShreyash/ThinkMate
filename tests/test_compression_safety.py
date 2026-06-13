"""Tests for hardening items H2 and H3 (memory compression safety + efficiency).

H2 — a failed compression must never wipe existing memory.
H3 — budget enforcement must finish under budget using a single read + single write,
     dropping the lowest-priority items first (oldest events -> beliefs -> facts).
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import config
from app.database import connection, models
from app.services import memory_compressor
from app.services.memory_compressor import _enforce_budget, compress_user_memory
from app.services.memory_loader import build_memory_block


async def _seed_profile(db, user_id, *, n_facts=0, n_beliefs=0, n_events=0):
    """Create a profile padded with n_* long items in each category."""
    await models.ensure_user(db, user_id, "u", "U")
    facts = [
        {"category": "personal", "content": f"Fact number {i}: " + "x" * 80,
         "confidence": 1.0}
        for i in range(n_facts)
    ]
    beliefs = [
        {"content": f"Belief number {i}: " + "y" * 80} for i in range(n_beliefs)
    ]
    events = [
        {"description": f"Event number {i}: " + "z" * 80, "event_date": None,
         "significance": "minor", "emotional_context": ""}
        for i in range(n_events)
    ]
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"facts": facts, "beliefs": beliefs, "events": events}},
    )


@pytest.mark.asyncio
async def test_failed_compression_preserves_memory():
    """When compress_memory returns None (failure), existing memory is left untouched."""
    async with connection.db_session() as db:
        user_id = 90001
        await _seed_profile(db, user_id, n_facts=3, n_beliefs=2, n_events=2)

        before = await db["user_profiles"].find_one({"_id": user_id})

        with patch.object(
            memory_compressor.llm_service, "compress_memory",
            new=AsyncMock(return_value=None),
        ):
            await compress_user_memory(user_id)

        after = await db["user_profiles"].find_one({"_id": user_id})

    # Memory is fully preserved: nothing was replaced or trimmed.
    assert [f["content"] for f in after["facts"]] == [f["content"] for f in before["facts"]]
    assert [b["content"] for b in after["beliefs"]] == [b["content"] for b in before["beliefs"]]
    assert [e["description"] for e in after["events"]] == [e["description"] for e in before["events"]]


@pytest.mark.asyncio
async def test_enforce_budget_ends_under_budget_in_single_write():
    """_enforce_budget brings the profile under budget with exactly one DB write."""
    original_budget = config.USER_MEMORY_BUDGET_CHARS
    config.USER_MEMORY_BUDGET_CHARS = 500
    try:
        async with connection.db_session() as db:
            user_id = 90002
            await _seed_profile(db, user_id, n_facts=10, n_beliefs=10, n_events=10)

            # Sanity: profile starts well over budget.
            _, needs = await build_memory_block(db, user_id)
            assert needs

            underlying = db["user_profiles"]._collection
            orig_update = underlying.update_one
            writes = {"n": 0}

            def counting_update(*args, **kwargs):
                writes["n"] += 1
                return orig_update(*args, **kwargs)

            with patch.object(underlying, "update_one", side_effect=counting_update):
                await _enforce_budget(db, user_id)

            # Exactly one write performed.
            assert writes["n"] == 1

            # Profile now fits the budget.
            text, needs_after = await build_memory_block(db, user_id)
            assert not needs_after
            assert len(text) <= config.USER_MEMORY_BUDGET_CHARS
    finally:
        config.USER_MEMORY_BUDGET_CHARS = original_budget


@pytest.mark.asyncio
async def test_enforce_budget_drops_events_before_facts():
    """Lowest-priority items (events) are shed before higher-priority facts."""
    original_budget = config.USER_MEMORY_BUDGET_CHARS
    config.USER_MEMORY_BUDGET_CHARS = 600
    try:
        async with connection.db_session() as db:
            user_id = 90003
            await _seed_profile(db, user_id, n_facts=5, n_beliefs=5, n_events=5)

            await _enforce_budget(db, user_id)

            doc = await db["user_profiles"].find_one({"_id": user_id})
            # Events are dropped first, so by the time any fact is removed every event
            # and belief must already be gone.
            if len(doc["facts"]) < 5:
                assert doc["events"] == []
                assert doc["beliefs"] == []
    finally:
        config.USER_MEMORY_BUDGET_CHARS = original_budget


@pytest.mark.asyncio
async def test_enforce_budget_no_write_when_already_under_budget():
    """Already-compliant profiles trigger no write at all."""
    async with connection.db_session() as db:
        user_id = 90004
        await _seed_profile(db, user_id, n_facts=1, n_beliefs=1, n_events=1)

        underlying = db["user_profiles"]._collection
        orig_update = underlying.update_one
        writes = {"n": 0}

        def counting_update(*args, **kwargs):
            writes["n"] += 1
            return orig_update(*args, **kwargs)

        with patch.object(underlying, "update_one", side_effect=counting_update):
            await _enforce_budget(db, user_id)

        assert writes["n"] == 0
