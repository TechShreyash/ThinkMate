"""Chat-context plumbing + DM backward-compat tests (Task 2.3).

Covers Requirements 1.1, 1.2, 1.5, 1.6, 1.7, 2.7 of the group-chat spec:

- A DM call ``handle_message(db, user_id, text)`` behaves exactly as before — one reply
  call on the 2-tuple DM path (no ``with_affinity=True``), the same ``chat_buffers._id``
  (``== user_id``), and a single-party ``{role, content}`` history (no ``"Name: "``
  prefixing) even though sender attribution is stored on each message.
- The group path renders multi-party history (``"Alice: hi"``) and calls the reply with
  ``with_affinity=True`` (the 3-tuple group path).
- ``enqueue_message`` keys its pending/batching state on ``chat_id``.

All tests use mongomock + pytest-asyncio per ``tests/conftest.py``. The LLM is patched
with ``AsyncMock`` exactly as in ``tests/test_batching_and_concurrency.py``; the patch
target is ``app.services.chat_manager.llm_service.generate_reply_bundle`` because
``chat_manager`` does ``from app.services.llm_service import llm_service``.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import config
from app.database import connection, models
from app.services.chat_manager import handle_message
from app.services.user_task_manager import UserTaskManager


# --------------------------------------------------------------------------- #
# 1. DM handle_message is unchanged (single reply call, DM 2-tuple path,
#    buffer _id == user_id, single-party history).
#    Validates: Requirements 1.1, 1.2, 1.5, 1.6, 1.7
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dm_handle_message_unchanged():
    user_id = 4242

    with patch(
        "app.services.chat_manager.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_reply:
        # DM path returns the legacy 2-tuple.
        mock_reply.return_value = ("hi there", None)

        async with connection.db_session() as db:
            result = await handle_message(db, user_id, "hello")

            # Return contract is the same (reply, reaction) 2-tuple.
            assert result == ("hi there", None)

            # Exactly one reply call, made on the DM (2-tuple) path: with_affinity is
            # NOT set / falsy.
            mock_reply.assert_called_once()
            assert mock_reply.call_args.kwargs.get("with_affinity", False) is False

            # The on-disk buffer doc is keyed by user_id (chat_id == user_id in a DM) and
            # contains the user message + assistant reply.
            doc = await db["chat_buffers"].find_one({"_id": user_id})
            assert doc is not None
            assert doc["_id"] == user_id

            messages = doc["messages"]
            assert len(messages) == 2
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "hello"
            assert messages[1]["role"] == "assistant"
            assert messages[1]["content"] == "hi there"
            # Sender fields may exist on disk, but each message still carries role/content.
            for m in messages:
                assert "role" in m and "content" in m


# --------------------------------------------------------------------------- #
# 2. DM history rendered to the reply call is single-party {role, content}
#    with NO "Name: " prefixing, even across a multi-message buffer.
#    Validates: Requirements 1.7, 2.7 (DM single-party contrast)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dm_history_is_single_party():
    user_id = 5151

    with patch(
        "app.services.chat_manager.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_reply:
        mock_reply.return_value = ("reply two", None)

        async with connection.db_session() as db:
            # Seed a couple of prior turns (stored with sender attribution).
            await models.add_message_to_buffer(
                db, user_id, "user", "first user msg",
                sender_id=user_id, sender_name="Bob",
            )
            await models.add_message_to_buffer(
                db, user_id, "assistant", "first reply",
                sender_id=0, sender_name="ThinkMate",
            )

            await handle_message(db, user_id, "second user msg")

            # The history passed to the reply call is the 3rd positional arg.
            history = mock_reply.call_args.args[2]
            assert isinstance(history, list)
            # Single-party shape: every rendered turn has exactly role/content keys.
            for turn in history:
                assert set(turn.keys()) == {"role", "content"}

            # The user turns are the raw text, never "Bob: ..." (no name prefixing in DMs).
            contents = [t["content"] for t in history]
            assert "first user msg" in contents
            assert "second user msg" in contents
            assert all(not c.startswith("Bob: ") for c in contents)


# --------------------------------------------------------------------------- #
# 3. Group handle_message renders multi-party history and calls the reply with
#    with_affinity=True (the 3-tuple group path).
#    Validates: Requirements 1.1, 2.7
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_group_handle_message_multiparty():
    chat_id = -100

    with patch(
        "app.services.chat_manager.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_reply, patch(
        "app.services.chat_manager.affinity_cache.bump",
        new_callable=AsyncMock,
    ) as mock_bump:
        # Group path returns the 3-tuple (reply, reaction, affinity_delta).
        mock_reply.return_value = ("hey all", None, 0.0)

        async with connection.db_session() as db:
            reply, reaction = await handle_message(
                db,
                chat_id=chat_id,
                user_text="hi",
                chat_type="group",
                sender_id=111,
                sender_name="Alice",
            )

            assert reply == "hey all"
            assert reaction is None

            # The group path calls the reply with with_affinity=True.
            mock_reply.assert_called_once()
            assert mock_reply.call_args.kwargs.get("with_affinity") is True

            # The user turn is rendered multi-party, prefixed "Alice: hi".
            history = mock_reply.call_args.args[2]
            user_turns = [t for t in history if t["role"] == "user"]
            assert any(t["content"] == "Alice: hi" for t in user_turns)

            # affinity_delta == 0.0 means the fold is skipped (no bump for that signal),
            # and "hi" is not a negative-signal keyword, so no affinity churn at all.
            mock_bump.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. enqueue_message keys its pending/batching state on chat_id (a group
#    batches per chat, distinct from the speaking user's id).
#    Validates: Requirements 1.1, 1.5 (chat_id-keyed plumbing)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enqueue_message_batches_by_chat_id():
    chat_id = -100789
    sender_user_id = 222  # a different id than the chat id

    manager = UserTaskManager()

    mock_bot = MagicMock()
    mock_bot.send_chat_action = AsyncMock()
    mock_message = MagicMock()
    mock_message.chat.id = chat_id
    mock_message.chat.type = "supergroup"
    mock_message.answer = AsyncMock()

    # Patch handle_message so the batch (if it fires) does no real work.
    with patch(
        "app.services.user_task_manager.handle_message",
        new_callable=AsyncMock,
    ) as mock_handle:
        mock_handle.return_value = ("ok", None)

        await manager.enqueue_message(
            mock_bot,
            chat_id,
            "hello group",
            mock_message,
            user_id=sender_user_id,
            chat_type="supergroup",
            sender_name="Alice",
        )

        # Pending state is stored under the chat_id key, not the sender's user_id.
        assert chat_id in manager._states
        assert sender_user_id not in manager._states

        state = manager._states[chat_id]
        assert len(state.pending_messages) == 1
        pending = state.pending_messages[0]
        assert pending["text"] == "hello group"
        assert pending["user_id"] == sender_user_id
        assert pending["sender_name"] == "Alice"

        # Clean up timers/tasks so teardown is quiet.
        if state.batch_task and not state.batch_task.done():
            state.batch_task.cancel()
        manager._stop_typing(state)
        await asyncio.sleep(0)
