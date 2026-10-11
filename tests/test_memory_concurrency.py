"""Regression coverage for memory writes and trim against changing snapshots."""

import asyncio
from unittest.mock import patch

import pytest

from app.config import config
from app.database import connection, models
from app.services import memory_compressor, memory_consolidator, memory_extractor
from app.services.schemas import (
    EmotionLog,
    EventExtract,
    EventUpdate,
    FactExtract,
    GroupMemoryExtraction,
    GroupMemoryUpdate,
    MemoryCompression,
    MemoryConsolidation,
    MemoryExtraction,
)


async def test_trim_snapshot_survives_hard_cap_rotation(monkeypatch):
    monkeypatch.setattr(config, "CHAT_BUFFER_HARD_CAP", 5)
    async with connection.db_session() as db:
        for number in range(5):
            await models.add_message_to_buffer(db, 71001, "user", f"m{number}")
        snapshot = (await db["chat_buffers"].find_one({"_id": 71001}))["messages"]
        for number in range(5, 8):
            await models.add_message_to_buffer(db, 71001, "user", f"m{number}")

        await models.delete_oldest_buffer_messages(db, 71001, 3, snapshot=snapshot)

        remaining = await models.get_chat_buffer(db, 71001)
        assert [message["content"] for message in remaining] == [
            "m3",
            "m4",
            "m5",
            "m6",
            "m7",
        ]


@pytest.mark.parametrize("legacy", [False, True])
async def test_whole_buffer_trim_preserves_append_during_write(legacy):
    async with connection.db_session() as db:
        if legacy:
            await db["chat_buffers"].insert_one(
                {"_id": 71002, "messages": [{"role": "user", "content": "old"}]}
            )
        else:
            await models.add_message_to_buffer(db, 71002, "user", "old")
        underlying = db["chat_buffers"]._collection
        original_update = underlying.update_one

        def append_then_trim(*args, **kwargs):
            original_update(
                {"_id": 71002},
                {
                    "$push": {
                        "messages": {
                            "message_id": "new-message",
                            "role": "user",
                            "content": "new",
                        }
                    }
                },
            )
            return original_update(*args, **kwargs)

        with patch.object(underlying, "update_one", side_effect=append_then_trim):
            trimmed = await models.delete_oldest_buffer_messages(db, 71002, 1)

        remaining = [
            message["content"] for message in await models.get_chat_buffer(db, 71002)
        ]
        assert remaining == (["old", "new"] if legacy else ["new"])
        assert trimmed == (not legacy)


@pytest.mark.parametrize("is_group", [False, True])
async def test_extractor_trims_only_messages_the_model_processed(monkeypatch, is_group):
    monkeypatch.setattr(config, "CHAT_BUFFER_HARD_CAP", 5)
    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    user_id = -71003 if is_group else 71003
    async with connection.db_session() as db:
        for number in range(5):
            await models.add_message_to_buffer(db, user_id, "user", f"m{number}")

        async def append_while_model_runs(*args, **kwargs):
            for number in range(5, 8):
                await models.add_message_to_buffer(db, user_id, "user", f"m{number}")
            if is_group:
                from app.services.schemas import GroupMemoryExtraction

                return GroupMemoryExtraction()
            return MemoryExtraction()

        method = "extract_group_memory" if is_group else "extract_memory"
        with patch.object(
            memory_extractor.llm_service, method, new=append_while_model_runs
        ):
            await memory_extractor.extract_and_trim(user_id, is_group=is_group)

        remaining = await models.get_chat_buffer(db, user_id)
        assert [message["content"] for message in remaining] == [
            "m3",
            "m4",
            "m5",
            "m6",
            "m7",
        ]


async def test_simultaneous_extractions_preserve_both_facts_and_moods():
    async with connection.db_session() as db:
        user_id = 71004
        await models.ensure_user(db, user_id, "u", "U")
        collection_type = type(db["user_profiles"])
        original_find = collection_type.find_one
        both_read = asyncio.Event()
        reads = 0

        async def read_snapshot_together(collection, *args, **kwargs):
            nonlocal reads
            snapshot = await original_find(collection, *args, **kwargs)
            reads += 1
            if reads <= 2:
                if reads == 2:
                    both_read.set()
                await both_read.wait()
            return snapshot

        with patch.object(collection_type, "find_one", new=read_snapshot_together):
            await asyncio.gather(
                *(
                    models.save_extracted_memories(
                        db,
                        user_id,
                        MemoryExtraction(
                            new_facts=[
                                FactExtract(
                                    category="personal", content=f"Fact {number}"
                                )
                            ],
                            emotional_state=EmotionLog(mood=f"mood{number}"),
                        ),
                    )
                    for number in range(2)
                )
            )

        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert {fact["content"] for fact in profile["facts"]} == {"Fact 0", "Fact 1"}
        assert {mood["mood"] for mood in profile["mood_history"]} == {"mood0", "mood1"}
        assert reads == 3  # losing writer retries against the winning write


@pytest.mark.parametrize("consolidate", [False, True])
async def test_maintenance_does_not_replace_memory_changed_during_llm_call(consolidate):
    module = memory_consolidator if consolidate else memory_compressor
    method = "consolidate_memory" if consolidate else "compress_memory"
    run = module.consolidate_user_memory if consolidate else module.compress_user_memory
    async with connection.db_session() as db:
        user_id = 71005
        await models.ensure_user(db, user_id, "u", "U")

        async def concurrent_extraction(*args, **kwargs):
            await models.save_extracted_memories(
                db,
                user_id,
                MemoryExtraction(
                    new_facts=[FactExtract(category="personal", content="Fresh fact")]
                ),
            )
            return MemoryConsolidation() if consolidate else MemoryCompression()

        with patch.object(module.llm_service, method, new=concurrent_extraction):
            await run(user_id)

        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert [fact["content"] for fact in profile["facts"]] == ["Fresh fact"]
        assert "last_consolidated_at" not in profile


async def test_budget_enforcement_does_not_overwrite_concurrent_memory(monkeypatch):
    monkeypatch.setattr(config, "USER_MEMORY_BUDGET_CHARS", 600)
    async with connection.db_session() as db:
        user_id = 71006
        await models.ensure_user(db, user_id, "u", "U")
        await models.save_extracted_memories(
            db,
            user_id,
            MemoryExtraction(
                new_facts=[
                    FactExtract(category="personal", content="Long fact " + "x" * 800)
                ]
            ),
        )
        original_update = models.update_memory_if_unchanged

        async def change_then_trim(database, snapshot, set_fields):
            await database["user_profiles"].update_one(
                {"_id": user_id}, {"$push": {"facts": {"content": "Fresh fact"}}}
            )
            return await original_update(database, snapshot, set_fields)

        with patch.object(models, "update_memory_if_unchanged", new=change_then_trim):
            await memory_compressor._enforce_budget(db, user_id)

        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert len(profile["facts"]) == 2
        assert profile["facts"][-1]["content"] == "Fresh fact"


async def test_updated_events_are_deduplicated_and_accept_legacy_metadata():
    async with connection.db_session() as db:
        user_id = 71007
        await models.ensure_user(db, user_id, "u", "U")
        await db["user_profiles"].update_one(
            {"_id": user_id}, {"$set": {"events": [{"description": "Old event"}]}}
        )
        await models.save_extracted_memories(
            db,
            user_id,
            MemoryExtraction(
                updated_events=[
                    EventUpdate(
                        old_description="Old event", new_description="New event"
                    ),
                    EventUpdate(
                        old_description="Old event", new_description=" NEW   EVENT "
                    ),
                ]
            ),
        )
        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert len(profile["events"]) == 1
        assert profile["events"][0]["description"] == "New event"
        assert profile["events"][0]["event_date"] is None

        await models.save_extracted_memories(
            db,
            user_id,
            MemoryExtraction(
                new_events=[
                    EventExtract(description="Same event", significance="minor")
                ],
                updated_events=[
                    EventUpdate(
                        old_description="New event", new_description="same EVENT"
                    )
                ],
            ),
        )
        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert len(profile["events"]) == 1


def test_close_db_closes_once_and_does_not_initialize(monkeypatch):
    from unittest.mock import Mock

    client = Mock()
    monkeypatch.setattr(connection, "_client", client)
    connection.close_db()
    connection.close_db()
    client.close.assert_called_once_with()
    assert connection._client is None


@pytest.mark.parametrize("is_group", [False, True])
@pytest.mark.parametrize("recreate", [False, True])
@pytest.mark.parametrize("initial_profile", ["absent", "legacy", "current"])
async def test_reset_during_extraction_rejects_obsolete_output(
    monkeypatch, is_group, recreate, initial_profile
):
    """A reset invalidates pending output even after an identical-time recreation."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    if is_group:
        # Generation capture also covers owners whose context is omitted entirely.
        monkeypatch.setattr(config, "USER_MEMORY_BUDGET_CHARS", 0)
    user_id = 71101
    chat_id = -71101 if is_group else user_id
    async with connection.db_session() as db:
        if initial_profile == "legacy":
            await db["user_profiles"].insert_one({"_id": user_id, "facts": []})
        elif initial_profile == "current":
            await models.ensure_user(db, user_id, "u", "User")
        for number in range(5):
            await models.add_message_to_buffer(
                db,
                chat_id,
                "user",
                f"m{number}",
                sender_id=user_id,
                sender_name="User",
            )

        stale = MemoryExtraction(
            new_facts=[FactExtract(category="personal", content="Obsolete fact")]
        )

        async def reset_while_model_runs(*args, **kwargs):
            before = await db["user_profiles"].find_one({"_id": user_id})
            assert before["memory_generation"]
            await models.reset_user(db, user_id)
            if recreate:
                await models.ensure_user(db, user_id, "fresh", "Fresh")
                # Creation time alone cannot distinguish an identical-millisecond
                # recreation, while the generation token always can.
                await db["user_profiles"].update_one(
                    {"_id": user_id}, {"$set": {"created_at": before.get("created_at")}}
                )
                after = await db["user_profiles"].find_one({"_id": user_id})
                assert after["memory_generation"] != before["memory_generation"]
            if not is_group:
                await models.add_message_to_buffer(db, user_id, "user", "after reset")
            return (
                GroupMemoryExtraction(
                    updates=[GroupMemoryUpdate(participant="User", extraction=stale)]
                )
                if is_group
                else stale
            )

        method = "extract_group_memory" if is_group else "extract_memory"
        forwarded = AsyncMock()
        with (
            patch.object(
                memory_extractor.llm_service, method, new=reset_while_model_runs
            ),
            patch.object(memory_extractor.log_forwarder, "send", new=forwarded),
        ):
            await memory_extractor.extract_and_trim(chat_id, is_group=is_group)

        after = await db["user_profiles"].find_one({"_id": user_id})
        if recreate:
            assert after["facts"] == []
            assert after["username"] == "fresh"
        else:
            assert after is None
        forwarded.assert_not_awaited()
        if is_group:
            group = await db["user_profiles"].find_one({"_id": chat_id})
            assert group["last_group_extraction"]["participant_updates"] == 0
            assert group["last_group_extraction"]["skipped_updates"] == 1
        else:
            remaining = await models.get_chat_buffer(db, user_id)
            assert [message["content"] for message in remaining] == ["after reset"]


async def test_legacy_generation_initialization_does_not_recreate_deleted_profile():
    async with connection.db_session() as db:
        user_id = 71102
        await db["user_profiles"].insert_one({"_id": user_id, "facts": []})
        underlying = db["user_profiles"]._collection
        original_update = underlying.find_one_and_update

        def delete_before_initialization(*args, **kwargs):
            underlying.delete_one({"_id": user_id})
            return original_update(*args, **kwargs)

        with patch.object(
            underlying, "find_one_and_update", side_effect=delete_before_initialization
        ):
            generation = await models.prepare_memory_generation(db, user_id)

        assert generation is None
        assert await db["user_profiles"].find_one({"_id": user_id}) is None


async def test_extraction_conflict_retry_does_not_recreate_reset_profile():
    async with connection.db_session() as db:
        user_id = 71103
        await models.ensure_user(db, user_id, "u", "User")
        generation = await models.prepare_memory_generation(db, user_id)
        original_update = models.update_memory_if_unchanged

        async def reset_before_write(database, snapshot, fields):
            await models.reset_user(database, user_id)
            return await original_update(database, snapshot, fields)

        with patch.object(models, "update_memory_if_unchanged", new=reset_before_write):
            saved = await models.save_extracted_memories(
                db,
                user_id,
                MemoryExtraction(
                    new_facts=[FactExtract(category="personal", content="Old fact")]
                ),
                expected_generation=generation,
            )

        assert saved is False
        assert await db["user_profiles"].find_one({"_id": user_id}) is None


async def test_reset_shared_group_during_extraction_does_not_recreate_marker(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    chat_id = -71104
    async with connection.db_session() as db:
        for number in range(5):
            await models.add_message_to_buffer(db, chat_id, "user", f"m{number}")

        async def reset_during_llm(*args, **kwargs):
            await models.reset_user(db, chat_id)
            await models.add_message_to_buffer(db, chat_id, "user", "after reset")
            return GroupMemoryExtraction(
                group_extraction=MemoryExtraction(
                    new_facts=[
                        FactExtract(category="relationship", content="Old group fact")
                    ]
                )
            )

        forwarded = AsyncMock()
        with (
            patch.object(
                memory_extractor.llm_service,
                "extract_group_memory",
                new=reset_during_llm,
            ),
            patch.object(memory_extractor.log_forwarder, "send", new=forwarded),
        ):
            await memory_extractor.extract_and_trim(chat_id, is_group=True)

        assert await db["user_profiles"].find_one({"_id": chat_id}) is None
        forwarded.assert_not_awaited()
        remaining = await models.get_chat_buffer(db, chat_id)
        assert [message["content"] for message in remaining] == ["after reset"]


async def test_dm_generation_precedes_transcript_snapshot(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    user_id = 71105
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "User")
        for number in range(5):
            await models.add_message_to_buffer(db, user_id, "user", f"old{number}")
        original_read = memory_extractor._read_raw_buffer

        async def reset_during_transcript_read(database, owner_id):
            messages = await original_read(database, owner_id)
            await models.reset_user(database, owner_id)
            await models.add_message_to_buffer(
                database, owner_id, "user", "after reset"
            )
            return messages

        obsolete = MemoryExtraction(
            new_facts=[FactExtract(category="personal", content="Old fact")]
        )
        with (
            patch.object(
                memory_extractor, "_read_raw_buffer", new=reset_during_transcript_read
            ),
            patch.object(
                memory_extractor.llm_service,
                "extract_memory",
                new=AsyncMock(return_value=obsolete),
            ),
        ):
            await memory_extractor.extract_and_trim(user_id, is_group=False)

        assert await db["user_profiles"].find_one({"_id": user_id}) is None
        messages = await models.get_chat_buffer(db, user_id)
        assert [message["content"] for message in messages] == ["after reset"]


@pytest.mark.parametrize("recreate", [False, True])
async def test_dm_failed_attempt_does_not_adopt_reset_generation(monkeypatch, recreate):
    """Retries from an obsolete run must leave the new profile and buffer untouched."""
    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    user_id = 71201
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "User")
        for number in range(5):
            await models.add_message_to_buffer(db, user_id, "user", f"old{number}")
        calls = 0

        async def failed_call_after_reset(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await models.reset_user(db, user_id)
                if recreate:
                    await models.ensure_user(db, user_id, "fresh", "Fresh")
                for number in range(5):
                    await models.add_message_to_buffer(
                        db, user_id, "user", f"new{number}"
                    )
                return None
            return MemoryExtraction(
                new_facts=[FactExtract(category="personal", content="Obsolete run")]
            )

        with patch.object(
            memory_extractor.llm_service, "extract_memory", new=failed_call_after_reset
        ):
            await memory_extractor.extract_and_trim(user_id, is_group=False)

        assert calls == 1
        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert (profile["facts"] == []) if recreate else (profile is None)
        messages = await models.get_chat_buffer(db, user_id)
        assert [message["content"] for message in messages] == [
            f"new{number}" for number in range(5)
        ]


@pytest.mark.parametrize("recreate", [False, True])
async def test_group_retry_keeps_original_owner_generation(monkeypatch, recreate):
    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    user_id, chat_id = 71202, -71202
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "User")
        for number in range(5):
            await models.add_message_to_buffer(
                db,
                chat_id,
                "user",
                f"old{number}",
                sender_id=user_id,
                sender_name="User",
            )
        calls = 0

        async def retry_after_reset(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await models.reset_user(db, user_id)
                if recreate:
                    await models.ensure_user(db, user_id, "fresh", "Fresh")
                return None
            return GroupMemoryExtraction(
                updates=[
                    GroupMemoryUpdate(
                        participant="User",
                        extraction=MemoryExtraction(
                            new_facts=[
                                FactExtract(category="personal", content="Obsolete run")
                            ]
                        ),
                    )
                ]
            )

        with patch.object(
            memory_extractor.llm_service, "extract_group_memory", new=retry_after_reset
        ):
            await memory_extractor.extract_and_trim(chat_id, is_group=True)

        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert (profile["facts"] == []) if recreate else (profile is None)
        group = await db["user_profiles"].find_one({"_id": chat_id})
        assert group["last_group_extraction"]["participant_updates"] == 0
        assert group["last_group_extraction"]["skipped_updates"] == 1


@pytest.mark.parametrize("consolidate", [False, True])
async def test_maintenance_budget_pass_does_not_trim_recreated_profile(
    monkeypatch, consolidate
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(config, "USER_MEMORY_BUDGET_CHARS", 600)
    user_id = 71203
    module = memory_consolidator if consolidate else memory_compressor
    operation = "apply_consolidation" if consolidate else "replace_user_memory"
    method = "consolidate_memory" if consolidate else "compress_memory"
    run = module.consolidate_user_memory if consolidate else module.compress_user_memory
    result = MemoryConsolidation() if consolidate else MemoryCompression()
    async with connection.db_session() as db:
        await models.ensure_user(db, user_id, "u", "User")
        original_apply = getattr(models, operation)
        new_fact = "Fresh profile " + "x" * 1000

        async def reset_after_apply(*args, **kwargs):
            applied = await original_apply(*args, **kwargs)
            await models.reset_user(db, user_id)
            await models.ensure_user(db, user_id, "fresh", "Fresh")
            await models.save_extracted_memories(
                db,
                user_id,
                MemoryExtraction(
                    new_facts=[FactExtract(category="personal", content=new_fact)]
                ),
            )
            return applied

        with (
            patch.object(
                module.llm_service, method, new=AsyncMock(return_value=result)
            ),
            patch.object(models, operation, new=reset_after_apply),
        ):
            await run(user_id)

        profile = await db["user_profiles"].find_one({"_id": user_id})
        assert [fact["content"] for fact in profile["facts"]] == [new_fact]


@pytest.mark.parametrize("is_group", [False, True])
async def test_failed_extraction_preserves_arrivals_during_final_attempt(
    monkeypatch, is_group
):
    monkeypatch.setattr(config, "CHAT_BUFFER_TRIM", 2)
    chat_id = -71204 if is_group else 71204
    async with connection.db_session() as db:
        for number in range(5):
            await models.add_message_to_buffer(db, chat_id, "user", f"old{number}")
        calls = 0

        async def fail_with_final_arrivals(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == memory_extractor.MAX_EXTRACTION_ATTEMPTS:
                for number in range(3):
                    await models.add_message_to_buffer(
                        db, chat_id, "user", f"new{number}"
                    )

        method = "extract_group_memory" if is_group else "extract_memory"
        with patch.object(
            memory_extractor.llm_service, method, new=fail_with_final_arrivals
        ):
            await memory_extractor.extract_and_trim(chat_id, is_group=is_group)

        messages = await models.get_chat_buffer(db, chat_id)
        assert [message["content"] for message in messages] == [
            "old3",
            "old4",
            "new0",
            "new1",
            "new2",
        ]
