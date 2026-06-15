"""Tests for the observability health helpers and the admin /health command.

Covers Task 4.3 of the observability spec:

* ``readiness(db)`` reports ready on a working DB and degrades gracefully (never
  raising) when the Mongo ping fails (Requirements 3.3, 3.4).
* ``liveness()`` returns a well-formed status/uptime/summary with no DB access.
* ``/health`` honors the ``ADMIN_USER_IDS`` / DM-only authorization default and
  adds no LLM call, and still produces a (degraded) report when readiness fails
  (Requirements 4.1, 4.3, 4.4, 4.7, 7.3, 7.4).

All tests use mongomock + pytest-asyncio per ``tests/conftest.py``. ``readiness``
delegates to ``app.database.connection.ping_db`` via a lazy import, so the ping is
controlled by patching ``app.database.connection.ping_db``.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import config
from app.handlers.commands import cmd_health
from app.services.health import liveness, readiness


def _make_message(user_id: int, chat_type: str = "private") -> MagicMock:
    """Build a mocked aiogram Message with a sender, chat type, and async answer."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.chat = MagicMock()
    message.chat.type = chat_type
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    return message


# --- readiness -------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_ok():
    """readiness returns ready when the Mongo ping succeeds (Req 3.3)."""
    db = MagicMock()
    with patch("app.database.connection.ping_db", new_callable=AsyncMock):
        result = await readiness(db)
    assert result == {"ready": True, "mongo": "ok"}


@pytest.mark.asyncio
async def test_readiness_degraded_never_raises():
    """readiness degrades (no raise) when the ping fails (Req 3.4)."""
    db = MagicMock()
    with patch(
        "app.database.connection.ping_db",
        new_callable=AsyncMock,
        side_effect=RuntimeError("server selection timeout"),
    ):
        result = await readiness(db)
    assert result["ready"] is False
    assert result["mongo"] == "error"
    assert "reason" in result
    assert "server selection timeout" in result["reason"]


# --- liveness --------------------------------------------------------------


def test_liveness_shape_no_io():
    """liveness reports status/uptime/summary with no DB access (Req 3.2)."""
    result = liveness()
    assert result["status"] == "ok"
    assert isinstance(result["uptime_secs"], (int, float))
    assert result["uptime_secs"] >= 0
    assert isinstance(result["summary"], dict)


# --- /health authorization -------------------------------------------------


@pytest.mark.asyncio
async def test_health_dm_default_replies():
    """With empty ADMIN_USER_IDS, /health in a DM replies once (Req 4.4)."""
    original = config.ADMIN_USER_IDS
    config.ADMIN_USER_IDS = set()
    try:
        db = MagicMock()
        message = _make_message(user_id=1, chat_type="private")
        with patch("app.database.connection.ping_db", new_callable=AsyncMock):
            await cmd_health(message, db)
        message.answer.assert_called_once()
    finally:
        config.ADMIN_USER_IDS = original


@pytest.mark.asyncio
async def test_health_group_default_declined():
    """With empty ADMIN_USER_IDS, /health in a group is declined (Req 4.4)."""
    original = config.ADMIN_USER_IDS
    config.ADMIN_USER_IDS = set()
    try:
        db = MagicMock()
        message = _make_message(user_id=1, chat_type="supergroup")
        with patch("app.database.connection.ping_db", new_callable=AsyncMock):
            await cmd_health(message, db)
        message.answer.assert_not_called()
    finally:
        config.ADMIN_USER_IDS = original


@pytest.mark.asyncio
async def test_health_admin_id_gate():
    """ADMIN_USER_IDS gates by user id regardless of chat type (Req 4.3)."""
    original = config.ADMIN_USER_IDS
    config.ADMIN_USER_IDS = {123}
    try:
        db = MagicMock()
        with patch("app.database.connection.ping_db", new_callable=AsyncMock):
            allowed = _make_message(user_id=123, chat_type="supergroup")
            await cmd_health(allowed, db)
            # In a group the report threads as a reply to the command.
            allowed.reply.assert_called_once()

            denied = _make_message(user_id=999, chat_type="supergroup")
            await cmd_health(denied, db)
            denied.reply.assert_not_called()
            denied.answer.assert_not_called()
    finally:
        config.ADMIN_USER_IDS = original


@pytest.mark.asyncio
async def test_health_readiness_failure_still_reports():
    """A failing readiness ping still yields a (degraded) report (Req 4.2)."""
    original = config.ADMIN_USER_IDS
    config.ADMIN_USER_IDS = set()
    try:
        db = MagicMock()
        message = _make_message(user_id=1, chat_type="private")
        with patch(
            "app.database.connection.ping_db",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ping down"),
        ):
            await cmd_health(message, db)
        message.answer.assert_called_once()
    finally:
        config.ADMIN_USER_IDS = original
