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

import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.config import config
from app.database import connection, models
from app.services import memory_extractor
from app.services.memory_extractor import extract_and_trim, extract_and_trim_group
from app.services.schemas import (
    FactExtract,
    GroupMemoryExtraction,
    GroupMemoryUpdate,
    MemoryExtraction,
)

# A group chat id is negative in Telegram; use a representative supergroup id.
GROUP_CHAT_ID = -100
ALICE_ID, ALICE_NAME = 111, "Alice"
BOB_ID, BOB_NAME = 222, "Bob"


def test_group_extraction_rejects_single_user_top_level_shape():
    """A plain MemoryExtraction JSON must not validate as an empty group extraction."""
    with pytest.raises(ValidationError):
        GroupMemoryExtraction.model_validate(
            {
                "new_facts": [
                    {"category": "relationship", "content": "The group plans lunch"}
                ],
                "updates": [],
            }
        )


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
        (BOB_ID, BOB_NAME, "pizza sounds good"),  # most recent (kept)
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

        group_result = GroupMemoryExtraction(
            updates=[
                GroupMemoryUpdate(
                    participant="Alice",
                    extraction=MemoryExtraction(
                        new_facts=[
                            FactExtract(
                                category="personal",
                                content="Alice has a cat named Miso",
                            )
                        ]
                    ),
                ),
                GroupMemoryUpdate(
                    participant="Bob",
                    extraction=MemoryExtraction(
                        new_facts=[
                            FactExtract(category="work", content="Bob is a teacher")
                        ]
                    ),
                ),
            ]
        )

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
            alice_facts = [
                f["content"] for f in await models.get_active_facts(db, ALICE_ID)
            ]
            bob_facts = [
                f["content"] for f in await models.get_active_facts(db, BOB_ID)
            ]
            group_doc = await db["user_profiles"].find_one({"_id": GROUP_CHAT_ID})

        assert alice_facts == ["Alice has a cat named Miso"]
        assert bob_facts == ["Bob is a teacher"]
        assert group_doc["profile_type"] == "group"
        assert group_doc["last_group_extraction"]["group_saved"] is False
        assert group_doc["last_group_extraction"]["participant_updates"] == 2
    finally:
        config.CHAT_BUFFER_TRIM = original_trim


@pytest.mark.asyncio
async def test_group_extraction_saves_shared_group_memory_to_chat_profile():
    """Shared group facts are stored under the chat id, separate from participant profiles."""
    original_trim = config.CHAT_BUFFER_TRIM
    config.CHAT_BUFFER_TRIM = 2
    try:
        async with connection.db_session() as db:
            await _seed_group_buffer(db, trim=2)

        group_result = GroupMemoryExtraction(
            group_extraction=MemoryExtraction(
                new_facts=[
                    FactExtract(
                        category="relationship",
                        content="The group often coordinates lunch plans together",
                    )
                ]
            ),
            updates=[],
        )

        with patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=group_result,
        ):
            await extract_and_trim_group(GROUP_CHAT_ID)

        async with connection.db_session() as db:
            group_facts = [
                f["content"] for f in await models.get_active_facts(db, GROUP_CHAT_ID)
            ]
            group_doc = await db["user_profiles"].find_one({"_id": GROUP_CHAT_ID})
            assert group_facts == ["The group often coordinates lunch plans together"]
            assert group_doc["profile_type"] == "group"
            assert group_doc["last_group_extraction"]["group_saved"] is True
            assert group_doc["last_group_extraction"]["participant_updates"] == 0
            # Known speakers receive empty identity-free skeletons before the call,
            # allowing a reset during extraction to invalidate pending output.
            for participant_id in (ALICE_ID, BOB_ID):
                participant = await db["user_profiles"].find_one(
                    {"_id": participant_id}
                )
                assert participant["facts"] == []
                assert participant["memory_generation"]
                assert "username" not in participant
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

        group_result = GroupMemoryExtraction(
            updates=[
                GroupMemoryUpdate(
                    participant="Carol",  # not present in the segment's name->id map
                    extraction=MemoryExtraction(
                        new_facts=[
                            FactExtract(
                                category="personal", content="Carol likes hiking"
                            )
                        ]
                    ),
                ),
            ]
        )

        with patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=group_result,
        ) as mock_extract:
            await extract_and_trim_group(GROUP_CHAT_ID)

        assert mock_extract.await_count == 1

        async with connection.db_session() as db:
            # Known speakers have lifecycle skeletons but receive no facts because the
            # only update was unresolved. An unknown participant gets no skeleton.
            for participant_id in (ALICE_ID, BOB_ID):
                participant = await db["user_profiles"].find_one(
                    {"_id": participant_id}
                )
                assert participant["facts"] == []
                assert participant["memory_generation"]
                assert "username" not in participant
            # The group profile exists as an extraction status marker, but has no shared facts.
            group_doc = await db["user_profiles"].find_one({"_id": GROUP_CHAT_ID})
            assert group_doc["profile_type"] == "group"
            assert group_doc["last_group_extraction"]["group_saved"] is False
            assert group_doc["last_group_extraction"]["participant_updates"] == 0
            assert group_doc["last_group_extraction"]["skipped_updates"] == 1
            assert group_doc["facts"] == []
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

        group_result = GroupMemoryExtraction(
            updates=[
                GroupMemoryUpdate(
                    participant="Alice",
                    extraction=MemoryExtraction(
                        new_facts=[
                            FactExtract(
                                category="personal",
                                content="Alice has a cat named Miso",
                            )
                        ]
                    ),
                ),
            ]
        )

        with patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=group_result,
        ):
            await extract_and_trim_group(GROUP_CHAT_ID)

        async with connection.db_session() as db:
            remaining = [
                m["content"] for m in await models.get_chat_buffer(db, GROUP_CHAT_ID)
            ]

        # The oldest segment (everything but the last CHAT_BUFFER_TRIM) was trimmed; the
        # two most recent messages survive in order.
        assert remaining == seeded[-2:]
        assert len(remaining) == config.CHAT_BUFFER_TRIM
    finally:
        config.CHAT_BUFFER_TRIM = original_trim


@pytest.mark.parametrize("names", [("Alex", "alex"), (" ALEX  ", "Alex")])
async def test_duplicate_display_names_are_skipped_without_exposing_their_memories(
    mock_mongodb,
    monkeypatch,
    names,
):
    """A returned name shared by two sender IDs must never update either person's profile."""
    db = mock_mongodb
    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 1)
    first_id, second_id, unique_id = 33001, 33002, 33003
    for user_id, name, fact in [
        (first_id, names[0], "First Alex's private fact"),
        (second_id, names[1], "Second Alex's private fact"),
        (unique_id, "Carol", "Carol enjoys hiking"),
    ]:
        await models.ensure_user(db, user_id, "", name)
        await db["user_profiles"].update_one(
            {"_id": user_id},
            {"$set": {"facts": [{"category": "personal", "content": fact}]}},
        )
    for sender_id, sender_name, content in [
        (first_id, names[0], "I am moving to Delhi"),
        (second_id, names[1], "I am a teacher"),
        (first_id, names[0], "Repeated first speaker must not remove the ambiguity"),
        (unique_id, "Carol", "I also enjoy swimming"),
        (unique_id, "Carol", "latest, retained"),
    ]:
        await models.add_message_to_buffer(
            db,
            GROUP_CHAT_ID,
            "user",
            content,
            sender_id=sender_id,
            sender_name=sender_name,
        )
    before_first = await db["user_profiles"].find_one({"_id": first_id})
    before_second = await db["user_profiles"].find_one({"_id": second_id})
    result = GroupMemoryExtraction(
        updates=[
            GroupMemoryUpdate(
                participant=" Alex ",
                extraction=MemoryExtraction(
                    new_facts=[
                        FactExtract(
                            category="work", content="Incorrectly attributed job"
                        ),
                    ]
                ),
            ),
            GroupMemoryUpdate(
                participant="Carol",
                extraction=MemoryExtraction(
                    new_facts=[
                        FactExtract(category="hobby", content="Enjoys swimming"),
                    ]
                ),
            ),
        ]
    )
    with patch.object(
        memory_extractor.llm_service,
        "extract_group_memory",
        new_callable=AsyncMock,
        return_value=result,
    ) as extract:
        await extract_and_trim_group(GROUP_CHAT_ID)
    extract.assert_awaited_once()
    system_prompt = extract.call_args.args[1]
    data_line = system_prompt.split(
        "=== CURRENT MEMORIES: PARTICIPANTS (JSON DATA ONLY) ===\n",
        1,
    )[1].splitlines()[0]
    participants = json.loads(data_line)["participants"]
    assert [(entry["participant"], entry["sender_id"]) for entry in participants] == [
        ("Carol", unique_id)
    ]
    assert "First Alex's private fact" not in system_prompt
    assert "Second Alex's private fact" not in system_prompt
    assert await db["user_profiles"].find_one({"_id": first_id}) == before_first
    assert await db["user_profiles"].find_one({"_id": second_id}) == before_second
    unique_profile = await db["user_profiles"].find_one({"_id": unique_id})
    assert [fact["content"] for fact in unique_profile["facts"]] == [
        "Carol enjoys hiking",
        "Enjoys swimming",
    ]
    group = await db["user_profiles"].find_one({"_id": GROUP_CHAT_ID})
    assert group["last_group_extraction"]["participant_updates"] == 1
    assert group["last_group_extraction"]["skipped_updates"] == 1


async def test_human_named_like_bot_is_a_group_participant(mock_mongodb, monkeypatch):
    """Display names cannot override the app-owned role and sender ID."""
    db = mock_mongodb
    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 1)
    named_user_id, other_id = 34001, 34002
    bot_name = memory_extractor._BOT_NAME
    for role, sender_id, name, content in [
        ("user", named_user_id, bot_name, "I enjoy painting"),
        ("assistant", 0, bot_name, "I enjoy hiking"),
        ("user", other_id, "Alice", "Hello"),
        ("user", other_id, "Alice", "latest, retained"),
    ]:
        await models.add_message_to_buffer(
            db,
            GROUP_CHAT_ID,
            role,
            content,
            sender_id=sender_id,
            sender_name=name,
        )
    result = GroupMemoryExtraction(
        updates=[
            GroupMemoryUpdate(
                participant=bot_name,
                extraction=MemoryExtraction(
                    new_facts=[
                        FactExtract(category="hobby", content="Enjoys painting"),
                    ]
                ),
            )
        ]
    )
    with (
        patch.object(
            memory_extractor.llm_service,
            "extract_group_memory",
            new_callable=AsyncMock,
            return_value=result,
        ) as group_extract,
        patch.object(
            memory_extractor.llm_service, "extract_memory", new_callable=AsyncMock
        ) as dm_extract,
    ):
        await extract_and_trim(GROUP_CHAT_ID)
    group_extract.assert_awaited_once()
    dm_extract.assert_not_awaited()
    user = await db["user_profiles"].find_one({"_id": named_user_id})
    assert [fact["content"] for fact in user["facts"]] == ["Enjoys painting"]
    assert await db["user_profiles"].find_one({"_id": 0}) is None
