"""Routing & identity tests for Phase 9 group chat (Task 3.3).

Two layers of coverage:

A) Pure unit tests for :func:`app.services.group_gate.is_addressed` — no mocks
   needed. These pin the addressed-detection contract: @mention of the bot,
   bot-name standalone token (case-insensitive, word-bounded), reply-to-bot, and
   the negative cases (plain message, substring-of-another-word).

B) Routing tests for ``handle_user_message`` / ``_handle_group_message`` with a
   mocked aiogram ``Message`` and patched hot-path dependencies. These pin the
   chat-type branch (private/group/channel), the addressed → reply vs.
   non-addressed → ambient-gate handoff, the single-write buffer invariant, and
   the command guard.

All tests use mocked aiogram ``Message`` objects (per ``tests/test_command_skip.py``
style) and ``unittest.mock`` — no real LLM, network, or DB.

**Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.handlers.messages as messages_module
from app.services.group_gate import is_addressed


# ===========================================================================
# A) Pure unit tests for is_addressed (no mocks).
# ===========================================================================

def test_is_addressed_mention_of_bot_username_returns_true():
    """An @mention of the bot's username classifies as addressed (Req 2.2)."""
    assert (
        is_addressed(
            text="hey @ThinkMateBot how are you",
            entities=None,
            reply_to_bot=False,
            bot_username="ThinkMateBot",
            bot_name="ThinkMate",
        )
        is True
    )


def test_is_addressed_bot_name_standalone_token_returns_true():
    """The bot name as a standalone token classifies as addressed (Req 2.4)."""
    assert (
        is_addressed(
            text="thinkmate what do you think",
            entities=None,
            reply_to_bot=False,
            bot_username="ThinkMateBot",
            bot_name="ThinkMate",
        )
        is True
    )


def test_is_addressed_reply_to_bot_returns_true_regardless_of_text():
    """A reply to the bot is addressed regardless of message text (Req 2.3)."""
    assert (
        is_addressed(
            text="anything at all, no mention here",
            entities=None,
            reply_to_bot=True,
            bot_username="ThinkMateBot",
            bot_name="ThinkMate",
        )
        is True
    )


def test_is_addressed_plain_message_returns_false():
    """A plain message with no mention/name/reply is NOT addressed (Req 2.5)."""
    assert (
        is_addressed(
            text="just chatting with friends about lunch",
            entities=None,
            reply_to_bot=False,
            bot_username="ThinkMateBot",
            bot_name="ThinkMate",
        )
        is False
    )


def test_is_addressed_name_as_substring_does_not_match():
    """The bot name as a substring of another word must NOT match (word-boundary)."""
    assert (
        is_addressed(
            text="thinkmately speaking i disagree",
            entities=None,
            reply_to_bot=False,
            bot_username="ThinkMateBot",
            bot_name="ThinkMate",
        )
        is False
    )


# ===========================================================================
# B) Routing tests for handle_user_message / _handle_group_message.
# ===========================================================================

_BOT_IDENTITY = {"id": 999, "username": "ThinkMateBot", "name": "ThinkMate"}


def _make_group_message(
    text: str,
    *,
    chat_type: str = "supergroup",
    chat_id: int = -100,
    user_id: int = 111,
    full_name: str = "Alice",
    reply_to_message=None,
    entities=None,
) -> MagicMock:
    """Build a mocked aiogram Message for a group/supergroup/channel update."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.from_user.full_name = full_name
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.chat.type = chat_type
    message.text = text
    message.entities = entities
    message.reply_to_message = reply_to_message
    message.answer = AsyncMock()
    message.bot = MagicMock()
    # Real-message defaults so the non-conversational guard (forward/channel) is inert.
    message.sender_chat = None
    message.forward_origin = None
    message.forward_date = None
    message.is_automatic_forward = False
    return message


def _patch_deps():
    """Return a list of patchers for the handler's hot-path dependencies.

    Patches the enqueue path, the explicit buffer write, the affinity cache
    (``bump``/``get``), the ambient-chime handoff, and the cached bot identity so
    no real ``get_me()`` call happens.
    """
    return [
        patch.object(
            messages_module.user_task_manager,
            "enqueue_message",
            new_callable=AsyncMock,
        ),
        patch.object(messages_module.models, "add_message_to_buffer", new_callable=AsyncMock),
        patch.object(messages_module.affinity_cache, "bump", new_callable=AsyncMock),
        patch.object(messages_module.affinity_cache, "get", new_callable=AsyncMock),
        patch.object(messages_module, "_maybe_ambient_chime", new_callable=AsyncMock),
        patch.object(
            messages_module,
            "_get_bot_identity",
            new_callable=AsyncMock,
            return_value=dict(_BOT_IDENTITY),
        ),
    ]


@pytest.mark.asyncio
async def test_addressed_group_message_enqueues_reply_no_buffer_no_chime():
    """An addressed group message enqueues a reply once and writes no extra buffer.

    Single-write invariant: the enqueue → handle_message path appends the user
    message, so the handler does NOT call add_message_to_buffer for addressed
    messages, and the ambient gate is not consulted (Req 2.2).
    """
    db = MagicMock()
    message = _make_group_message("hey @ThinkMateBot hi")

    (p_enqueue, p_buffer, p_bump, p_get, p_chime, p_identity) = _patch_deps()
    with p_enqueue as mock_enqueue, p_buffer as mock_buffer, p_bump as mock_bump, \
            p_get as mock_get, p_chime as mock_chime, p_identity:
        await messages_module.handle_user_message(message, db)

        assert mock_enqueue.call_count == 1, "addressed message should enqueue exactly once"
        _, kwargs = mock_enqueue.call_args
        # chat_id is passed positionally as the 2nd arg; user_id/reason are kwargs.
        args, _ = mock_enqueue.call_args
        assert args[1] == -100, "enqueue chat_id must be the group chat id"
        assert kwargs["reason"] == "reply"
        assert kwargs["user_id"] == 111

        # Single-write invariant: no explicit buffer write in the handler.
        assert not mock_buffer.called, "addressed path must not double-write the buffer"
        # Addressed → no ambient handoff.
        assert not mock_chime.called, "addressed message must not run the ambient gate"


@pytest.mark.asyncio
async def test_non_addressed_group_message_hands_off_to_gate_no_reply():
    """A non-addressed group message is handed to the ambient gate, not replied to.

    Single-write invariant (post-fix): the handler no longer writes the buffer
    itself — the non-addressed buffer write moved INTO ``_maybe_ambient_chime``
    (drop path only). So the handler's contract for a non-addressed message is:
    it does NOT enqueue a direct reply (reason="reply"), and it DOES hand off to
    ``_maybe_ambient_chime``. Because the chime helper is patched here, no buffer
    write is expected from the handler at all (Req 2.5).
    """
    db = MagicMock()
    message = _make_group_message("just chatting with friends")

    (p_enqueue, p_buffer, p_bump, p_get, p_chime, p_identity) = _patch_deps()
    with p_enqueue as mock_enqueue, p_buffer as mock_buffer, p_bump as mock_bump, \
            p_get as mock_get, p_chime as mock_chime, p_identity:
        await messages_module.handle_user_message(message, db)

        # Not enqueued directly as a reply.
        reply_calls = [
            c for c in mock_enqueue.call_args_list
            if c.kwargs.get("reason") == "reply"
        ]
        assert not reply_calls, "non-addressed message must not enqueue a direct reply"

        # The handler itself no longer writes the buffer for non-addressed messages:
        # that write moved into _maybe_ambient_chime (the drop-path sole writer),
        # which is patched here. So the handler must not call add_message_to_buffer.
        assert not mock_buffer.called, (
            "handler must not write the buffer directly; the write moved into "
            "_maybe_ambient_chime"
        )

        # Handed off to the ambient gate (the new single owner of the buffer write
        # on the non-addressed path).
        assert mock_chime.called, "non-addressed message must hand off to the ambient gate"
        chime_args, _ = mock_chime.call_args
        assert chime_args[0] is message, "chime must receive the original message"
        assert chime_args[1] is db, "chime must receive the db handle"


@pytest.mark.asyncio
async def test_channel_message_is_ignored_entirely():
    """A channel update is ignored: no buffer write, no enqueue, no answer (Req 2.6)."""
    db = MagicMock()
    message = _make_group_message("anything in a channel", chat_type="channel")

    (p_enqueue, p_buffer, p_bump, p_get, p_chime, p_identity) = _patch_deps()
    with p_enqueue as mock_enqueue, p_buffer as mock_buffer, p_bump as mock_bump, \
            p_get as mock_get, p_chime as mock_chime, p_identity:
        await messages_module.handle_user_message(message, db)

        assert not mock_enqueue.called, "channel update must not enqueue"
        assert not mock_buffer.called, "channel update must not write the buffer"
        assert not mock_chime.called, "channel update must not run the ambient gate"
        assert not message.answer.called, "channel update must not answer"


@pytest.mark.parametrize(
    "attr,value",
    [
        ("is_automatic_forward", True),   # linked-channel post auto-copied into the group
        ("forward_origin", object()),      # user manually forwarded something in
        ("forward_date", 1234567890),      # legacy forwarded marker
        ("sender_chat", object()),         # sent on behalf of a channel/group, not a person
    ],
)
@pytest.mark.asyncio
async def test_forwarded_or_channel_authored_message_is_ignored(attr, value):
    """Forwarded / auto-forwarded / channel-authored messages are not user turns.

    Regression for the bot replying to a linked-channel post that appeared in the
    discussion group. Each of these markers must short-circuit the handler: no enqueue,
    no buffer write, no ambient gate, no answer.
    """
    db = MagicMock()
    message = _make_group_message("ThinkMate is live (Beta) ...")
    setattr(message, attr, value)

    (p_enqueue, p_buffer, p_bump, p_get, p_chime, p_identity) = _patch_deps()
    with p_enqueue as mock_enqueue, p_buffer as mock_buffer, p_bump as mock_bump, \
            p_get as mock_get, p_chime as mock_chime, p_identity:
        await messages_module.handle_user_message(message, db)

        assert not mock_enqueue.called, f"{attr} message must not enqueue"
        assert not mock_buffer.called, f"{attr} message must not write the buffer"
        assert not mock_chime.called, f"{attr} message must not run the ambient gate"
        assert not message.answer.called, f"{attr} message must not answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/foo", "/start"])
async def test_bot_command_in_group_returns_early(text):
    """A bot command in a group returns early: no enqueue, no buffer (Req 2.8).

    Commands are handled by the commands router, not the catch-all conversation
    handler, so the command guard must fire before any group routing.
    """
    db = MagicMock()
    message = _make_group_message(text)

    (p_enqueue, p_buffer, p_bump, p_get, p_chime, p_identity) = _patch_deps()
    with p_enqueue as mock_enqueue, p_buffer as mock_buffer, p_bump as mock_bump, \
            p_get as mock_get, p_chime as mock_chime, p_identity:
        await messages_module.handle_user_message(message, db)

        assert not mock_enqueue.called, f"command {text!r} must not enqueue"
        assert not mock_buffer.called, f"command {text!r} must not write the buffer"
        assert not mock_chime.called, f"command {text!r} must not run the ambient gate"
