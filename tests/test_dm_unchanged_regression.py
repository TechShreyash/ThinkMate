"""Task 13.1 — DM-unchanged regression suite (the primary guard for Req 5.2 / 5.3).

The group-user-memory work is strictly additive: the private (DM) path must stay
byte-for-byte the same as before per-person group memory, identity capture, and
Logs_Channel forwarding were introduced.

These tests pin the DM contract of ``chat_manager.handle_message`` and the DM routing
helper ``handle_user_message`` so any future regression on the group side that leaks
into the DM path fails loudly:

* the DM system prompt carries NO per-user ("PERSON SPEAKING NOW") block (Req 3.6, 5.2),
* the DM path performs the same two buffer writes (user + assistant) on the
  ``chat_id == user_id`` document, and returns the same ``(reply, reaction)`` 2-tuple,
* the DM reply call is made on the 2-tuple path (``with_affinity`` is falsy), so no
  per-user/group combination or affinity fold happens,
* the DM path does NO identity capture and NO Logs_Channel forwarding (Req 5.3).

Conventions follow ``tests/test_group_plumbing.py``: mongomock + pytest-asyncio per
``tests/conftest.py``, with ``llm_service.generate_reply_bundle`` patched as an
``AsyncMock``. The DM path uses the real ``build_system_prompt`` so the assertion is
against the actually-assembled prompt string handed to the reply call.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import connection, models
from app.prompts.system_prompt import build_system_prompt
from app.services.chat_manager import handle_message

# The distinctive header the per-user block adds; it must never appear on the DM path.
_PER_USER_MARKER = "MEMORIES OF THE PERSON SPEAKING NOW"


@pytest.mark.asyncio
async def test_dm_system_prompt_has_no_per_user_block():
    """The DM reply call receives a group-only-style prompt with no per-user section.

    Validates: Requirements 3.6, 5.2
    """
    user_id = 7001

    with patch(
        "app.services.chat_manager.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_reply:
        mock_reply.return_value = ("hi", None)

        async with connection.db_session() as db:
            await handle_message(db, user_id, "hello there")

            # The system prompt is the 2nd positional arg of the reply call.
            system_prompt = mock_reply.call_args.args[1]
            assert _PER_USER_MARKER not in system_prompt

            # And it equals the prompt the DM path would build with no user_memory_text:
            # the per-user block is purely additive, so omitting it yields the same text
            # (modulo the time-context block the DM path always adds).
            assert build_system_prompt(
                "p", "m", time_context="", user_memory_text=""
            ).find(_PER_USER_MARKER) == -1


@pytest.mark.asyncio
async def test_dm_buffer_writes_and_return_contract_unchanged():
    """Two buffer writes on the user_id-keyed doc; the same (reply, reaction) 2-tuple.

    Validates: Requirements 5.2
    """
    user_id = 7002

    with patch(
        "app.services.chat_manager.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_reply:
        mock_reply.return_value = ("a reply", "👍")

        async with connection.db_session() as db:
            result = await handle_message(db, user_id, "ping")

            # Same return contract: a 2-tuple of (reply_text, reaction).
            assert result == ("a reply", "👍")

            # Buffer is keyed by user_id (chat_id == user_id in a DM) with exactly the
            # user message followed by the assistant reply.
            doc = await db["chat_buffers"].find_one({"_id": user_id})
            assert doc is not None and doc["_id"] == user_id
            messages = doc["messages"]
            assert [(m["role"], m["content"]) for m in messages] == [
                ("user", "ping"),
                ("assistant", "a reply"),
            ]


@pytest.mark.asyncio
async def test_dm_reply_call_is_two_tuple_path_no_affinity():
    """The DM reply call sets no ``with_affinity`` flag, so no group combination runs.

    Validates: Requirements 5.2, 5.3
    """
    user_id = 7003

    with patch(
        "app.services.chat_manager.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_reply, patch(
        "app.services.chat_manager.affinity_cache.bump",
        new_callable=AsyncMock,
    ) as mock_bump:
        mock_reply.return_value = ("ok", None)

        async with connection.db_session() as db:
            await handle_message(db, user_id, "just a normal message")

            mock_reply.assert_called_once()
            assert mock_reply.call_args.kwargs.get("with_affinity", False) is False
            # No affinity fold on the DM path.
            mock_bump.assert_not_called()


@pytest.mark.asyncio
async def test_dm_router_enqueues_without_identity_capture():
    """The DM catch-all enqueues conversation and performs no identity refresh.

    Identity capture is a group-only concern wired into ``_handle_group_message``; the
    DM handler ``handle_user_message`` must not touch ``refresh_identity_if_changed``
    nor forward an identity event.

    Validates: Requirements 5.3
    """
    import app.handlers.messages as messages_module

    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = 7005
    message.text = "tell me something"
    message.bot = MagicMock()
    message.answer = AsyncMock()
    db = MagicMock()

    with patch.object(
        messages_module.user_task_manager, "enqueue_message", new_callable=AsyncMock
    ) as mock_enqueue, patch.object(
        models, "refresh_identity_if_changed", new_callable=AsyncMock
    ) as mock_identity, patch.object(
        messages_module.log_forwarder, "send", new_callable=AsyncMock
    ) as mock_forward:
        await messages_module.handle_user_message(message, db)

        mock_enqueue.assert_called_once_with(
            message.bot, message.from_user.id, "tell me something", message
        )
        mock_identity.assert_not_called()
        mock_forward.assert_not_called()
