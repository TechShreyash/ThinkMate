"""Tests for the Phase 11 consolidation run, insights rendering, and serialization.

Covers Requirements 2.4, 2.5, 3.4, 3.5, 9.3, 9.4 of the consolidation spec:

* ``consolidate_user_memory`` applies a valid result, advances ``last_consolidated_at``,
  and leaves the profile under ``USER_MEMORY_BUDGET_CHARS`` (the reused ``_enforce_budget``).
* The never-wipe contract: a ``None`` result (or a raising LLM) never writes/clears memory
  and never advances ``last_consolidated_at``, and the run never raises into the scheduler.
* ``compile_memory_text`` renders the ``=== BEHAVIORAL INSIGHTS ===`` section with items and
  an empty placeholder, and is robust to a profile with no ``insights`` key.
* ``run_consolidator`` runs under ``memory_lock`` and skips when the lock is already held.

All tests use mongomock + pytest-asyncio per ``tests/conftest.py``. The LLM is patched with
``AsyncMock`` (no network); an autouse ``metrics.reset()`` fixture isolates metric state
(as in ``tests/test_metrics_instrumentation.py``).
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import config
from app.database import connection, models
from app.services.metrics import metrics
from app.services.memory_loader import compile_memory_text, build_memory_block
from app.services.user_task_manager import UserTaskManager
from app.services.schemas import (
    MemoryConsolidation,
    ConsolidatedInsight,
    CompressedFact,
    CompressedBelief,
    CompressedEvent,
)


@pytest.fixture(autouse=True)
def reset_metrics():
    """Isolate metric state from other tests (mirrors test_metrics_instrumentation)."""
    metrics.reset()
    yield
    metrics.reset()


def _valid_consolidation() -> MemoryConsolidation:
    """A small, coherent consolidation result that stays well under any budget."""
    return MemoryConsolidation(
        profile_summary="A consolidated summary of the user.",
        communication_style="Warm and direct.",
        consolidated_facts=[
            CompressedFact(category="personal", content="Lives in Berlin."),
            CompressedFact(category="work", content="Works as a designer."),
        ],
        consolidated_beliefs=[
            CompressedBelief(content="Values craftsmanship over speed."),
        ],
        consolidated_events=[
            CompressedEvent(description="Moved cities last year.", significance="major"),
        ],
        insights=[
            ConsolidatedInsight(content="Tends to get stressed during deadlines; values reassurance then."),
            ConsolidatedInsight(content="Processes setbacks by withdrawing first, then talking."),
        ],
    )


# --------------------------------------------------------------------------- #
# A. Successful run: applied, timestamped, under budget, metrics incremented.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_consolidation_success_applies_and_enforces_budget():
    from app.services.memory_consolidator import consolidate_user_memory

    user_id = 80001
    original_budget = config.USER_MEMORY_BUDGET_CHARS
    config.USER_MEMORY_BUDGET_CHARS = 800  # force the seeded profile over budget
    try:
        async with connection.db_session() as db:
            await models.ensure_user(db, user_id, "u", "U")
            # Seed an OVER-BUDGET profile (many facts).
            facts = [
                {"category": "personal", "content": f"Fact number {i} about the user here."}
                for i in range(40)
            ]
            await db["user_profiles"].update_one(
                {"_id": user_id}, {"$set": {"facts": facts}}
            )
            _, over_before = await build_memory_block(db, user_id)
            assert over_before  # precondition: over budget

        with patch(
            "app.services.memory_consolidator.llm_service.consolidate_memory",
            new_callable=AsyncMock,
        ) as mock_consolidate:
            mock_consolidate.return_value = _valid_consolidation()
            await consolidate_user_memory(user_id)
            mock_consolidate.assert_awaited_once()

        async with connection.db_session() as db:
            doc = await db["user_profiles"].find_one({"_id": user_id})
            # Apply happened: insights present + summary refreshed.
            assert doc["profile_summary"] == "A consolidated summary of the user."
            insight_texts = [ins["content"] for ins in doc.get("insights", [])]
            assert "Tends to get stressed during deadlines; values reassurance then." in insight_texts
            # last_consolidated_at set on success.
            assert doc.get("last_consolidated_at") is not None
            # Reused _enforce_budget guarantees the profile ends under budget.
            _, over_after = await build_memory_block(db, user_id)
            assert not over_after

        counters = metrics.snapshot()["counters"]
        assert counters.get("consolidation.runs") == 1
        assert counters.get("consolidation.success") == 1
        assert "consolidation.failure" not in counters
    finally:
        config.USER_MEMORY_BUDGET_CHARS = original_budget


# --------------------------------------------------------------------------- #
# B. Never-wipe on a None result: memory + timestamp untouched, apply skipped.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_consolidation_none_never_wipes_memory():
    from app.services.memory_consolidator import consolidate_user_memory

    user_id = 80002
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "U")
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {"$set": {
                "facts": [{"category": "personal", "content": "Has a dog named Bruno."}],
                "beliefs": [{"content": "Believes family comes first."}],
                "events": [{"description": "Graduated.", "significance": "major"}],
                "insights": [{"content": "Pre-existing insight."}],
            }},
        )
        before = await db["user_profiles"].find_one({"_id": user_id})
        # last_consolidated_at intentionally absent here (never consolidated).
        assert "last_consolidated_at" not in before

    with patch(
        "app.services.memory_consolidator.llm_service.consolidate_memory",
        new_callable=AsyncMock,
    ) as mock_consolidate, patch(
        "app.services.memory_consolidator.models.apply_consolidation",
        new_callable=AsyncMock,
    ) as mock_apply:
        mock_consolidate.return_value = None
        await consolidate_user_memory(user_id)
        mock_apply.assert_not_awaited()  # apply must be skipped on None (never-wipe)

    async with connection.db_session() as db:
        after = await db["user_profiles"].find_one({"_id": user_id})

    assert after["facts"] == before["facts"]
    assert after["beliefs"] == before["beliefs"]
    assert after["events"] == before["events"]
    assert after["insights"] == before["insights"]
    assert "last_consolidated_at" not in after  # not advanced

    counters = metrics.snapshot()["counters"]
    assert counters.get("consolidation.runs") == 1
    assert counters.get("consolidation.failure") == 1
    assert "consolidation.success" not in counters


# --------------------------------------------------------------------------- #
# C. The run never raises if the LLM raises; failure is counted.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_consolidation_run_never_raises_on_llm_error():
    from app.services.memory_consolidator import consolidate_user_memory

    user_id = 80003
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "U")

    with patch(
        "app.services.memory_consolidator.llm_service.consolidate_memory",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # Must not raise into the scheduler.
        await consolidate_user_memory(user_id)

    counters = metrics.snapshot()["counters"]
    assert counters.get("consolidation.runs") == 1
    assert counters.get("consolidation.failure") == 1
    assert "consolidation.success" not in counters


# --------------------------------------------------------------------------- #
# D. Insights rendering in compile_memory_text (pure, defensive).
# --------------------------------------------------------------------------- #
def test_compile_memory_text_renders_insights_section():
    text = compile_memory_text({"insights": [{"content": "X pattern"}]})
    assert "=== BEHAVIORAL INSIGHTS ===" in text
    assert "X pattern" in text


def test_compile_memory_text_empty_insights_placeholder():
    # An empty dict has no "insights" key — must render the placeholder, not raise.
    text = compile_memory_text({})
    assert "=== BEHAVIORAL INSIGHTS ===" in text
    assert "(No long-term insights yet)" in text


# --------------------------------------------------------------------------- #
# E. run_consolidator serialization under memory_lock.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_consolidator_runs_under_lock():
    mgr = UserTaskManager()
    user_id = 80004
    with patch(
        "app.services.memory_consolidator.consolidate_user_memory",
        new_callable=AsyncMock,
    ) as mock_consolidate:
        await mgr.run_consolidator(user_id)
        mock_consolidate.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_run_consolidator_skips_when_lock_held():
    mgr = UserTaskManager()
    user_id = 80005
    with patch(
        "app.services.memory_consolidator.consolidate_user_memory",
        new_callable=AsyncMock,
    ) as mock_consolidate:
        state = await mgr.get_state(user_id)
        await state.memory_lock.acquire()
        try:
            await mgr.run_consolidator(user_id)
            mock_consolidate.assert_not_called()
        finally:
            state.memory_lock.release()
