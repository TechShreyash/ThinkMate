"""Memory maintenance must never refresh or invent a user's observed mood."""

from datetime import UTC, datetime

import pytest

from app.database import connection, models
from app.services.schemas import EmotionLog, MemoryCompression, MemoryConsolidation

MAINTENANCE_CASES = [
    (models.replace_user_memory, MemoryCompression),
    (models.apply_consolidation, MemoryConsolidation),
]


@pytest.mark.parametrize("maintain,schema", MAINTENANCE_CASES)
@pytest.mark.parametrize("emitted_mood", ["sad", "excited", None])
async def test_maintenance_preserves_original_mood_and_detection_time(
    maintain,
    schema,
    emitted_mood,
):
    user_id = 88101
    observed_at = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    stored_mood = {
        "mood": "sad",
        "intensity": 0.7,
        "trigger": "missed a deadline",
        "detected_at": observed_at,
    }
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "test", "Test")
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {"$set": {"emotional_state": stored_mood, "mood_history": [stored_mood]}},
        )
        before = await db["user_profiles"].find_one({"_id": user_id})
        proposed_mood = (
            None
            if emitted_mood is None
            else EmotionLog(
                mood=emitted_mood,
                intensity=0.7,
                trigger="missed a deadline",
            )
        )

        await maintain(
            db,
            user_id,
            schema(
                profile_summary="Refreshed summary",
                emotional_state=proposed_mood,
            ),
        )

        after = await db["user_profiles"].find_one({"_id": user_id})

    assert after["emotional_state"] == before["emotional_state"]
    assert after["mood_history"] == before["mood_history"]
    assert after["profile_summary"] == "Refreshed summary"


@pytest.mark.parametrize("maintain,schema", MAINTENANCE_CASES)
@pytest.mark.parametrize("has_null_mood", [True, False])
async def test_maintenance_cannot_invent_mood_when_none_was_observed(
    maintain,
    schema,
    has_null_mood,
):
    user_id = 88102
    profile = {"_id": user_id, "facts": [], "beliefs": [], "events": []}
    if has_null_mood:
        profile["emotional_state"] = None
    async with connection.db_session() as db:
        await db["user_profiles"].insert_one(profile)

        await maintain(
            db,
            user_id,
            schema(
                emotional_state=EmotionLog(
                    mood="happy", intensity=0.9, trigger="old memory"
                ),
            ),
        )

        after = await db["user_profiles"].find_one({"_id": user_id})

    if has_null_mood:
        assert after["emotional_state"] is None
    else:
        assert "emotional_state" not in after
