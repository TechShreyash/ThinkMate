import pytest
from app.database import connection, models
from app.services.schemas import MemoryExtraction, FactExtract, EventExtract, EmotionLog, ProfileUpdate

@pytest.mark.asyncio
async def test_db_initialization():
    # Verify index creation does not raise errors on our mocked database
    await connection.init_db()
    db = connection.get_db()
    
    # Assert collection index can be retrieved from mock database
    indexes = db["llm_audit_log"]._collection.index_information()
    assert any(k.startswith("user_id_1_timestamp_-1") for k in indexes.keys())

@pytest.mark.asyncio
async def test_ensure_user_and_buffer():
    async with connection.db_session() as db:
        # 1. Ensure user works
        await models.ensure_user(db, 12345, "testuser", "Test User")
        
        # Verify user inserted
        user = await db["user_profiles"].find_one({"_id": 12345})
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
async def test_save_extracted_memories():
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
            new_events=[
                EventExtract(description="Graduated college", date="2026-05", significance="major", emotion="pride")
            ],
            emotional_state=EmotionLog(mood="happy", intensity=0.9, trigger="good weather")
        )
        
        await models.save_extracted_memories(db, 12345, extraction)
        
        # Verify updates in user profiles
        user = await db["user_profiles"].find_one({"_id": 12345})
        assert user["communication_style"] == "Friendly and concise"
        assert user["emotional_state"]["mood"] == "happy"
        assert user["emotional_state"]["intensity"] == 0.9
        
        # Verify active facts
        facts = await models.get_active_facts(db, 12345)
        assert len(facts) == 2
        contents = [f["content"] for f in facts]
        assert "Enjoys green tea" in contents
        assert "Lives in Seattle" in contents
