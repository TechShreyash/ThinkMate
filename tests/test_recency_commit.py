"""Tests for the implicit-address recency commit point and the idle-sweep prune hook.

Covers Task 8 of the implicit-bot-addressing spec:

- A group reply that is actually sent records that the bot spoke
  (``implicit_gate.note_bot_spoke``) — Requirement 6.1.
- A suppressed ambient empty-reply sends nothing and therefore does NOT record
  that the bot spoke — Requirement 6.1.
- The idle sweep (``_evict_idle``) prunes both the implicit-address gate and the
  spam-burst detector — Requirements 6.4, 10.13.

Mirrors the existing pure/deterministic test style (mocked ``handle_message``,
config knobs overridden with set/restore in ``try/finally``).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import config
from app.services.user_task_manager import UserTaskManager


def _make_group_message(chat_id: int) -> MagicMock:
    """Build a mock aiogram Message for a group chat."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "group"
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    msg.react = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_sent_group_reply_calls_note_bot_spoke():
    """A genuinely-sent group reply records that the bot spoke (Req 6.1)."""
    chat_id = 920001
    manager = UserTaskManager()
    mock_bot = MagicMock()
    message = _make_group_message(chat_id)

    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    config.MESSAGE_BATCH_DELAY_SECS = 0.1
    try:
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle, \
             patch("app.services.user_task_manager.implicit_gate.note_bot_spoke") as mock_note:
            mock_handle.return_value = ("Hey there!", None)

            await manager.enqueue_message(
                mock_bot,
                chat_id,
                "is anyone around",
                message,
                chat_type="group",
                reason="reply",
            )
            await asyncio.sleep(0.25)

            # The reply was actually sent as a Telegram reply (threaded under the
            # triggering message in groups)...
            message.reply.assert_called_once_with("Hey there!")
            # ...so the bot's recent activity is recorded for the chat.
            mock_note.assert_called_once()
            assert mock_note.call_args[0][0] == chat_id
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay


@pytest.mark.asyncio
async def test_suppressed_ambient_empty_reply_does_not_call_note_bot_spoke():
    """A suppressed ambient empty-reply sends nothing and records nothing (Req 6.1)."""
    chat_id = 920002
    manager = UserTaskManager()
    mock_bot = MagicMock()
    message = _make_group_message(chat_id)

    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    config.MESSAGE_BATCH_DELAY_SECS = 0.1
    try:
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle, \
             patch("app.services.user_task_manager.implicit_gate.note_bot_spoke") as mock_note:
            # Ambient chime-in declined: empty reply text => nothing is sent.
            mock_handle.return_value = ("", None)

            await manager.enqueue_message(
                mock_bot,
                chat_id,
                "good morning everyone",
                message,
                chat_type="group",
                reason="ambient",
            )
            await asyncio.sleep(0.25)

            # Nothing was sent...
            message.answer.assert_not_called()
            message.reply.assert_not_called()
            # ...so the bot did not "speak" and note_bot_spoke is never called.
            mock_note.assert_not_called()
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay


@pytest.mark.asyncio
async def test_evict_idle_prunes_implicit_gate_and_spam_burst_detector():
    """The idle sweep prunes the implicit gate and the spam-burst detector (Req 6.4, 10.13)."""
    manager = UserTaskManager()

    with patch("app.services.user_task_manager.ambient_gate.prune", return_value=0), \
         patch("app.services.user_task_manager.affinity_cache.prune", return_value=0), \
         patch("app.services.user_task_manager.implicit_gate.prune", return_value=0) as mock_implicit_prune, \
         patch("app.services.user_task_manager.spam_burst_detector.prune", return_value=0) as mock_burst_prune:
        await manager._evict_idle()

        mock_implicit_prune.assert_called_once()
        mock_burst_prune.assert_called_once()
