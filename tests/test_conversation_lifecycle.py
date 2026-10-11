"""Regression tests for conversation task ownership and member concurrency."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import config
from app.database import connection, models
from app.services.affinity import AffinityCache
from app.services.chat_manager import EMPTY_REPLY_FALLBACK, handle_message
from app.services.group_gate import (
    SpamBurstDetector,
    count_distinct_mentions,
    is_addressed,
)
from app.services.telegram_text import apply_response_cap
from app.services.user_task_manager import UserTaskManager


def _message(chat_id=42):
    message = MagicMock()
    message.chat.id = chat_id
    message.chat.type = "private"
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_enqueue_preserves_reply_already_running(monkeypatch):
    monkeypatch.setattr(config, "MESSAGE_BATCH_DELAY_SECS", 0.01)
    manager = UserTaskManager()
    bot = MagicMock(send_chat_action=AsyncMock())
    first, second = _message(), _message()
    started, release = asyncio.Event(), asyncio.Event()

    async def reply(_db, _chat_id, text, **_kwargs):
        if text == "first":
            started.set()
            await release.wait()
        return f"answer to {text}", None

    try:
        with patch("app.services.user_task_manager.handle_message", side_effect=reply):
            await manager.enqueue_message(bot, 42, "first", first)
            await asyncio.wait_for(started.wait(), timeout=1)
            await manager.enqueue_message(bot, 42, "second", second)
            await asyncio.sleep(0.03)
            release.set()
            await manager.shutdown(timeout=1)

        first.answer.assert_awaited_once_with("answer to first")
        second.answer.assert_awaited_once_with("answer to second")
        assert not manager._tasks
    finally:
        release.set()
        await manager.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_shutdown_flushes_pending_batch_and_stops_admission(monkeypatch):
    monkeypatch.setattr(config, "MESSAGE_BATCH_DELAY_SECS", 100)
    monkeypatch.setattr(config, "MAX_BATCH_DELAY_SECS", 100)
    manager = UserTaskManager()
    bot = MagicMock(send_chat_action=AsyncMock())
    message = _message()
    with patch(
        "app.services.user_task_manager.handle_message",
        new_callable=AsyncMock,
        return_value=("delivered", None),
    ) as handle:
        await manager.enqueue_message(bot, 42, "queued", message)
        await manager.shutdown(timeout=1)
        await manager.enqueue_message(bot, 42, "too late", message)

    handle.assert_awaited_once()
    message.answer.assert_awaited_once_with("delivered")
    assert not manager._tasks
    assert not manager._states


@pytest.mark.asyncio
async def test_new_batch_restarts_typing_before_old_task_finishes(monkeypatch):
    monkeypatch.setattr(config, "MESSAGE_BATCH_DELAY_SECS", 100)
    manager = UserTaskManager()
    bot = MagicMock(send_chat_action=AsyncMock())
    message = _message()
    try:
        await manager.enqueue_message(bot, 42, "first", message)
        state = await manager.get_state(42)
        old_typing = state.typing_task
        manager._stop_typing(state)
        await manager.enqueue_message(bot, 42, "second", message)
        assert state.typing_task is not old_typing
        assert not state.typing_stop_event.is_set()
    finally:
        with patch(
            "app.services.user_task_manager.handle_message",
            new_callable=AsyncMock,
            return_value=("reply", None),
        ):
            await manager.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_shutdown_bounds_and_awaits_running_memory_work():
    manager = UserTaskManager()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def extract(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch.object(manager, "run_extractor", side_effect=extract):
        manager.schedule_extractor(42)
        await asyncio.wait_for(started.wait(), timeout=1)
        await manager.shutdown(timeout=0.01)
        manager.schedule_extractor(43)

    assert cancelled.is_set()
    assert not manager._tasks


@pytest.mark.asyncio
async def test_reset_stops_prior_work_and_defers_fresh_messages(monkeypatch):
    monkeypatch.setattr(config, "MESSAGE_BATCH_DELAY_SECS", 0.01)
    manager = UserTaskManager()
    bot = MagicMock(send_chat_action=AsyncMock())
    first, queued, fresh = _message(), _message(), _message()
    reply_started, reply_cancelled = asyncio.Event(), asyncio.Event()
    memory_started = {42: asyncio.Event(), 43: asyncio.Event()}
    memory_cancelled = set()
    release_other = asyncio.Event()

    async def reply(db, chat_id, text, **_kwargs):
        await models.add_message_to_buffer(db, chat_id, "user", text)
        if text == "prior":
            reply_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                reply_cancelled.set()
        await models.add_message_to_buffer(db, chat_id, "assistant", f"reply to {text}")
        return f"reply to {text}", None

    async def extract(chat_id, **_kwargs):
        memory_started[chat_id].set()
        try:
            await release_other.wait()
        except asyncio.CancelledError:
            memory_cancelled.add(chat_id)
            raise

    try:
        with (
            patch("app.services.user_task_manager.handle_message", side_effect=reply),
            patch.object(manager, "run_extractor", side_effect=extract),
        ):
            await manager.enqueue_message(bot, 42, "prior", first)
            await asyncio.wait_for(reply_started.wait(), timeout=1)
            await manager.enqueue_message(bot, 42, "queued prior", queued)
            manager.schedule_extractor(42)
            manager.schedule_extractor(43)
            for event in memory_started.values():
                await asyncio.wait_for(event.wait(), timeout=1)

            async with manager.reset_conversation(42):
                assert reply_cancelled.is_set()
                assert memory_cancelled == {42}
                assert 42 not in manager._states
                fresh_enqueue = asyncio.create_task(
                    manager.enqueue_message(bot, 42, "fresh", fresh)
                )
                await asyncio.sleep(0)
                assert not fresh_enqueue.done()
                async with connection.db_session() as db:
                    await models.reset_user(db, 42)

            await asyncio.wait_for(fresh_enqueue, timeout=1)
            release_other.set()
            await manager.shutdown(timeout=1)

        first.answer.assert_not_awaited()
        queued.answer.assert_not_awaited()
        fresh.answer.assert_awaited_once_with("reply to fresh")
        async with connection.db_session() as db:
            history = await models.get_chat_buffer(db, 42)
        assert [message["content"] for message in history] == [
            "fresh",
            "reply to fresh",
        ]
        assert not manager._tasks
        assert not manager._resets
    finally:
        release_other.set()
        await manager.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_failed_reset_releases_waiting_messages():
    manager = UserTaskManager()
    bot = MagicMock(send_chat_action=AsyncMock())
    message = _message()
    with patch(
        "app.services.user_task_manager.handle_message",
        new_callable=AsyncMock,
        return_value=("delivered", None),
    ) as handle:
        with pytest.raises(RuntimeError, match="storage unavailable"):
            async with manager.reset_conversation(42):
                fresh_enqueue = asyncio.create_task(
                    manager.enqueue_message(bot, 42, "fresh", message)
                )
                await asyncio.sleep(0)
                assert not fresh_enqueue.done()
                raise RuntimeError("storage unavailable")

        await asyncio.wait_for(fresh_enqueue, timeout=1)
        await manager.shutdown(timeout=1)
    assert not manager._resets
    handle.assert_awaited_once()
    message.answer.assert_awaited_once_with("delivered")


@pytest.mark.asyncio
async def test_reset_cancels_tracked_delivery_and_refuses_work_while_held():
    manager = UserTaskManager()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def send():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = manager.start_conversation_task(42, send(), name="proactive-send:42")
    await asyncio.wait_for(started.wait(), timeout=1)
    async with manager.reset_conversation(42):
        assert task.cancelled()
        assert cancelled.is_set()
        assert (
            manager.start_conversation_task(42, send(), name="proactive-send:42")
            is None
        )
    assert not manager._tasks


@pytest.mark.asyncio
async def test_group_batch_preserves_interleaved_speaker_order():
    manager = UserTaskManager()
    chat_id = -98765430
    state = await manager.get_state(chat_id)
    for sender, text in [(1, "one"), (1, "two"), (2, "other"), (1, "three")]:
        message = _message(chat_id)
        message.chat.type = "supergroup"
        message.reply = AsyncMock()
        state.pending_messages.append(
            {
                "text": text,
                "user_id": sender,
                "message": message,
                "sender_name": f"User {sender}",
                "reason": "reply",
            }
        )

    with patch(
        "app.services.user_task_manager.handle_message",
        new_callable=AsyncMock,
        return_value=("reply", None),
    ) as handle:
        await manager._process_batch(chat_id)

    assert [call.args[2] for call in handle.await_args_list] == [
        "one\ntwo",
        "other",
        "three",
    ]
    assert [call.kwargs["sender_id"] for call in handle.await_args_list] == [1, 2, 1]


@pytest.mark.asyncio
async def test_concurrent_affinity_signals_do_not_lose_updates():
    async with connection.db_session() as db:
        await models.upsert_chat_member(db, -100, 42, affinity=0.25)
        cache = AffinityCache()
        await cache.get(db, -100, 42)
        upsert = models.upsert_chat_member

        async def slow_upsert(*args, **kwargs):
            await asyncio.sleep(0.01)
            return await upsert(*args, **kwargs)

        with patch(
            "app.services.affinity.models.upsert_chat_member", side_effect=slow_upsert
        ):
            await asyncio.gather(*(cache.bump(db, -100, 42, 0.1) for _ in range(3)))

        member = await models.get_chat_member(db, -100, 42)
        assert member["affinity"] == pytest.approx(0.55)
        assert (await cache.get(db, -100, 42))["affinity"] == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_cold_mode_write_preserves_persisted_affinity_and_normalized_mode():
    async with connection.db_session() as db:
        await models.upsert_chat_member(db, -100, 42, affinity=0.8)
        cache = AffinityCache()
        await cache.set_mode(db, -100, 42, "quiet")
        assert await cache.get(db, -100, 42) == {"affinity": 0.8, "mode": "quiet"}
        await cache.set_mode(db, -100, 42, "invalid")
        assert await cache.get(db, -100, 42) == {"affinity": 0.8, "mode": "auto"}


@pytest.mark.asyncio
@pytest.mark.parametrize("affinity", [None, "invalid", float("nan"), float("inf")])
async def test_invalid_persisted_affinity_degrades_to_existing_default(affinity):
    async with connection.db_session() as db:
        await db["chat_members"].insert_one(
            {"_id": "-100:42", "affinity": affinity, "mode": "quiet"}
        )
        result = await AffinityCache().get(db, -100, 42)
    assert result == {"affinity": config.AFFINITY_DEFAULT, "mode": "quiet"}


@pytest.mark.asyncio
async def test_optional_affinity_failure_does_not_discard_generated_reply():
    async with connection.db_session() as db:
        with (
            patch(
                "app.services.chat_manager.llm_service.generate_reply_bundle",
                new_callable=AsyncMock,
                return_value=("still delivered", None, 0.1),
            ),
            patch(
                "app.services.chat_manager.affinity_cache.bump",
                new_callable=AsyncMock,
                side_effect=RuntimeError("affinity write unavailable"),
            ),
        ):
            result = await handle_message(
                db,
                -100,
                "stop",
                chat_type="supergroup",
                sender_id=42,
                sender_name="Alice",
            )

        assert result == ("still delivered", None)
        history = await models.get_chat_buffer(db, -100)
        assert history[-1]["content"] == "still delivered"


@pytest.mark.asyncio
async def test_ambient_decline_does_not_add_blank_assistant_history():
    async with connection.db_session() as db:
        with patch(
            "app.services.chat_manager.llm_service.generate_reply_bundle",
            new_callable=AsyncMock,
            return_value=("  ", "👍", 0),
        ):
            result = await handle_message(
                db,
                -100,
                "hello",
                chat_type="supergroup",
                sender_id=42,
                sender_name="Alice",
                reason="ambient",
            )

        assert result == ("", None)
        history = await models.get_chat_buffer(db, -100)
        assert [message["role"] for message in history] == ["user"]


@pytest.mark.asyncio
async def test_empty_direct_reply_persists_delivered_fallback():
    async with connection.db_session() as db:
        with patch(
            "app.services.chat_manager.llm_service.generate_reply_bundle",
            new_callable=AsyncMock,
            return_value=("", None),
        ):
            result = await handle_message(db, 42, "hello")

        assert result == (EMPTY_REPLY_FALLBACK, None)
        history = await models.get_chat_buffer(db, 42)
        assert history[-1]["content"] == EMPTY_REPLY_FALLBACK


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["private", "supergroup"])
async def test_reply_history_uses_same_response_cap_as_delivery(chat_type, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESPONSE_CHARS", 30)
    reply = "long reply " * 20
    bundle = (reply, None, 0) if chat_type == "supergroup" else (reply, None)
    async with connection.db_session() as db:
        with patch(
            "app.services.chat_manager.llm_service.generate_reply_bundle",
            new_callable=AsyncMock,
            return_value=bundle,
        ):
            result = await handle_message(db, 42, "hello", chat_type=chat_type)
        history = await models.get_chat_buffer(db, 42)
    assert result == (apply_response_cap(reply), None)
    assert history[-1]["content"] == result[0]


def test_mention_offsets_use_utf16_after_astral_emoji():
    text = "😀 @Alice @alice"
    entities = [
        SimpleNamespace(type="mention", offset=3, length=6),
        SimpleNamespace(type="mention", offset=10, length=6),
    ]
    assert count_distinct_mentions(text, entities) == 1
    assert SpamBurstDetector._strip_and_normalize(text, entities) == "😀"


def test_text_mention_can_address_bot_by_linked_username():
    entity = SimpleNamespace(
        type="text_mention",
        offset=3,
        length=5,
        user=SimpleNamespace(username="ThinkMateBot"),
    )
    assert is_addressed(
        text="😀 Buddy",
        entities=[entity],
        reply_to_bot=False,
        bot_username="ThinkMateBot",
        bot_name="ThinkMate",
    )


def test_malformed_surrogate_offset_is_ignored():
    entity = SimpleNamespace(type="mention", offset=1, length=1)
    assert count_distinct_mentions("😀 @alice", [entity]) == 0
