"""Tests for the Phase 11 consolidation CRUD helpers in app/database/models.py.

Covers (mongomock + pytest-asyncio, db_session per tests/test_database.py):
  - find_users_due_for_consolidation: time-based due selection (null/old/recent),
    the min_items item-count threshold, and the limit cap.
  - apply_consolidation: single coherent write (summary/style/facts/beliefs/events),
    last_consolidated_at/updated_at set, and insights truncated to config.MAX_INSIGHTS.
  - ensure_user: initializes insights to [] on insert.

_Requirements: 8.2, 8.3, 8.4, 9.1, 9.2_
"""
from datetime import datetime, timezone, timedelta

import pytest

from app.config import config
from app.database import connection, models
from app.services.schemas import (
    MemoryConsolidation,
    ConsolidatedInsight,
    CompressedFact,
    CompressedBelief,
    CompressedEvent,
    EmotionLog,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _profile_doc(user_id: int, *, last_consolidated_at, n_items: int) -> dict:
    """Build a raw user_profiles doc with `n_items` total facts+beliefs+events.

    Items are split across the three arrays so the count threshold is exercised over the
    sum (as find_users_due_for_consolidation computes it).
    """
    facts = [
        {"category": "personal", "content": f"fact {i}", "confidence": 1.0}
        for i in range(n_items)
    ]
    doc = {
        "_id": user_id,
        "username": "u",
        "display_name": "U",
        "profile_summary": "",
        "communication_style": "",
        "emotional_state": None,
        "facts": facts,
        "beliefs": [],
        "events": [],
        "insights": [],
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    if last_consolidated_at is not None:
        doc["last_consolidated_at"] = last_consolidated_at
    return doc


# --------------------------------------------------------------------------------------
# find_users_due_for_consolidation
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_due_selects_null_and_old_excludes_recent_and_item_poor():
    """Due = (null/absent OR older than interval) AND items >= min_items.

    Seeds four users:
      (a) no last_consolidated_at + enough items        -> DUE
      (b) old last_consolidated_at (now-10d) + enough    -> DUE
      (c) recent last_consolidated_at (now) + enough     -> NOT due (recent)
      (d) null last_consolidated_at + too few items      -> NOT due (item-poor)

    _Requirements: 8.2, 9.2_
    """
    interval_secs = 86400  # 1 day
    min_items = 3
    now = _utcnow()

    async with connection.db_session() as db:
        # (a) never consolidated, enough items
        await db["user_profiles"].insert_one(
            _profile_doc(1, last_consolidated_at=None, n_items=5)
        )
        # (b) consolidated long ago, enough items
        await db["user_profiles"].insert_one(
            _profile_doc(2, last_consolidated_at=now - timedelta(days=10), n_items=5)
        )
        # (c) consolidated just now, enough items -> excluded (recent)
        await db["user_profiles"].insert_one(
            _profile_doc(3, last_consolidated_at=now, n_items=5)
        )
        # (d) never consolidated but too few items -> excluded (item-poor)
        await db["user_profiles"].insert_one(
            _profile_doc(4, last_consolidated_at=None, n_items=2)
        )

        due = await models.find_users_due_for_consolidation(
            db, interval_secs=interval_secs, min_items=min_items, limit=10
        )

    due_set = set(due)
    assert 1 in due_set  # (a) null + enough items
    assert 2 in due_set  # (b) old + enough items
    assert 3 not in due_set  # (c) recent
    assert 4 not in due_set  # (d) too few items
    assert due_set == {1, 2}


@pytest.mark.asyncio
async def test_find_due_excludes_item_count_exactly_below_threshold():
    """A user with exactly min_items-1 items is excluded; exactly min_items is included.

    _Requirements: 8.2_
    """
    min_items = 8
    async with connection.db_session() as db:
        await db["user_profiles"].insert_one(
            _profile_doc(10, last_consolidated_at=None, n_items=min_items - 1)
        )
        await db["user_profiles"].insert_one(
            _profile_doc(11, last_consolidated_at=None, n_items=min_items)
        )

        due = await models.find_users_due_for_consolidation(
            db, interval_secs=86400, min_items=min_items, limit=10
        )

    assert 10 not in due
    assert 11 in due


@pytest.mark.asyncio
async def test_find_due_counts_across_facts_beliefs_events():
    """The item count is the SUM across facts+beliefs+events, not any single array.

    _Requirements: 8.2_
    """
    async with connection.db_session() as db:
        await db["user_profiles"].insert_one({
            "_id": 20,
            "facts": [{"content": "f1"}, {"content": "f2"}],
            "beliefs": [{"content": "b1"}, {"content": "b2"}],
            "events": [{"description": "e1"}, {"description": "e2"}],
            "insights": [],
        })  # 6 total

        due = await models.find_users_due_for_consolidation(
            db, interval_secs=86400, min_items=6, limit=10
        )

    assert 20 in due


@pytest.mark.asyncio
async def test_find_due_respects_limit():
    """`limit` caps the number of returned users even when more qualify.

    _Requirements: 8.2, 9.2_
    """
    limit = 3
    async with connection.db_session() as db:
        # Seed 6 due users (null last_consolidated_at + enough items).
        for uid in range(100, 106):
            await db["user_profiles"].insert_one(
                _profile_doc(uid, last_consolidated_at=None, n_items=5)
            )

        due = await models.find_users_due_for_consolidation(
            db, interval_secs=86400, min_items=3, limit=limit
        )

    assert len(due) == limit


# --------------------------------------------------------------------------------------
# apply_consolidation
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_consolidation_writes_coherent_profile_and_truncates_insights():
    """apply_consolidation writes summary/style/items, sets timestamps, and truncates
    insights to config.MAX_INSIGHTS even when given more.

    _Requirements: 8.3, 8.4, 9.2_
    """
    original_max = config.MAX_INSIGHTS
    config.MAX_INSIGHTS = 5
    try:
        user_id = 30001
        extra = config.MAX_INSIGHTS + 3  # more insights than the cap
        consolidation = MemoryConsolidation(
            profile_summary="A thoughtful long-term user.",
            communication_style="Warm, prefers concise replies.",
            consolidated_facts=[
                CompressedFact(category="personal", content="Lives in Berlin"),
                CompressedFact(category="work", content="Works as an engineer"),
            ],
            consolidated_beliefs=[
                CompressedBelief(content="Values work-life balance"),
            ],
            consolidated_events=[
                CompressedEvent(description="Started a new job", date="2024-01", significance="major"),
            ],
            insights=[ConsolidatedInsight(content=f"insight {i}") for i in range(extra)],
            emotional_state=EmotionLog(mood="content", intensity=0.6, trigger="stable routine"),
        )

        async with connection.db_session() as db:
            await models.ensure_user(db, user_id, "testuser", "Test User")

            before = await db["user_profiles"].find_one({"_id": user_id})
            assert "last_consolidated_at" not in before  # not set on insert

            await models.apply_consolidation(db, user_id, consolidation)

            doc = await db["user_profiles"].find_one({"_id": user_id})

        # Summary / style refreshed.
        assert doc["profile_summary"] == "A thoughtful long-term user."
        assert doc["communication_style"] == "Warm, prefers concise replies."

        # Facts / beliefs / events written with expected content.
        assert [f["content"] for f in doc["facts"]] == ["Lives in Berlin", "Works as an engineer"]
        assert doc["facts"][0]["category"] == "personal"
        assert [b["content"] for b in doc["beliefs"]] == ["Values work-life balance"]
        assert doc["events"][0]["description"] == "Started a new job"
        assert doc["events"][0]["significance"] == "major"

        # Emotional state preserved.
        assert doc["emotional_state"]["mood"] == "content"

        # Timestamps set.
        assert doc.get("last_consolidated_at") is not None
        assert doc.get("updated_at") is not None

        # Insights truncated to the cap.
        assert len(doc["insights"]) == config.MAX_INSIGHTS
        assert [ins["content"] for ins in doc["insights"]] == [f"insight {i}" for i in range(config.MAX_INSIGHTS)]
        # Each insight carries the stored shape.
        assert all("created_at" in ins and "updated_at" in ins for ins in doc["insights"])
    finally:
        config.MAX_INSIGHTS = original_max


@pytest.mark.asyncio
async def test_apply_consolidation_skips_summary_and_style_when_absent():
    """When summary/style are None, apply_consolidation leaves the existing values intact.

    _Requirements: 8.3_
    """
    user_id = 30002
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "U")
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {"$set": {"profile_summary": "existing summary", "communication_style": "existing style"}},
        )

        consolidation = MemoryConsolidation(
            consolidated_facts=[CompressedFact(category="hobby", content="Plays chess")],
            insights=[ConsolidatedInsight(content="Strategic thinker")],
        )
        await models.apply_consolidation(db, user_id, consolidation)

        doc = await db["user_profiles"].find_one({"_id": user_id})

    # Unchanged because the consolidation left them None.
    assert doc["profile_summary"] == "existing summary"
    assert doc["communication_style"] == "existing style"
    # Facts/insights still written.
    assert [f["content"] for f in doc["facts"]] == ["Plays chess"]
    assert [ins["content"] for ins in doc["insights"]] == ["Strategic thinker"]
    assert doc.get("last_consolidated_at") is not None


@pytest.mark.asyncio
async def test_apply_consolidation_with_fewer_insights_than_cap_keeps_all():
    """Fewer insights than MAX_INSIGHTS are all stored (no padding, no truncation).

    _Requirements: 8.4_
    """
    original_max = config.MAX_INSIGHTS
    config.MAX_INSIGHTS = 5
    try:
        user_id = 30003
        consolidation = MemoryConsolidation(
            insights=[ConsolidatedInsight(content=f"insight {i}") for i in range(2)],
        )
        async with connection.db_session() as db:
            await models.ensure_user(db, user_id, "u", "U")
            await models.apply_consolidation(db, user_id, consolidation)
            doc = await db["user_profiles"].find_one({"_id": user_id})

        assert len(doc["insights"]) == 2
    finally:
        config.MAX_INSIGHTS = original_max


# --------------------------------------------------------------------------------------
# ensure_user insights initialization
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_user_initializes_insights_to_empty_list():
    """ensure_user sets insights to [] on insert (additive $setOnInsert).

    _Requirements: 9.2_
    """
    user_id = 40001
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "testuser", "Test User")
        doc = await db["user_profiles"].find_one({"_id": user_id})

    assert "insights" in doc
    assert doc["insights"] == []
