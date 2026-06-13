import pytest
from app.config import config
from app.database import connection, models
from app.services.memory_loader import build_memory_block
from app.services.schemas import MemoryCompression, CompressedFact, CompressedEvent, EmotionLog

@pytest.mark.asyncio
async def test_input_guard_config():
    # Verify key tuning variables are present and sane (exact values are env-tunable).
    assert config.USER_MEMORY_BUDGET_CHARS == 4000
    assert config.CHARS_PER_TOKEN == 4
    assert config.MAX_INPUT_CHARS >= config.MAX_RESPONSE_CHARS > 0

@pytest.mark.asyncio
async def test_build_memory_block_and_compression_flag():
    async with connection.db_session() as db:
        user_id = 11111
        await models.ensure_user(db, user_id, "testuser", "Test User")
        
        # Initially empty memory block
        mem_text, needs_comp = await build_memory_block(db, user_id)
        assert not needs_comp
        assert "=== USER PROFILE ===" in mem_text
        assert "=== CORE FACTS ===" in mem_text
        assert "=== LIFE EVENTS TIMELINE ===" in mem_text
        assert "=== CURRENT MOOD ===" in mem_text
        
        # Test needs_compression set to True when exceeding budget
        original_budget = config.USER_MEMORY_BUDGET_CHARS
        config.USER_MEMORY_BUDGET_CHARS = 50  # Lower budget to trigger compression
        try:
            # Let's insert a fact that makes total length > 50
            await db["user_profiles"].update_one(
                {"_id": user_id},
                {
                    "$push": {
                        "facts": {
                            "category": "preference",
                            "content": "This is a very long fact description to exceed budget."
                        }
                    }
                }
            )
            
            mem_text2, needs_comp2 = await build_memory_block(db, user_id)
            assert needs_comp2
        finally:
            config.USER_MEMORY_BUDGET_CHARS = original_budget

@pytest.mark.asyncio
async def test_replace_user_memory():
    async with connection.db_session() as db:
        user_id = 22222
        await models.ensure_user(db, user_id, "compuser", "Compressed User")
        
        # Add some initial facts and events in MongoDB
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {
                "$push": {
                    "facts": {"category": "personal", "content": "Lives in Seattle", "confidence": 1.0},
                    "events": {"description": "Bought a car", "event_date": "2026-01", "significance": "minor"}
                }
            }
        )
        
        # Prepare compression updates
        compression = MemoryCompression(
            profile_summary="An developer from Seattle.",
            communication_style="Direct and logical.",
            compressed_facts=[
                CompressedFact(category="personal", content="Resides in Seattle, WA"),
                CompressedFact(category="preference", content="Enjoys typing code")
            ],
            compressed_events=[
                CompressedEvent(description="Bought a Tesla", date="2026-01-15", significance="major")
            ],
            emotional_state=EmotionLog(mood="calm", intensity=0.7, trigger="weekend")
        )
        
        # Perform replacement (this overwrites the collections because we hard delete during replacement/update)
        await models.replace_user_memory(db, user_id, compression)
        
        # Fetch profile doc
        profile = await db["user_profiles"].find_one({"_id": user_id})
        
        # 1. Verify user profile fields
        assert profile["profile_summary"] == "An developer from Seattle."
        assert profile["communication_style"] == "Direct and logical."
            
        # 2. Verify active facts: old ones are replaced (hard deletes are used)
        active_facts = [f["content"] for f in profile["facts"]]
        assert len(active_facts) == 2
        assert "Resides in Seattle, WA" in active_facts
        assert "Enjoys typing code" in active_facts
        assert "Lives in Seattle" not in active_facts
            
        # 3. Verify events: old one should be deleted completely and new one inserted
        events = profile["events"]
        assert len(events) == 1
        assert events[0]["description"] == "Bought a Tesla"
        assert events[0]["event_date"] == "2026-01-15"
        assert events[0]["significance"] == "major"
            
        # 4. Verify mood logged directly
        assert profile["emotional_state"]["mood"] == "calm"
        assert profile["emotional_state"]["intensity"] == 0.7
        assert profile["emotional_state"]["trigger"] == "weekend"
