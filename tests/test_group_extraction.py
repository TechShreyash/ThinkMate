"""Multi-party group memory extraction tests (Task 6.2).

Validates the group extraction path in ``app/services/memory_extractor.py``:
- a two-speaker segment yields a SINGLE extraction call and saves each participant's
  updates to the correct per-``user_id`` profile (Requirements 5.1, 5.2, 5.3);
- an update tagged to a participant who is not in the segment is skipped rather than
  misattributed, with no crash and no stray profile (Requirement 5.4);
- the processed segment is trimmed with the existing atomic trim, keeping the most
  recent ``CHAT_BUFFER_TRIM`` messages (Requirement 5.6).

All tests use mongomock + pytest-asyncio per ``tests/conftest.py``. The extraction LLM is
patched with ``AsyncMock`` so no network is touched. The extractor calls
``llm_service.extract_group_memory`` via the module-level singleton imported into
``app.services.memory_extractor``; we patch that exact reference.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import config
from app.database import connection, models
from app.services import memory_extractor
from app.services.memory_extractor import extract_and_trim, extract_and_trim_group
from app.services.schemas import (
    GroupMemoryExtraction,
    GroupMemoryUpdate,
    MemoryExtraction,
    FactExtract,
)

# A group chat id is negative in Telegram; use a representative supergroup id.
GROUP_CHAT_ID = -100
ALICE_ID, ALICE_NAME = 111, "Alice"
BOB_ID, BOB_NAME = 222, "Bob"


async def _seed_group_buffer(db, *, trim: int):
    """Seed a two-speaker group buffer with more than ``trim`` messages.

    Both Alice and Bob speak within the first ``len - trim`` messages so they appear in the
    extracted segment (the name->id map is built from the segment, not the whole buffer).
    Returns the ordered list of seeded contents for trim assertions.
    """
    seeded = [
        (ALICE_ID, ALICE_NAME, "Hi everyone"),
        (BOB_ID, BOB_NAME, "Hey Alice, good to see you"),
        (ALICE_ID, ALICE_NAME, "I adopted a cat named Miso"),
        (BOB_ID, BOB_NAME, "I just started teaching at the local school"),
        (ALICE_ID, ALICE_NAME, "anyway, what's for lunch"),  # most recent (kept)
        (BOB_ID, BOB_NAME, "pizza sounds good"),             # most recent (kept)
    ]
    for sid, sname, content in seeded:
        await models.add_message_to_buffer(
            db, GROUP_CHAT_ID, "user", content, sender_id=sid, sender_name=sname
        )
    return [c for _, _, c in seeded]


@pytest.mark.asyncio
async def test_two_speaker_segment_attributes_facts_to_correct_users():
    """One extraction call; Alice's fact -> profile 111, Bob's fact -> profile 222.

    **Validates: Requirements 5.1, 5.2, 5.3**
    """
    original_trim = config.CHAT_BUFFER_TRIM
    config.CHAT_BUFFER_TRIM = 2
    try:
        async with connection.db_session() as db:
            await _seed_group_buffer(db, trim=2)

        group_result = GroupMemoryExtraction(updates=[
            GroupMemoryUpdate(
                participant="Alice",
                extraction=MemoryExtraction(
                    new_facts=[FactExtract(category="personal", content="Alice has a cat named Miso")]
                ),
            ),
            GroupMemoryUpdate(
                participant="Bob",
                extraction=MemoryExtraction(
                    new_facts=[FactExtract(category="work", content="Bob is a teacher")]
                ),
            ),
        ])

        with patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=group_result,
        ) as mock_extract:
            # extract_and_trim dispatches to the group path: 2 distinct human senders.
            await extract_and_trim(GROUP_CHAT_ID)

        # Exactly one multi-party extraction call (not one per participant).
        assert mock_extract.await_count == 1

        async with connection.db_session() as db:
            alice_facts = [f["content"] for f in await models.get_active_facts(db, ALICE_ID)]
            bob_facts = [f["content"] for f in await models.get_active_facts(db, BOB_ID)]

        assert alice_facts == ["Alice has a cat named Miso"]
        assert bob_facts == ["Bob is a teacher"]
    finally:
        config.CHAT_BUFFER_TRIM = original_trim


@pytest.mark.asyncio
async def test_unresolved_participant_is_skipped():
    """An update tagged to a non-participant ("Carol") is skipped, no crash, no profile.

    **Validates: Requirements 5.4**
    """
    original_trim = config.CHAT_BUFFER_TRIM
    config.CHAT_BUFFER_TRIM = 2
    try:
        async with connection.db_session() as db:
            await _seed_group_buffer(db, trim=2)

        group_result = GroupMemoryExtraction(updates=[
            GroupMemoryUpdate(
                participant="Carol",  # not present in the segment's name->id map
                extraction=MemoryExtraction(
                    new_facts=[FactExtract(category="personal", content="Carol likes hiking")]
                ),
            ),
        ])

        with patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=group_result,
        ) as mock_extract:
            await extract_and_trim_group(GROUP_CHAT_ID)

        assert mock_extract.await_count == 1

        async with connection.db_session() as db:
            # No profile is created for the unknown participant, and the known speakers
            # got nothing (the only update was unresolved).
            assert await db["user_profiles"].find_one({"_id": ALICE_ID}) is None
            assert await db["user_profiles"].find_one({"_id": BOB_ID}) is None
            # Crucially, no profile was fabricated under the chat id either.
            assert await db["user_profiles"].find_one({"_id": GROUP_CHAT_ID}) is None
    finally:
        config.CHAT_BUFFER_TRIM = original_trim


@pytest.mark.asyncio
async def test_processed_segment_is_trimmed():
    """After a successful extraction, only the most recent CHAT_BUFFER_TRIM messages remain.

    **Validates: Requirements 5.6**
    """
    original_trim = config.CHAT_BUFFER_TRIM
    config.CHAT_BUFFER_TRIM = 2
    try:
        async with connection.db_session() as db:
            seeded = await _seed_group_buffer(db, trim=2)

        group_result = GroupMemoryExtraction(updates=[
            GroupMemoryUpdate(
                participant="Alice",
                extraction=MemoryExtraction(
                    new_facts=[FactExtract(category="personal", content="Alice has a cat named Miso")]
                ),
            ),
        ])

        with patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=group_result,
        ):
            await extract_and_trim_group(GROUP_CHAT_ID)

        async with connection.db_session() as db:
            remaining = [m["content"] for m in await models.get_chat_buffer(db, GROUP_CHAT_ID)]

        # The oldest segment (everything but the last CHAT_BUFFER_TRIM) was trimmed; the
        # two most recent messages survive in order.
        assert remaining == seeded[-2:]
        assert len(remaining) == config.CHAT_BUFFER_TRIM
    finally:
        config.CHAT_BUFFER_TRIM = original_trim
