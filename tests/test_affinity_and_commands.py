"""Tests for the affinity store (``AffinityCache``) and the ``/quiet`` ``/chatty`` commands.

Uses mongomock + pytest-asyncio per ``tests/conftest.py`` conventions. Each AffinityCache
test uses a FRESH ``AffinityCache()`` instance to avoid cross-test cache state leaking
through the module-level singleton.

Patch targets (see module imports):
- ``affinity.py`` does ``from app.database import models`` -> patch
  ``app.services.affinity.models.get_chat_member``.
- ``commands.py`` does ``from app.services.affinity import affinity_cache`` -> patch
  ``app.handlers.commands.affinity_cache.set_mode``.

**Validates: Requirements 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 6.1, 6.2, 6.3**
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import config
from app.database import connection, models
from app.services.affinity import AffinityCache
import app.handlers.commands as commands_module
from app.handlers.commands import cmd_quiet, cmd_chatty


# --------------------------------------------------------------------------- #
# AffinityCache                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_read_through_defaults_on_miss():
    """First get on a cold member returns defaults and creates the chat_members record.

    **Validates: Requirements 4.2**
    """
    async with connection.db_session() as db:
        cache = AffinityCache()
        chat_id, user_id = -100, 111

        result = await cache.get(db, chat_id, user_id)

        assert result == {"affinity": config.AFFINITY_DEFAULT, "mode": "auto"}

        # Record was created and persisted via miss-create.
        doc = await models.get_chat_member(db, chat_id, user_id)
        assert doc is not None
        assert doc["affinity"] == config.AFFINITY_DEFAULT
        assert doc["mode"] == "auto"


@pytest.mark.asyncio
async def test_get_serves_second_read_from_cache_without_db_hit():
    """After a warm get, a second get is served from cache and does NOT hit the DB.

    **Validates: Requirements 4.2**
    """
    async with connection.db_session() as db:
        cache = AffinityCache()
        chat_id, user_id = -100, 222

        # Warm the cache (this read/creates the record).
        first = await cache.get(db, chat_id, user_id)
        assert first == {"affinity": config.AFFINITY_DEFAULT, "mode": "auto"}

        # Now spy on the model read fn; a cache hit must not call it.
        with patch(
            "app.services.affinity.models.get_chat_member",
            new_callable=AsyncMock,
        ) as spy_get:
            second = await cache.get(db, chat_id, user_id)

        assert second == first
        assert not spy_get.called, "second get should be served from cache, not the DB"


@pytest.mark.asyncio
async def test_bump_clamps_to_range_and_writes_through():
    """bump caps at 1.0 / floors at 0.0 and persists the clamped value.

    **Validates: Requirements 4.4, 4.5, 4.6, 4.7**
    """
    async with connection.db_session() as db:
        chat_id, user_id = -100, 333

        # Bump well beyond 1.0 -> caps at 1.0.
        cache_high = AffinityCache()
        capped = await cache_high.bump(db, chat_id, user_id, delta=5.0)
        assert capped == 1.0
        doc = await models.get_chat_member(db, chat_id, user_id)
        assert doc["affinity"] == 1.0

        # Bump well below 0.0 -> floors at 0.0 (fresh cache, reads current 1.0 from DB).
        cache_low = AffinityCache()
        floored = await cache_low.bump(db, chat_id, user_id, delta=-10.0)
        assert floored == 0.0
        doc = await models.get_chat_member(db, chat_id, user_id)
        assert doc["affinity"] == 0.0


@pytest.mark.asyncio
async def test_set_mode_writes_through_and_updates_cache():
    """set_mode persists the mode and updates the cached value.

    **Validates: Requirements 4.4, 6.1, 6.2**
    """
    async with connection.db_session() as db:
        cache = AffinityCache()
        chat_id, user_id = -100, 444

        await cache.set_mode(db, chat_id, user_id, "quiet")
        doc = await models.get_chat_member(db, chat_id, user_id)
        assert doc["mode"] == "quiet"
        assert (await cache.get(db, chat_id, user_id))["mode"] == "quiet"

        await cache.set_mode(db, chat_id, user_id, "chatty")
        doc = await models.get_chat_member(db, chat_id, user_id)
        assert doc["mode"] == "chatty"
        assert (await cache.get(db, chat_id, user_id))["mode"] == "chatty"


@pytest.mark.asyncio
async def test_prune_evicts_idle_entries():
    """prune drops entries idle longer than max_idle and returns the count pruned.

    **Validates: Requirements 4.2**
    """
    async with connection.db_session() as db:
        cache = AffinityCache()
        chat_id, user_id = -100, 555

        # Warm one entry.
        await cache.get(db, chat_id, user_id)
        assert len(cache._cache) == 1

        # Prune with a far-future "now" and a tiny max_idle so the entry is stale.
        pruned = cache.prune(now=10**12, max_idle=1.0)
        assert pruned >= 1
        assert (chat_id, user_id) not in cache._cache

        # A subsequent get re-reads from the DB (optional sanity check).
        result = await cache.get(db, chat_id, user_id)
        assert result["mode"] == "auto"


# --------------------------------------------------------------------------- #
# /quiet and /chatty commands                                                 #
# --------------------------------------------------------------------------- #


def _make_group_message(chat_type: str, chat_id: int, user_id: int) -> MagicMock:
    """Build a mocked aiogram Message for a group/DM with a real sender."""
    message = MagicMock()
    message.chat = MagicMock()
    message.chat.type = chat_type
    message.chat.id = chat_id
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_cmd_quiet_in_group_sets_mode_and_acknowledges():
    """/quiet in a group sets mode 'quiet' for the speaker and sends an ack.

    **Validates: Requirements 6.1**
    """
    db = MagicMock()
    message = _make_group_message("supergroup", chat_id=-100, user_id=222)

    with patch.object(
        commands_module.affinity_cache, "set_mode", new_callable=AsyncMock
    ) as mock_set_mode:
        await cmd_quiet(message, db)

    mock_set_mode.assert_awaited_once_with(db, -100, 222, "quiet")
    assert message.answer.called


@pytest.mark.asyncio
async def test_cmd_chatty_in_group_sets_mode_and_acknowledges():
    """/chatty in a group sets mode 'chatty' for the speaker and sends an ack.

    **Validates: Requirements 6.2**
    """
    db = MagicMock()
    message = _make_group_message("supergroup", chat_id=-100, user_id=222)

    with patch.object(
        commands_module.affinity_cache, "set_mode", new_callable=AsyncMock
    ) as mock_set_mode:
        await cmd_chatty(message, db)

    mock_set_mode.assert_awaited_once_with(db, -100, 222, "chatty")
    assert message.answer.called


@pytest.mark.asyncio
async def test_cmd_quiet_in_dm_does_not_set_mode():
    """/quiet in a DM creates no group state and sends a graceful explanation.

    **Validates: Requirements 6.3, 4.8**
    """
    db = MagicMock()
    message = _make_group_message("private", chat_id=222, user_id=222)

    with patch.object(
        commands_module.affinity_cache, "set_mode", new_callable=AsyncMock
    ) as mock_set_mode:
        await cmd_quiet(message, db)

    assert not mock_set_mode.called, "DM must not create group affinity state"
    assert message.answer.called


@pytest.mark.asyncio
async def test_cmd_chatty_in_dm_does_not_set_mode():
    """/chatty in a DM creates no group state and sends a graceful explanation.

    **Validates: Requirements 6.3, 4.8**
    """
    db = MagicMock()
    message = _make_group_message("private", chat_id=222, user_id=222)

    with patch.object(
        commands_module.affinity_cache, "set_mode", new_callable=AsyncMock
    ) as mock_set_mode:
        await cmd_chatty(message, db)

    assert not mock_set_mode.called, "DM must not create group affinity state"
    assert message.answer.called
