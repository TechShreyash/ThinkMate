"""Tests for the group-chat model changes in app/database/models.py (Task 1.4).

Covers:
- ``add_message_to_buffer`` sender attribution (sender_id/sender_name persisted,
  defaults to chat_id, DM shape preserved, $slice hard cap bound).
- ``get_chat_member`` / ``upsert_chat_member`` CRUD: defaults, round-trip,
  affinity clamping to [0, 1], invalid-mode coercion, missing-member None.

Uses mongomock + pytest-asyncio via the autouse ``mock_mongodb`` fixture and the
``db_session`` helper, per tests/conftest.py conventions.

_Requirements: 1.2, 1.7, 2.1, 4.1, 4.3, 4.7_
"""
import pytest
from app.config import config
from app.database import connection, models


# --- add_message_to_buffer: sender attribution ---------------------------------

@pytest.mark.asyncio
async def test_buffer_stores_sender_id_and_name():
    """Each pushed message persists sender_id and sender_name on the raw doc."""
    async with connection.db_session() as db:
        chat_id = -1001234567890
        await models.add_message_to_buffer(
            db, chat_id, "user", "happy birthday Bob!",
            sender_id=111, sender_name="Alice",
        )

        doc = await db["chat_buffers"].find_one({"_id": chat_id})
        assert doc is not None
        msg = doc["messages"][0]
        assert msg["sender_id"] == 111
        assert msg["sender_name"] == "Alice"
        assert msg["role"] == "user"
        assert msg["content"] == "happy birthday Bob!"


@pytest.mark.asyncio
async def test_buffer_defaults_sender_id_to_chat_id_when_omitted():
    """When sender_id is omitted it defaults to chat_id; sender_name defaults to ''."""
    async with connection.db_session() as db:
        chat_id = 98765
        await models.add_message_to_buffer(db, chat_id, "user", "hello")

        doc = await db["chat_buffers"].find_one({"_id": chat_id})
        msg = doc["messages"][0]
        assert msg["sender_id"] == chat_id
        assert msg["sender_name"] == ""


@pytest.mark.asyncio
async def test_buffer_dm_call_returns_messages_and_keeps_id():
    """A DM-style call (chat_id == user_id, no sender args) returns the post-update
    array and keeps the on-disk _id equal to chat_id."""
    async with connection.db_session() as db:
        user_id = 555
        msgs1 = await models.add_message_to_buffer(db, user_id, "user", "Hello bot!")
        msgs2 = await models.add_message_to_buffer(db, user_id, "assistant", "Hello human!")

        # Returns the cumulative post-update messages array.
        assert isinstance(msgs1, list) and len(msgs1) == 1
        assert len(msgs2) == 2
        assert msgs2[0]["content"] == "Hello bot!"
        assert msgs2[1]["content"] == "Hello human!"

        # On-disk document _id is exactly the chat_id (== user_id in a DM).
        doc = await db["chat_buffers"].find_one({"_id": user_id})
        assert doc is not None
        assert doc["_id"] == user_id
        # Sender attribution still present, defaulting to the DM user.
        assert doc["messages"][0]["sender_id"] == user_id


@pytest.mark.asyncio
async def test_buffer_hard_cap_bounds_array():
    """The $slice hard cap bounds the stored array length even under many pushes."""
    original_cap = config.CHAT_BUFFER_HARD_CAP
    config.CHAT_BUFFER_HARD_CAP = 5
    try:
        async with connection.db_session() as db:
            chat_id = 4242
            for i in range(12):
                await models.add_message_to_buffer(db, chat_id, "user", f"msg-{i}")

            doc = await db["chat_buffers"].find_one({"_id": chat_id})
            messages = doc["messages"]
            assert len(messages) == 5
            # Hard cap keeps the most recent messages.
            assert messages[-1]["content"] == "msg-11"
            assert messages[0]["content"] == "msg-7"
    finally:
        config.CHAT_BUFFER_HARD_CAP = original_cap


# --- chat_members CRUD ---------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_member_first_upsert_applies_defaults():
    """First upsert with no affinity/mode applies AFFINITY_DEFAULT and mode 'auto';
    the composite _id is "{chat_id}:{user_id}"."""
    async with connection.db_session() as db:
        chat_id, user_id = -100, 222
        await models.upsert_chat_member(db, chat_id, user_id)

        member = await models.get_chat_member(db, chat_id, user_id)
        assert member is not None
        assert member["_id"] == f"{chat_id}:{user_id}"
        assert member["chat_id"] == chat_id
        assert member["user_id"] == user_id
        assert member["affinity"] == config.AFFINITY_DEFAULT
        assert member["mode"] == "auto"


@pytest.mark.asyncio
async def test_chat_member_affinity_round_trip():
    """Upserting an affinity value can be read back."""
    async with connection.db_session() as db:
        chat_id, user_id = -100, 333
        await models.upsert_chat_member(db, chat_id, user_id, affinity=0.62)

        member = await models.get_chat_member(db, chat_id, user_id)
        assert member["affinity"] == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_chat_member_affinity_clamps_high():
    """Affinity above 1.0 is clamped to 1.0."""
    async with connection.db_session() as db:
        chat_id, user_id = -100, 444
        await models.upsert_chat_member(db, chat_id, user_id, affinity=1.5)

        member = await models.get_chat_member(db, chat_id, user_id)
        assert member["affinity"] == 1.0


@pytest.mark.asyncio
async def test_chat_member_affinity_clamps_low():
    """Affinity below 0.0 is clamped to 0.0."""
    async with connection.db_session() as db:
        chat_id, user_id = -100, 555
        await models.upsert_chat_member(db, chat_id, user_id, affinity=-0.3)

        member = await models.get_chat_member(db, chat_id, user_id)
        assert member["affinity"] == 0.0


@pytest.mark.asyncio
async def test_chat_member_invalid_mode_coerced_to_auto():
    """An invalid mode (e.g. 'loud') is coerced to 'auto' rather than stored."""
    async with connection.db_session() as db:
        chat_id, user_id = -100, 666
        await models.upsert_chat_member(db, chat_id, user_id, mode="loud")

        member = await models.get_chat_member(db, chat_id, user_id)
        assert member["mode"] == "auto"


@pytest.mark.asyncio
async def test_chat_member_valid_mode_round_trip():
    """A valid mode is stored and read back."""
    async with connection.db_session() as db:
        chat_id, user_id = -100, 777
        await models.upsert_chat_member(db, chat_id, user_id, mode="quiet")

        member = await models.get_chat_member(db, chat_id, user_id)
        assert member["mode"] == "quiet"


@pytest.mark.asyncio
async def test_get_chat_member_returns_none_when_absent():
    """get_chat_member returns None for a non-existent member."""
    async with connection.db_session() as db:
        member = await models.get_chat_member(db, -100, 999999)
        assert member is None
