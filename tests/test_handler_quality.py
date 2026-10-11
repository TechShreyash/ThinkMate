"""Handler regressions for truthful feedback, resource bounds, and group controls."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message

from app.config import config
from app.database import models
from app.handlers import commands, membership, messages
from app.handlers.middlewares import ThrottlingMiddleware
from app.services.group_gate import AmbientGate


def _message(chat_type="private"):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(
        id=5353, is_bot=False, username="tester", first_name="Tester"
    )
    message.chat = SimpleNamespace(id=5353, type=chat_type)
    message.date = None
    message.answer = AsyncMock()
    return message


@pytest.mark.parametrize(
    "profile",
    [
        {"communication_style": "Prefers short answers"},
        {"gender": "female"},
        {"insights": [{"content": "Prefers a steady routine"}]},
        {"emotional_state": {"mood": "excited", "detected_at": "2026-01-01"}},
        {"mood_history": [{"mood": "excited"}]},
    ],
)
async def test_profile_shows_all_stored_memory_components(profile):
    collection = SimpleNamespace(find_one=AsyncMock(return_value=profile))
    db = {"user_profiles": collection}
    message = _message()
    await commands.cmd_profile(message, db)
    collection.find_one.assert_awaited_once_with({"_id": message.from_user.id})
    text = message.answer.await_args.args[0]
    assert "Here is what I remember" in text
    assert "don't have any memories" not in text


@pytest.mark.parametrize("delivery", [True, False, RuntimeError("offline")])
async def test_reset_only_claims_backup_when_it_was_delivered(mock_mongodb, delivery):
    message = _message()
    await models.ensure_user(mock_mongodb, message.from_user.id, "tester", "Tester")
    sender = (
        AsyncMock(side_effect=delivery)
        if isinstance(delivery, Exception)
        else AsyncMock(return_value=delivery)
    )
    with patch.object(commands.log_forwarder, "send_document", sender):
        await commands.cmd_reset(message, SimpleNamespace(args="confirm"), mock_mongodb)
    assert await mock_mongodb["user_profiles"].find_one({"_id": 5353}) is None
    text = message.answer.await_args.args[0]
    assert ("backup saved" in text) is (delivery is True)


async def test_confirmed_reset_stops_private_work_before_backup_and_deletion(
    mock_mongodb,
):
    message = _message()
    await models.ensure_user(mock_mongodb, 5353, "tester", "Tester")
    operations = []
    original_export = models.export_user_data
    original_reset = models.reset_user

    @asynccontextmanager
    async def reset_private_work(user_id):
        assert user_id == 5353
        operations.append("enter")
        try:
            yield
        finally:
            operations.append("exit")

    async def export(database, user_id):
        operations.append("export")
        return await original_export(database, user_id)

    async def erase(database, user_id):
        operations.append("erase")
        return await original_reset(database, user_id)

    async def acknowledge(*_args, **_kwargs):
        operations.append("acknowledge")

    message.answer = AsyncMock(side_effect=acknowledge)

    with (
        patch.object(
            commands.user_task_manager, "reset_conversation", new=reset_private_work
        ),
        patch.object(commands.models, "export_user_data", new=export),
        patch.object(commands.models, "reset_user", new=erase),
        patch.object(
            commands.log_forwarder, "send_document", AsyncMock(return_value=False)
        ),
    ):
        await commands.cmd_reset(message, SimpleNamespace(args="confirm"), mock_mongodb)
    assert operations == ["enter", "export", "erase", "exit", "acknowledge"]
    assert await mock_mongodb["user_profiles"].find_one({"_id": 5353}) is None


async def test_reset_does_not_delete_data_if_private_work_cannot_stop(mock_mongodb):
    message = _message()
    await models.ensure_user(mock_mongodb, 5353, "tester", "Tester")
    reset_context = MagicMock()
    reset_context.__aenter__ = AsyncMock(side_effect=TimeoutError("work still running"))
    with (
        patch.object(
            commands.user_task_manager, "reset_conversation", return_value=reset_context
        ),
        patch.object(commands.models, "export_user_data", AsyncMock()) as export,
        pytest.raises(TimeoutError, match="work still running"),
    ):
        await commands.cmd_reset(message, SimpleNamespace(args="confirm"), mock_mongodb)
    assert await mock_mongodb["user_profiles"].find_one({"_id": 5353}) is not None
    export.assert_not_awaited()
    message.answer.assert_not_awaited()


async def test_onboarding_callback_escapes_plain_text_for_html(mock_mongodb):
    message = _message()
    message.edit_text = AsyncMock()
    callback = SimpleNamespace(
        data="gd:onboard",
        from_user=message.from_user,
        message=message,
        answer=AsyncMock(),
    )
    with patch.object(config, "BOT_NAME", "<Nova & friends>"):
        await commands.on_guide_nav(callback, mock_mongodb)
    assert "&lt;Nova &amp; friends&gt;" in message.edit_text.await_args.args[0]
    assert message.edit_text.await_args.kwargs["parse_mode"] == "HTML"
    callback.answer.assert_awaited_once()


async def test_guide_acknowledges_before_onboarding_database_work(mock_mongodb):
    message = _message()
    message.edit_text = AsyncMock()
    callback = SimpleNamespace(
        data="gd:onboard",
        from_user=message.from_user,
        message=message,
        answer=AsyncMock(),
    )

    async def unavailable_database(*_args):
        callback.answer.assert_awaited_once()
        raise RuntimeError("database offline")

    with (
        patch.object(commands.models, "ensure_user", new=unavailable_database),
        pytest.raises(RuntimeError, match="database offline"),
    ):
        await commands.on_guide_nav(callback, mock_mongodb)
    message.edit_text.assert_not_awaited()


async def test_expired_callback_ack_does_not_prevent_guide_navigation(mock_mongodb):
    message = _message()
    message.edit_text = AsyncMock()
    callback = SimpleNamespace(
        data="gd:memory",
        from_user=message.from_user,
        message=message,
        answer=AsyncMock(side_effect=RuntimeError("query is too old")),
    )
    await commands.on_guide_nav(callback, mock_mongodb)
    message.edit_text.assert_awaited_once()
    assert "How my memory works" in message.edit_text.await_args.args[0]


@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("checkins", "nudges"),
        ("reactions", "emoji"),
        ("reset", "forget"),
        ("groupbot", "enabled"),
        ("groupmode", "talkative"),
    ],
)
def test_renamed_command_examples_match_help_and_telegram_menu(key, name):
    command_config = {**config.COMMANDS, key: (name, True)}
    with patch.object(config, "COMMANDS", command_config):
        help_text = commands._build_help_text(is_admin=False)
        menu_entry = commands._menu_for((key,))[0]
    assert f"/{key} " not in help_text
    assert f"/{name} " in help_text
    assert menu_entry.command == name
    assert f"/{name} " in menu_entry.description
    assert f"/{key} " not in menu_entry.description


async def test_disabled_group_ignores_media_replies(mock_mongodb):
    message = _message("supergroup")
    message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=999))
    await models.set_group_enabled(mock_mongodb, message.chat.id, False)
    with patch.object(messages, "_get_bot_identity", AsyncMock()) as identity:
        await messages.handle_media_message(message, mock_mongodb)
    message.answer.assert_not_awaited()
    identity.assert_not_awaited()


async def test_enabled_group_deflects_media_replies_to_bot(mock_mongodb):
    message = _message("supergroup")
    message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=999))
    with patch.object(
        messages, "_get_bot_identity", AsyncMock(return_value={"id": 999})
    ):
        await messages.handle_media_message(message, mock_mongodb)
    message.answer.assert_awaited_once()


async def test_slow_routing_diagnostic_cannot_allow_two_ambient_chimes():
    message = _message("supergroup")
    diagnostic_started = asyncio.Event()
    finish_diagnostic = asyncio.Event()

    async def trace(_message, decision, _detail=""):
        if decision == "ambient→chime":
            diagnostic_started.set()
            await finish_diagnostic.wait()

    with (
        patch.object(messages, "ambient_gate", AmbientGate()),
        patch.object(config, "GROUP_AMBIENT_BASE_RATE", 1.0),
        patch.object(config, "GROUP_AMBIENT_COOLDOWN_SECS", 30.0),
        patch.object(messages.time, "time", return_value=1000.0),
        patch.object(messages, "_trace_routing", new=trace),
        patch.object(
            messages.affinity_cache,
            "get",
            AsyncMock(return_value={"affinity": 1.0, "mode": "auto"}),
        ),
        patch.object(messages.models, "get_group_mode", AsyncMock(return_value="auto")),
        patch.object(messages.models, "add_message_to_buffer", AsyncMock()) as buffer,
        patch.object(
            messages.user_task_manager, "enqueue_message", AsyncMock()
        ) as enqueue,
    ):
        first = asyncio.create_task(
            messages._maybe_ambient_chime(message, {}, "hello", "Tester")
        )
        try:
            await asyncio.wait_for(diagnostic_started.wait(), timeout=1)
            await asyncio.wait_for(
                messages._maybe_ambient_chime(message, {}, "hello", "Tester"),
                timeout=1,
            )
            buffer.assert_awaited_once()
            enqueue.assert_not_awaited()
        finally:
            finish_diagnostic.set()
            await first
        enqueue.assert_awaited_once()


async def test_group_intro_diagnostic_reports_send_failure():
    bot = SimpleNamespace(
        get_me=AsyncMock(
            return_value=SimpleNamespace(username="bot", first_name="Nova")
        ),
        send_message=AsyncMock(side_effect=RuntimeError("Forbidden")),
    )
    event = SimpleNamespace(
        bot=bot, chat=SimpleNamespace(id=-123, type="group", title="Test")
    )
    with patch.object(
        membership.log_forwarder, "diagnostic", AsyncMock()
    ) as diagnostic:
        await membership.on_added_to_group(event)
    assert "intro not sent" in diagnostic.await_args.args[2]


async def test_throttle_history_stays_bounded_during_sustained_spam():
    message = _message()
    handler = AsyncMock()
    with (
        patch.object(config, "RATE_LIMIT_MAX_REQUESTS", 3),
        patch("app.handlers.middlewares.time.time", return_value=100.0),
    ):
        middleware = ThrottlingMiddleware()
        for _ in range(1000):
            await middleware(handler, message, {})
        assert len(middleware.users[5353]) == 3
    assert handler.await_count == 3
    message.answer.assert_awaited_once()


async def test_throttle_warning_cannot_overwrite_a_later_update():
    warning_started = asyncio.Event()
    finish_warning = asyncio.Event()

    async def blocked_warning(*_args, **_kwargs):
        warning_started.set()
        await finish_warning.wait()

    message = _message()
    message.answer = AsyncMock(side_effect=blocked_warning)
    handler = AsyncMock()
    with (
        patch.object(config, "RATE_LIMIT_MAX_REQUESTS", 1),
        patch(
            "app.handlers.middlewares.time.time",
            side_effect=[100.0, 100.0, 101.0, 102.0],
        ),
    ):
        middleware = ThrottlingMiddleware()
        await middleware(handler, message, {})
        warning_task = asyncio.create_task(middleware(handler, message, {}))
        await warning_started.wait()
        await middleware(handler, message, {})
        finish_warning.set()
        await warning_task
    assert middleware.users[5353] == [102.0]
    handler.assert_awaited_once()
    message.answer.assert_awaited_once()


async def test_throttle_retains_latest_send_times_for_out_of_order_updates():
    message = _message()
    handler = AsyncMock()
    with (
        patch.object(config, "RATE_LIMIT_MAX_REQUESTS", 2),
        patch.object(config, "RATE_LIMIT_WINDOW_SECS", 5.0),
        patch.object(config, "STALE_MESSAGE_SECS", 0),
        patch("app.handlers.middlewares.time.time", return_value=110.0),
    ):
        middleware = ThrottlingMiddleware()
        for timestamp in [100.0, 101.0, 90.0, 102.0]:
            message.date = datetime.fromtimestamp(timestamp, UTC)
            await middleware(handler, message, {})
    assert handler.await_count == 2
    assert middleware.users[5353] == [101.0, 102.0]
