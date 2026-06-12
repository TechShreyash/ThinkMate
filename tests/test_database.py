import os
import pytest
import pytest_asyncio
from app.database import connection, models
from app.services.schemas import MemoryExtraction, FactExtract, EventExtract, EmotionLog, ProfileUpdate

@pytest_asyncio.fixture
async def temp_db():
    # Setup temporary database path
    original_db_path = connection.DB_PATH
    connection.DB_PATH = "data/test_database.sqlite"
    
    # Initialize DB
    await connection.init_db()
    
    yield connection.DB_PATH
    
    # Cleanup
    if os.path.exists(connection.DB_PATH):
        try:
            os.remove(connection.DB_PATH)
        except Exception:
            pass
    # Restore original path
    connection.DB_PATH = original_db_path

@pytest.mark.asyncio
async def test_db_initialization(temp_db):
    assert os.path.exists(temp_db)
    
    # Verify tables exist
    async with connection.db_session() as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table';") as cursor:
            rows = await cursor.fetchall()
            tables = [row["name"] for row in rows]
            assert "user_profiles" in tables
            assert "facts" in tables
            assert "events" in tables
            assert "emotional_log" in tables
            assert "chat_buffer" in tables

@pytest.mark.asyncio
async def test_ensure_user_and_buffer(temp_db):
    async with connection.db_session() as db:
        # 1. Ensure user works
        await models.ensure_user(db, 12345, "testuser", "Test User")
        
        # Verify user inserted
        async with db.execute("SELECT * FROM user_profiles WHERE user_id = 12345;") as cursor:
            user = await cursor.fetchone()
            assert user is not None
            assert user["username"] == "testuser"
            assert user["display_name"] == "Test User"
            
        # 2. Add message to buffer
        await models.add_message_to_buffer(db, 12345, "user", "Hello bot!")
        await models.add_message_to_buffer(db, 12345, "assistant", "Hello human!")
        
        count = await models.get_buffer_count(db, 12345)
        assert count == 2
        
        buffer = await models.get_chat_buffer(db, 12345)
        assert len(buffer) == 2
        assert buffer[0]["role"] == "user"
        assert buffer[0]["content"] == "Hello bot!"
        
        # 3. Trim buffer
        await models.delete_oldest_buffer_messages(db, 12345, 1)
        count = await models.get_buffer_count(db, 12345)
        assert count == 1
        
        remaining = await models.get_chat_buffer(db, 12345)
        assert remaining[0]["role"] == "assistant"

@pytest.mark.asyncio
async def test_save_extracted_memories(temp_db):
    async with connection.db_session() as db:
        await models.ensure_user(db, 12345, "testuser", "Test User")
        
        # Prepare memory extraction structure
        extraction = MemoryExtraction(
            profile_updates=ProfileUpdate(communication_style="Friendly and concise"),
            new_facts=[
                FactExtract(category="preference", content="Enjoys green tea"),
                FactExtract(category="personal", content="Lives in Seattle")
            ],
            updated_facts=[],
            events=[
                EventExtract(description="Graduated college", date="2026-05", significance="major", emotion="pride")
            ],
            emotional_state=EmotionLog(mood="happy", intensity=0.9, trigger="good weather")
        )
        
        await models.save_extracted_memories(db, 12345, extraction)
        
        # Verify updates in user profiles
        async with db.execute("SELECT communication_style FROM user_profiles WHERE user_id = 12345;") as cursor:
            row = await cursor.fetchone()
            assert row["communication_style"] == "Friendly and concise"
            
        # Verify active facts
        facts = await models.get_active_facts(db, 12345)
        assert len(facts) == 2
        contents = [f["content"] for f in facts]
        assert "Enjoys green tea" in contents
        assert "Lives in Seattle" in contents
