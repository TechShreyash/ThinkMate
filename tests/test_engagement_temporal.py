"""Tests for the temporal hot path (Phase 12, Feature A).

Covers the pure ``build_time_context`` gap helper and the DM/group wiring in
``chat_manager.handle_message``:

* ``build_time_context`` renders the current UTC time always, and a coarse
  minutes/hours/days "last talked" line only when a previous interaction exists
  (never raw seconds, never a fabricated gap on first contact).
* The DM path records ``last_interaction_at`` via the single combined
  ``touch_and_get_last_interaction`` round-trip and adds no extra LLM call beyond
  the reply.
* The group path does NOT touch ``last_interaction_at``.

mongomock + pytest-asyncio per ``tests/conftest.py`` (autouse ``mock_mongodb``
provides ``db``); the reply LLM is patched with ``AsyncMock`` (chat_manager imports
``from app.services.llm_service import llm_service``, so we patch
``app.services.chat_manager.llm_service.generate_reply_bundle``).

**Validates: Requirements 1.3, 1.4, 1.6, 2.4, 2.5**
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import app.services.chat_manager as chat_manager
from app.database import models


# --- Pure gap helper: build_time_context (Requirements 1.4, 1.6) ---

def test_build_time_context_always_has_current_time_line():
    """The current-time line is always present, regardless of prior interaction."""
    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)
    result = chat_manager.build_time_context(now, None)
    assert "Current time (UTC):" in result
    assert "2024-06-01 14:30" in result


def test_build_time_context_first_contact_has_no_gap_line():
    """With ``prev=None`` only the current-time line is rendered, no 'Last talked'."""
    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)
    result = chat_manager.build_time_context(now, None)
    assert "Last talked" not in result
    # Exactly the single current-time line.
    assert result.count("\n") == 0


def test_build_time_context_days_gap_is_coarse():
    """A ~2-day-old prior interaction renders a coarse 'day(s) ago' line plus the time line."""
    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)
    prev = now - timedelta(days=2, hours=3)
    result = chat_manager.build_time_context(now, prev)
    assert "Current time (UTC):" in result
    assert "Last talked with this user: 2 day(s) ago" in result
    # Coarse units only — never raw seconds.
    assert "second" not in result.lower()


def test_build_time_context_minutes_boundary():
    """A gap under an hour renders coarse minutes (secs < 3600)."""
    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)
    prev = now - timedelta(minutes=30)
    result = chat_manager.build_time_context(now, prev)
    assert "Last talked with this user: 30 minute(s) ago" in result


def test_build_time_context_hours_boundary():
    """A gap between an hour and a day renders coarse hours (3600 <= secs < 86400)."""
    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)
    prev = now - timedelta(hours=5, minutes=15)
    result = chat_manager.build_time_context(now, prev)
    assert "Last talked with this user: 5 hour(s) ago" in result


# --- DM hot path records last_interaction_at via one combined round-trip ---

async def test_dm_path_records_last_interaction_with_single_combined_call(mock_mongodb):
    """A DM ``handle_message`` records ``last_interaction_at`` (absent before) using the
    combined helper exactly once, and makes exactly one reply LLM call (no extra call for
    temporal context). Validates: Requirements 1.3, 2.4, 2.5"""
    db = mock_mongodb
    chat_id = 1001
    await models.ensure_user(db, chat_id, "alice", "Alice")

    # Pre-condition: no last_interaction_at recorded yet.
    before = await db["user_profiles"].find_one({"_id": chat_id})
    assert before is not None
    assert before.get("last_interaction_at") is None

    reply_mock = AsyncMock(return_value=("hi", None))
    # Spy on the combined helper while still performing the real single write.
    touch_spy = AsyncMock(side_effect=models.touch_and_get_last_interaction)

    with patch.object(chat_manager.llm_service, "generate_reply_bundle", reply_mock), \
         patch.object(chat_manager.models, "touch_and_get_last_interaction", touch_spy):
        reply, reaction = await chat_manager.handle_message(db, chat_id, "hello there")

    assert reply == "hi"
    # The combined read-then-set helper was awaited exactly once on the DM path.
    touch_spy.assert_awaited_once()
    # Exactly one reply LLM call — no EXTRA llm call was added for temporal context.
    reply_mock.assert_awaited_once()

    # last_interaction_at is now recorded on the profile.
    after = await db["user_profiles"].find_one({"_id": chat_id})
    assert after.get("last_interaction_at") is not None
    assert isinstance(after["last_interaction_at"], datetime)


# --- Group path must NOT touch last_interaction_at (Requirement 2.5) ---

async def test_group_path_does_not_touch_last_interaction(mock_mongodb):
    """A group-path ``handle_message`` must not record ``last_interaction_at`` and must not
    call the combined helper. Validates: Requirements 2.5"""
    db = mock_mongodb
    chat_id = -5005  # group chat id
    await models.ensure_user(db, chat_id, "groupchat", "Group Chat")

    # with_affinity=True on the group path => 3-tuple reply bundle.
    reply_mock = AsyncMock(return_value=("hi", None, None))
    touch_spy = AsyncMock(side_effect=models.touch_and_get_last_interaction)

    with patch.object(chat_manager.llm_service, "generate_reply_bundle", reply_mock), \
         patch.object(chat_manager.models, "touch_and_get_last_interaction", touch_spy):
        reply, reaction = await chat_manager.handle_message(
            db,
            chat_id,
            "good morning everyone",
            chat_type="group",
            sender_id=7007,
            sender_name="Bob",
        )

    assert reply == "hi"
    # The group path never calls the DM-only combined helper.
    touch_spy.assert_not_awaited()

    # last_interaction_at remains unset by the group path.
    doc = await db["user_profiles"].find_one({"_id": chat_id})
    assert doc.get("last_interaction_at") is None
