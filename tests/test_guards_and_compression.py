import os
import pytest
import pytest_asyncio
from aiosqlite import Connection
from app.config import config
from app.database import connection, models
from app.services.memory_loader import build_memory_block
from app.services.schemas import MemoryCompression, CompressedFact, CompressedEvent, EmotionLog
from app.services.llm_service import LLMService

@pytest_asyncio.fixture
async def temp_db():
    original_db_path = connection.DB_PATH
    connection.DB_PATH = "data/test_guards_db.sqlite"
    await connection.init_db()
    yield connection.DB_PATH
    if os.path.exists(connection.DB_PATH):
        try:
            os.remove(connection.DB_PATH)
        except Exception:
            pass
    connection.DB_PATH = original_db_path

@pytest.mark.asyncio
async def test_input_guard_config():
    # Verify new configuration variables exist and have correct defaults
    assert config.USER_MEMORY_BUDGET_CHARS == 10000
    assert config.CHARS_PER_TOKEN == 4
    assert config.MAX_INPUT_CHARS == 1000
    assert config.MAX_RESPONSE_CHARS == 1000

@pytest.mark.asyncio
async def test_build_memory_block_and_compression_flag(temp_db):
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
            await db.execute(
                "INSERT INTO facts (user_id, category, content) VALUES (?, ?, ?)",
                (user_id, "preference", "This is a very long fact description to exceed budget.")
            )
            await db.commit()
            
            mem_text2, needs_comp2 = await build_memory_block(db, user_id)
            assert needs_comp2
        finally:
            config.USER_MEMORY_BUDGET_CHARS = original_budget

@pytest.mark.asyncio
async def test_replace_user_memory(temp_db):
    async with connection.db_session() as db:
        user_id = 22222
        await models.ensure_user(db, user_id, "compuser", "Compressed User")
        
        # Add some initial facts and events
        await db.execute(
            "INSERT INTO facts (user_id, category, content) VALUES (?, 'personal', 'Lives in Seattle')",
            (user_id,)
        )
        await db.execute(
            "INSERT INTO events (user_id, description, event_date, significance) VALUES (?, 'Bought a car', '2026-01', 'minor')",
            (user_id,)
        )
        await db.commit()
        
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
        
        # Perform replacement
        await models.replace_user_memory(db, user_id, compression)
        
        # 1. Verify user profile fields
        async with db.execute("SELECT profile_summary, communication_style FROM user_profiles WHERE user_id = ?", (user_id,)) as cursor:
            profile = await cursor.fetchone()
            assert profile["profile_summary"] == "An developer from Seattle."
            assert profile["communication_style"] == "Direct and logical."
            
        # 2. Verify active facts: old one should be soft-deleted (is_active=0) and new ones active
        async with db.execute("SELECT content, is_active FROM facts WHERE user_id = ?", (user_id,)) as cursor:
            facts = await cursor.fetchall()
            assert len(facts) == 3
            # Active facts should be the compressed ones
            active_facts = [f["content"] for f in facts if f["is_active"] == 1]
            inactive_facts = [f["content"] for f in facts if f["is_active"] == 0]
            assert len(active_facts) == 2
            assert "Resides in Seattle, WA" in active_facts
            assert "Enjoys typing code" in active_facts
            assert "Lives in Seattle" in inactive_facts
            
        # 3. Verify events: old one should be deleted completely and new one inserted
        async with db.execute("SELECT description, event_date, significance FROM events WHERE user_id = ?", (user_id,)) as cursor:
            events = await cursor.fetchall()
            assert len(events) == 1
            assert events[0]["description"] == "Bought a Tesla"
            assert events[0]["event_date"] == "2026-01-15"
            assert events[0]["significance"] == "major"
            
        # 4. Verify mood logged
        async with db.execute("SELECT mood, intensity, trigger FROM emotional_log WHERE user_id = ? ORDER BY detected_at DESC LIMIT 1", (user_id,)) as cursor:
            mood = await cursor.fetchone()
            assert mood["mood"] == "calm"
            assert mood["intensity"] == 0.7
            assert mood["trigger"] == "weekend"
