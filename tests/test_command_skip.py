"""Bug condition exploration test for the DM "skip bot commands" bugfix.

Property 1 (Bug Condition): Bot commands must not be treated as conversation.

The catch-all text handler ``handle_user_message`` (registered with
``@router.message(F.text)``) currently has no guard for command-like text, so an
unregistered slash command (e.g. ``/foo``) that the command router does not consume
falls through and is enqueued to the memory/LLM pipeline as if it were conversation.

This test asserts the DESIRED (fixed) behavior: for a message whose text is a bot
command, ``handle_user_message`` should ignore it — neither answering nor enqueueing.

On the UNFIXED code this test is EXPECTED TO FAIL (the catch-all DOES enqueue the
command), which confirms the bug exists.

**Validates: Requirements 1.1, 1.2, 1.3**
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.handlers.messages as messages_module


def _make_command_strings():
    """Generate a scoped set of command-like strings (Hypothesis is not a dep)."""
    words = ["foo", "bar", "baz", "doit", "xyzzy", "settings", "qux", "test"]
    cmds = ["/foo", "/foo@ThinkMateBot"]
    for w in words:
        cmds.append("/" + w)
        cmds.append("/" + w + "@ThinkMateBot")
    return cmds


def _make_command_message(text: str) -> MagicMock:
    """Build a mocked aiogram Message with a real sender and command text."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = 4242
    message.text = text
    message.bot = MagicMock()
    message.answer = AsyncMock()
    message.sender_chat = None
    message.forward_origin = None
    message.forward_date = None
    message.is_automatic_forward = False
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("text", _make_command_strings())
async def test_bot_command_is_not_treated_as_conversation(text):
    """A bot command reaching the catch-all must be ignored: no enqueue, no answer."""
    db = MagicMock()
    message = _make_command_message(text)

    with patch.object(
        messages_module.user_task_manager, "enqueue_message", new_callable=AsyncMock
    ) as mock_enqueue:
        await messages_module.handle_user_message(message, db)

        assert not mock_enqueue.called, (
            f"bug: handle_user_message({text!r}) enqueued the command "
            f"instead of returning early"
        )
        assert not message.answer.called, (
            f"bug: handle_user_message({text!r}) answered the command "
            f"instead of returning early"
        )
