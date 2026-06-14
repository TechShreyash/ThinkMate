"""Tests for the Phase 12 engagement data model + CRUD (Feature A/B/D persistence).

Covers ``ensure_user`` field initialization, bounded ``mood_history`` appends in
``save_extracted_memories``, the combined ``touch_and_get_last_interaction`` hot-path
helper, the single-field setters, and ``find_users_due_for_proactive`` selection/gating.

mongomock + pytest-asyncio per ``tests/conftest.py``; config saved/restored as in
``tests/test_hardening.py``. No real LLM or network.
"""
from datetime import datetime, timezone, timedelta

import pytest

from app.config import config
from app.database import connection, models
from app.services.schemas import MemoryExtraction, EmotionLog


# --- 1. ensure_user initializes engagement fields ---------------------------------------

@pytest.mark.asyncio
async def test_ensure_user_initializes_mood_history_and_onboarded():
    async with connection.db_session() as db:
        user_id = 80001
        await models.ensure_user(db, user_id, "u", "U")
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc is not None
        assert doc["mood_history"] == []
        assert doc["onboarded"] is False


# --- 2. save_extracted_memories appends bounded mood history -----------------------------

@pytest.mark.asyncio
async def test_save_extracted_memories_appends_mood_history():
    async with connection.db_session() as db:
        user_id = 80002
        await models.ensure_user(db, user_id, "u", "U")

        # First mood write: happy.
        await models.save_extracted_memories(
            db, user_id,
            MemoryExtraction(emotional_state=EmotionLog(mood="happy", intensity=0.6)),
        )
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert len(doc["mood_history"]) == 1
        assert doc["mood_history"][0]["mood"] == "happy"
        assert doc["mood_history"][0]["intensity"] == 0.6
        assert doc["emotional_state"]["mood"] == "happy"

        # Second mood write: stressed -> history grows, emotional_state overwritten.
        await models.save_extracted_memories(
            db, user_id,
            MemoryExtraction(emotional_state=EmotionLog(mood="stressed", intensity=0.8)),
        )
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert len(doc["mood_history"]) == 2
        assert [m["mood"] for m in doc["mood_history"]] == ["happy", "stressed"]
        assert doc["emotional_state"]["mood"] == "stressed"


@pytest.mark.asyncio
async def test_mood_history_is_bounded_dropping_oldest():
    original = config.MAX_MOOD_HISTORY
    config.MAX_MOOD_HISTORY = 3
    try:
        async with connection.db_session() as db:
            user_id = 80003
            await models.ensure_user(db, user_id, "u", "U")

            moods = ["m1", "m2", "m3", "m4", "m5"]
            for mood in moods:
                await models.save_extracted_memories(
                    db, user_id,
                    MemoryExtraction(emotional_state=EmotionLog(mood=mood, intensity=0.5)),
                )

            doc = await db["user_profiles"].find_one({"_id": user_id})
            history = [m["mood"] for m in doc["mood_history"]]
            # Bounded to MAX_MOOD_HISTORY, oldest dropped, most-recent retained in order.
            assert len(history) == 3
            assert history == ["m3", "m4", "m5"]
            assert doc["emotional_state"]["mood"] == "m5"
    finally:
        config.MAX_MOOD_HISTORY = original


# --- 3. touch_and_get_last_interaction ---------------------------------------------------

@pytest.mark.asyncio
async def test_touch_and_get_last_interaction_first_call_returns_none_and_sets():
    async with connection.db_session() as db:
        user_id = 80004
        await models.ensure_user(db, user_id, "u", "U")

        now = datetime.now(timezone.utc)
        prev = await models.touch_and_get_last_interaction(db, user_id, now=now)
        assert prev is None  # no previous interaction recorded

        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc["last_interaction_at"] is not None


@pytest.mark.asyncio
async def test_touch_and_get_last_interaction_returns_previous_and_updates():
    async with connection.db_session() as db:
        user_id = 80005
        await models.ensure_user(db, user_id, "u", "U")

        first = datetime.now(timezone.utc)
        await models.touch_and_get_last_interaction(db, user_id, now=first)

        later = first + timedelta(hours=2)
        prev = await models.touch_and_get_last_interaction(db, user_id, now=later)
        # Returns the previous timestamp (within BSON ms storage resolution)...
        assert prev is not None
        assert abs(prev.replace(tzinfo=None) - first.replace(tzinfo=None)) < timedelta(milliseconds=1)

        # ...and the stored value advances to the newer `now`.
        doc = await db["user_profiles"].find_one({"_id": user_id})
        stored = doc["last_interaction_at"]
        assert abs(stored.replace(tzinfo=None) - later.replace(tzinfo=None)) < timedelta(milliseconds=1)


@pytest.mark.asyncio
async def test_touch_and_get_last_interaction_no_upsert_for_missing_profile():
    async with connection.db_session() as db:
        user_id = 80006  # never ensured -> no document
        prev = await models.touch_and_get_last_interaction(
            db, user_id, now=datetime.now(timezone.utc)
        )
        assert prev is None
        # Must not create a partial document.
        assert await db["user_profiles"].find_one({"_id": user_id}) is None


# --- 4. single-field setters -------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_proactive_enabled_writes_only_its_field():
    async with connection.db_session() as db:
        user_id = 80007
        await models.ensure_user(db, user_id, "alice", "Alice")

        await models.set_proactive_enabled(db, user_id, False)
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc["proactive_enabled"] is False
        # Other fields intact.
        assert doc["username"] == "alice"
        assert doc["onboarded"] is False
        assert doc["mood_history"] == []


@pytest.mark.asyncio
async def test_set_onboarded_writes_only_its_field():
    async with connection.db_session() as db:
        user_id = 80008
        await models.ensure_user(db, user_id, "bob", "Bob")

        await models.set_onboarded(db, user_id, True)
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert doc["onboarded"] is True
        # Other fields intact.
        assert doc["username"] == "bob"
        assert doc["facts"] == []


@pytest.mark.asyncio
async def test_set_last_proactive_writes_only_its_field():
    async with connection.db_session() as db:
        user_id = 80009
        await models.ensure_user(db, user_id, "carol", "Carol")

        now = datetime.now(timezone.utc)
        await models.set_last_proactive(db, user_id, now=now)
        doc = await db["user_profiles"].find_one({"_id": user_id})
        assert abs(doc["last_proactive_at"].replace(tzinfo=None) - now.replace(tzinfo=None)) < timedelta(milliseconds=1)
        # Other fields intact.
        assert doc["username"] == "carol"
        assert doc["onboarded"] is False


# --- 5. find_users_due_for_proactive selection & gating ----------------------------------

def _profile(user_id, *, last_interaction_at=None, last_proactive_at="__absent__",
             proactive_enabled="__absent__", item_count=5):
    """Build a raw user_profiles doc for direct insert_one seeding."""
    facts = [{"category": "personal", "content": f"fact {i}"} for i in range(item_count)]
    doc = {"_id": user_id, "facts": facts, "beliefs": [], "events": []}
    if last_interaction_at is not None:
        doc["last_interaction_at"] = last_interaction_at
    if last_proactive_at != "__absent__":
        doc["last_proactive_at"] = last_proactive_at
    if proactive_enabled != "__absent__":
        doc["proactive_enabled"] = proactive_enabled
    return doc


@pytest.mark.asyncio
async def test_find_users_due_for_proactive_selects_only_eligible():
    original_min_items = config.PROACTIVE_MIN_ITEMS
    config.PROACTIVE_MIN_ITEMS = 3
    try:
        async with connection.db_session() as db:
            now = datetime.now(timezone.utc)

            # (a) DUE: inactive (3d) + enough items + never nudged + enabled-by-absence.
            await db["user_profiles"].insert_one(
                _profile(1, last_interaction_at=now - timedelta(days=3), item_count=5)
            )
            # (b) NOT due: active (1 min ago).
            await db["user_profiles"].insert_one(
                _profile(2, last_interaction_at=now - timedelta(minutes=1), item_count=5)
            )
            # (c) NOT due: inactive but recently nudged.
            await db["user_profiles"].insert_one(
                _profile(3, last_interaction_at=now - timedelta(days=3),
                         last_proactive_at=now, item_count=5)
            )
            # (d) NOT due: inactive but opted out.
            await db["user_profiles"].insert_one(
                _profile(4, last_interaction_at=now - timedelta(days=3),
                         proactive_enabled=False, item_count=5)
            )
            # (e) NOT due: inactive but too few items (< PROACTIVE_MIN_ITEMS).
            await db["user_profiles"].insert_one(
                _profile(5, last_interaction_at=now - timedelta(days=3), item_count=1)
            )
            # (f) NOT due: no last_interaction_at at all.
            await db["user_profiles"].insert_one(
                _profile(6, last_interaction_at=None, item_count=5)
            )

            due = await models.find_users_due_for_proactive(
                db,
                inactivity_secs=86400,      # 1 day
                min_interval_secs=172800,   # 2 days
                limit=20,
                now=now,
            )
            assert due == [1]
    finally:
        config.PROACTIVE_MIN_ITEMS = original_min_items


@pytest.mark.asyncio
async def test_find_users_due_for_proactive_respects_limit():
    original_min_items = config.PROACTIVE_MIN_ITEMS
    config.PROACTIVE_MIN_ITEMS = 3
    try:
        async with connection.db_session() as db:
            now = datetime.now(timezone.utc)

            # Seed 5 due users; cap the scan at 2.
            for uid in range(101, 106):
                await db["user_profiles"].insert_one(
                    _profile(uid, last_interaction_at=now - timedelta(days=3), item_count=5)
                )

            due = await models.find_users_due_for_proactive(
                db,
                inactivity_secs=86400,
                min_interval_secs=172800,
                limit=2,
                now=now,
            )
            assert len(due) == 2
            assert all(uid in range(101, 106) for uid in due)
    finally:
        config.PROACTIVE_MIN_ITEMS = original_min_items
