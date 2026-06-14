"""Tests for the Phase 12 engagement slash-command handlers (Feature C: commands).

Covers ``cmd_onboard`` (static plain-text intro + persisted ``onboarded`` flag, no LLM),
the ``/start`` ``/onboard``-nudge logic gated on the onboarded flag, the ``/pause`` and
``/resume`` proactive toggles, and the ``/help`` listing.

mongomock + pytest-asyncio per ``tests/conftest.py``: the autouse ``mock_mongodb``
fixture provides the mongomock-backed db, which is passed directly to the command
handlers (they take ``db`` as a parameter). No real LLM or network.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import models
from app.handlers.commands import (
    cmd_help,
    cmd_onboard,
    cmd_pause,
    cmd_resume,
    cmd_start,
)


def _make_command_message(
    user_id: int = 5151,
    username: str = "tester",
    first_name: str = "Tester",
) -> MagicMock:
    """Build a mocked aiogram Message with a real sender and an awaitable ``answer``."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.from_user.username = username
    message.from_user.first_name = first_name
    message.chat = MagicMock()
    message.chat.type = "private"
    message.answer = AsyncMock()
    return message


def _answered_text(message: MagicMock) -> str:
    """Return the positional text passed to the (single) ``message.answer`` call."""
    args, _ = message.answer.call_args
    return args[0]


def _is_plain_text(text: str) -> bool:
    """True when ``text`` carries no markdown emphasis/bullets/headers."""
    if any(ch in text for ch in ("*", "_", "#")):
        return False
    for line in text.splitlines():
        if line.lstrip().startswith("- "):
            return False
    return True


# --- 1. /onboard ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboard_sets_flag_answers_plain_text_and_no_llm(mock_mongodb):
    db = mock_mongodb
    message = _make_command_message(user_id=5101)

    with patch(
        "app.services.llm_service.llm_service.generate_reply_bundle",
        new_callable=AsyncMock,
    ) as mock_llm:
        await cmd_onboard(message, db)

    # Flag persisted on the profile document.
    doc = await db["user_profiles"].find_one({"_id": 5101})
    assert doc is not None
    assert doc["onboarded"] is True

    # Exactly one answer, and it is plain text (no markdown bullets/emphasis/headers).
    message.answer.assert_called_once()
    assert _is_plain_text(_answered_text(message))

    # No LLM call is made by onboarding.
    mock_llm.assert_not_called()


# --- 2. /start /onboard-nudge logic -----------------------------------------------------

@pytest.mark.asyncio
async def test_start_nudges_onboard_for_fresh_user(mock_mongodb):
    db = mock_mongodb
    message = _make_command_message(user_id=5102)

    await cmd_start(message, db)

    message.answer.assert_called_once()
    assert "/onboard" in _answered_text(message)


@pytest.mark.asyncio
async def test_start_does_not_nudge_onboard_once_onboarded(mock_mongodb):
    db = mock_mongodb
    message = _make_command_message(user_id=5103)

    # Establish the profile, then mark it onboarded.
    await models.ensure_user(db, 5103, "tester", "Tester")
    await models.set_onboarded(db, 5103, True)

    await cmd_start(message, db)

    message.answer.assert_called_once()
    assert "/onboard" not in _answered_text(message)


# --- 3. /pause then /resume -------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_then_resume_toggles_proactive_enabled(mock_mongodb):
    db = mock_mongodb
    message = _make_command_message(user_id=5104)
    await models.ensure_user(db, 5104, "tester", "Tester")

    await cmd_pause(message, db)
    doc = await db["user_profiles"].find_one({"_id": 5104})
    assert doc["proactive_enabled"] is False

    await cmd_resume(message, db)
    doc = await db["user_profiles"].find_one({"_id": 5104})
    assert doc["proactive_enabled"] is True


# --- 4. /help ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_help_lists_engagement_commands():
    message = _make_command_message()

    await cmd_help(message)

    message.answer.assert_called_once()
    text = _answered_text(message)
    assert "/onboard" in text
    assert "/pause" in text
    assert "/resume" in text
