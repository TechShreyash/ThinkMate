"""Resource cleanup on normal shutdown and partial startup failure."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main as entrypoint
from app.handlers.middlewares import UpdateTaskMiddleware
from app.services.error_log_sink import make_error_log_sink
from app.services.llm_service import LLMService


@pytest.fixture
def resources(monkeypatch):
    events = []

    def operation(name):
        async def run(*args, **kwargs):
            events.append(name)

        return AsyncMock(side_effect=run)

    bot = SimpleNamespace(
        session=SimpleNamespace(close=operation("telegram close")),
        delete_webhook=operation("delete webhook"),
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
    )
    dispatcher = SimpleNamespace(
        update=SimpleNamespace(outer_middleware=Mock()),
        include_router=Mock(),
        resolve_used_update_types=Mock(return_value=["message"]),
        start_polling=operation("polling"),
    )
    sink = SimpleNamespace(close=operation("sink close"))
    monkeypatch.setattr(entrypoint, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(entrypoint, "Dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(entrypoint, "make_error_log_sink", Mock(return_value=sink))
    monkeypatch.setattr(entrypoint.logger, "add", Mock(return_value=99))
    monkeypatch.setattr(entrypoint.logger, "remove", Mock())
    for name in ("ping_db", "init_db", "load_persisted_metrics", "setup_bot_commands"):
        monkeypatch.setattr(entrypoint, name, operation(name))
    monkeypatch.setattr(entrypoint, "validate_persona_file", Mock())
    for name in (
        "start_metrics_logger",
        "start_metrics_persister",
        "start_consolidation_scheduler",
        "start_proactive_scheduler",
    ):
        monkeypatch.setattr(entrypoint, name, Mock(return_value=None))
    monkeypatch.setattr(
        entrypoint,
        "user_task_manager",
        SimpleNamespace(shutdown=operation("conversations")),
    )
    monkeypatch.setattr(
        entrypoint, "llm_service", SimpleNamespace(close=operation("llm close"))
    )
    monkeypatch.setattr(entrypoint, "flush_metrics", operation("metrics"))
    monkeypatch.setattr(entrypoint.log_forwarder, "set_bot", Mock())
    monkeypatch.setattr(entrypoint.log_forwarder, "send", operation("log send"))
    monkeypatch.setattr(entrypoint.log_forwarder, "close", operation("logs close"))
    monkeypatch.setattr(
        entrypoint, "close_db", Mock(side_effect=lambda: events.append("db close"))
    )
    return SimpleNamespace(events=events, bot=bot, dispatcher=dispatcher, sink=sink)


async def test_shutdown_keeps_telegram_open_until_work_and_logs_drain(resources):
    await entrypoint.main()
    resources.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
    resources.dispatcher.start_polling.assert_awaited_once_with(
        resources.bot, allowed_updates=["message"], close_bot_session=False
    )
    events = resources.events
    assert events.index("delete webhook") < events.index("polling")
    assert events.index("conversations") < events.index("llm close")
    assert events.index("sink close") < events.index("logs close")
    assert (
        events.index("logs close")
        < events.index("telegram close")
        < events.index("db close")
    )


async def test_startup_failure_still_releases_clients(resources):
    entrypoint.ping_db.side_effect = RuntimeError("unreachable database")
    with pytest.raises(RuntimeError, match="unreachable database"):
        await entrypoint.main()
    entrypoint.Bot.assert_not_called()
    assert "llm close" in resources.events
    assert "db close" in resources.events


async def test_partial_scheduler_startup_failure_stops_started_tasks(
    resources, monkeypatch
):
    task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(entrypoint, "start_metrics_logger", Mock(return_value=task))
    entrypoint.start_metrics_persister.side_effect = RuntimeError(
        "scheduler setup failed"
    )
    with pytest.raises(RuntimeError, match="scheduler setup failed"):
        await entrypoint.main()
    assert task.cancelled()
    assert "llm close" in resources.events
    assert "telegram close" in resources.events
    assert "db close" in resources.events


async def test_cleanup_failure_does_not_skip_remaining_resources(resources):
    resources.sink.close.side_effect = RuntimeError("failed log drain")
    await entrypoint.main()
    assert "logs close" in resources.events
    assert "telegram close" in resources.events
    assert "db close" in resources.events


async def test_shutdown_drains_inflight_update_handler_before_clients(resources):
    started = asyncio.Event()

    async def command(event, data):
        started.set()
        await asyncio.sleep(0)
        resources.events.append("command finished")

    async def polling(*args, **kwargs):
        updates = resources.dispatcher.update.outer_middleware.call_args_list[0].args[0]
        asyncio.create_task(updates(command, None, {}))
        await started.wait()

    resources.dispatcher.start_polling.side_effect = polling
    await entrypoint.main()
    assert resources.events.index("command finished") < resources.events.index(
        "conversations"
    )
    assert resources.events.index("command finished") < resources.events.index(
        "telegram close"
    )


async def test_update_middleware_close_cancels_stalled_handler_and_stops_admission():
    middleware = UpdateTaskMiddleware()
    started = asyncio.Event()

    async def command(event, data):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(middleware(command, None, {}))
    await asyncio.wait_for(started.wait(), timeout=1)
    await middleware.close(timeout=0)
    assert task.cancelled()
    handler = AsyncMock()
    assert await middleware(handler, None, {}) is None
    handler.assert_not_called()


async def test_llm_close_drains_audit_before_client_close():
    service = LLMService.__new__(LLMService)
    events = []

    async def audit():
        await asyncio.sleep(0)
        events.append("audit")

    async def close_client():
        events.append("client")

    service._bg_tasks = {asyncio.create_task(audit())}
    service.client = SimpleNamespace(close=close_client)
    await service.close()
    assert events == ["audit", "client"]


async def test_llm_close_cancels_stalled_audit():
    service = LLMService.__new__(LLMService)
    task = asyncio.create_task(asyncio.Event().wait())
    service._bg_tasks = {task}
    service.client = SimpleNamespace(close=AsyncMock())
    await service.close(timeout=0)
    assert task.cancelled()
    service.client.close.assert_awaited_once()


async def test_error_sink_close_cancels_stalled_sends(monkeypatch):
    from app.services import log_forwarder

    started = asyncio.Event()

    async def send(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(log_forwarder, "send", send)
    sink = make_error_log_sink(Mock(), asyncio.get_running_loop())
    message = SimpleNamespace(
        record={
            "level": SimpleNamespace(no=40, name="ERROR"),
            "extra": {},
            "message": "Test failure",
            "name": "test",
            "function": "test",
        }
    )
    sink(message)
    await asyncio.wait_for(started.wait(), timeout=1)
    tasks = set(sink._tasks)
    await sink.close(timeout=0)
    assert all(task.cancelled() for task in tasks)
    sink(message)
    await asyncio.sleep(0)
    assert not sink._tasks


async def test_error_sink_close_drains_a_warning_queued_immediately_before_close(
    monkeypatch,
):
    from app.services import log_forwarder

    send = AsyncMock()
    monkeypatch.setattr(log_forwarder, "send", send)
    sink = make_error_log_sink(Mock(), asyncio.get_running_loop())
    sink(
        SimpleNamespace(
            record={
                "level": SimpleNamespace(no=40, name="ERROR"),
                "extra": {},
                "message": "Last warning before shutdown",
                "name": "test",
                "function": "test",
            }
        )
    )
    await sink.close()
    send.assert_awaited_once()
