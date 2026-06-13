"""Preservation property tests for the DM "skip bot commands" bugfix.

Property 2 (Preservation): Non-command handling must remain unchanged.

These tests capture the baseline behavior of the catch-all text handler
``handle_user_message`` (registered with ``@router.message(F.text)``) for inputs
where the bug condition does NOT hold:

1. Conversational (non-command) text within ``MAX_INPUT_CHARS`` is enqueued to the
   memory/LLM pipeline (and not answered directly).
2. Non-command text longer than ``MAX_INPUT_CHARS`` triggers the length-guard
   ``message.answer(...)`` response and is NOT enqueued.
3. A message with no sender (``message.from_user is None``) returns early — neither
   answers nor enqueues.

Following observation-first methodology, these tests describe behavior observed on
the UNFIXED code; they are EXPECTED TO PASS on unfixed code (establishing the
baseline to preserve) and must continue to pass after the fix.

**Validates: Requirements 3.1, 3.3, 3.4**
"""
import string

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.handlers.messages as messages_module
from app.config import config

# The exact length-guard response text used by handle_user_message.
LENGTH_GUARD_TEXT = (
    "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
)


def _make_conversational_strings():
    """Generate a scoped set of non-command strings (Hypothesis is not a dep).

    None of these start with "/", and all are within MAX_INPUT_CHARS so they exercise
    the conversational (enqueue) path. Includes the tricky "2/3" case from the design,
    which contains a slash but does not start with one.
    """
    base = [
        "hello, how are you?",
        "what's the weather like today",
        "2/3 of the way there",
        "tell me a story about dragons",
        "i had a great day at work",
        "can you help me think through a problem",
        "math: 10/2 = 5 and 9/3 = 3",
        "    leading spaces then words",
        "emoji time 😀 🎉 let's chat",
        "a",
    ]
    # A few generated word-strings for breadth.
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    for i, w in enumerate(words):
        base.append(f"{w} message number {i} " + string.ascii_lowercase)
    return base


def _make_message(text, with_sender=True):
    """Build a mocked aiogram Message consistent with tests/test_reactions.py."""
    message = MagicMock()
    if with_sender:
        message.from_user = MagicMock()
        message.from_user.id = 4242
    else:
        message.from_user = None
    message.text = text
    message.bot = MagicMock()
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("text", _make_conversational_strings())
async def test_conversational_text_is_enqueued_and_not_answered(text):
    """Non-command text within the length limit must be enqueued, not answered."""
    # Guard: keep the generated inputs within the conversational length window.
    assert len(text) <= config.MAX_INPUT_CHARS
    db = MagicMock()
    message = _make_message(text)

    with patch.object(
        messages_module.user_task_manager, "enqueue_message", new_callable=AsyncMock
    ) as mock_enqueue:
        await messages_module.handle_user_message(message, db)

        mock_enqueue.assert_called_once_with(
            message.bot, message.from_user.id, text, message
        )
        assert not message.answer.called, (
            f"preservation: handle_user_message({text!r}) should not answer "
            f"conversational text directly"
        )


@pytest.mark.asyncio
async def test_overlong_non_command_text_triggers_length_guard():
    """Non-command text longer than MAX_INPUT_CHARS hits the length guard, no enqueue."""
    long_text = "a" * (config.MAX_INPUT_CHARS + 1)
    assert not long_text.startswith("/")
    db = MagicMock()
    message = _make_message(long_text)

    with patch.object(
        messages_module.user_task_manager, "enqueue_message", new_callable=AsyncMock
    ) as mock_enqueue:
        await messages_module.handle_user_message(message, db)

        message.answer.assert_called_once_with(LENGTH_GUARD_TEXT)
        assert not mock_enqueue.called, (
            "preservation: over-long text must not be enqueued"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("over_by", [1, 100, 1000])
async def test_length_guard_preserved_across_lengths(over_by):
    """Property-style: any non-command text over the limit is guarded, not enqueued."""
    long_text = "x" * (config.MAX_INPUT_CHARS + over_by)
    db = MagicMock()
    message = _make_message(long_text)

    with patch.object(
        messages_module.user_task_manager, "enqueue_message", new_callable=AsyncMock
    ) as mock_enqueue:
        await messages_module.handle_user_message(message, db)

        message.answer.assert_called_once_with(LENGTH_GUARD_TEXT)
        assert not mock_enqueue.called


@pytest.mark.asyncio
async def test_empty_sender_returns_early():
    """A message with no sender must be ignored: neither answers nor enqueues."""
    db = MagicMock()
    message = _make_message("hello there", with_sender=False)

    with patch.object(
        messages_module.user_task_manager, "enqueue_message", new_callable=AsyncMock
    ) as mock_enqueue:
        await messages_module.handle_user_message(message, db)

        assert not mock_enqueue.called, (
            "preservation: message with no sender must not be enqueued"
        )
        assert not message.answer.called, (
            "preservation: message with no sender must not be answered"
        )
