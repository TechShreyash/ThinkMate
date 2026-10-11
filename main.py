"""Initialize services, poll Telegram, and release resources on every exit path."""

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from loguru import logger

from app.config import config
from app.database.connection import close_db, init_db, ping_db
from app.handlers import main_router
from app.handlers.commands import _fmt_uptime, setup_bot_commands
from app.handlers.middlewares import (
    DbSessionMiddleware,
    ThrottlingMiddleware,
    UpdateTaskMiddleware,
)
from app.services import log_forwarder
from app.services.chat_manager import validate_persona_file
from app.services.error_log_sink import make_error_log_sink
from app.services.health import (
    flush_metrics,
    load_persisted_metrics,
    start_consolidation_scheduler,
    start_metrics_logger,
    start_metrics_persister,
    start_proactive_scheduler,
)
from app.services.llm_service import llm_service
from app.services.user_task_manager import user_task_manager


async def _cleanup_step(
    name: str, operation: Awaitable[object], *, timeout: float = 15.0
) -> None:
    """Keep one failed or stalled cleanup from skipping the remaining resources."""
    try:
        async with asyncio.timeout(timeout):
            await operation
    except Exception:  # noqa: BLE001 - cleanup must continue after any failure
        logger.exception(f"Shutdown step failed: {name}")


async def _stop_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Background task failed: {result}")


async def main() -> None:
    background_tasks: list[asyncio.Task] = []
    bot = None
    sink = None
    sink_id = None
    started_at = None
    who = "ThinkMate"
    updates = UpdateTaskMiddleware()
    try:
        logger.info("Verifying MongoDB connection...")
        await ping_db()
        await init_db()
        await load_persisted_metrics()

        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        log_forwarder.set_bot(bot)
        sink = make_error_log_sink(bot, asyncio.get_running_loop())
        sink_id = logger.add(
            sink,
            level="WARNING",
            filter=lambda record: not record["extra"].get("no_forward"),
            enqueue=False,
        )
        validate_persona_file()

        for start_task in (
            start_metrics_logger,
            start_metrics_persister,
            start_consolidation_scheduler,
        ):
            task = start_task()
            if task is not None:
                background_tasks.append(task)
        proactive_task = start_proactive_scheduler(bot)
        if proactive_task is not None:
            background_tasks.append(proactive_task)

        dp = Dispatcher()
        dp.update.outer_middleware(updates)
        dp.update.outer_middleware(ThrottlingMiddleware())
        dp.update.outer_middleware(DbSessionMiddleware())
        dp.include_router(main_router)
        await setup_bot_commands(bot)

        # Arbitrary start_polling keywords are workflow data, so drop the backlog
        # through Telegram's webhook API before starting long polling.
        await bot.delete_webhook(drop_pending_updates=True)
        try:
            me = await bot.get_me()
            if me.username:
                who = f"@{me.username}"
        except Exception:  # noqa: BLE001 - identity is only cosmetic
            logger.debug("Bot identity lookup failed; using the default display name.")
        started_at = datetime.now(UTC)
        await log_forwarder.send(
            bot,
            None,
            f"🚀 {who} started — polling Telegram.\n"
            f"🕐 {started_at:%Y-%m-%d %H:%M:%S} UTC",
        )
        logger.info("Polling Telegram Bot...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            # Keep Telegram available while replies and log uploads drain below.
            close_bot_session=False,
        )
    finally:
        logger.info("Shutting down background services...")
        await _cleanup_step("schedulers", _stop_tasks(background_tasks))
        await _cleanup_step("update handlers", updates.close())
        await _cleanup_step("conversations", user_task_manager.shutdown())
        await _cleanup_step("LLM and audit writes", llm_service.close())
        await _cleanup_step("metrics", flush_metrics())
        if bot is not None and started_at is not None:
            uptime = (datetime.now(UTC) - started_at).total_seconds()
            await _cleanup_step(
                "shutdown announcement",
                log_forwarder.send(
                    bot,
                    None,
                    f"🛑 {who} shutting down.\n⏱ uptime {_fmt_uptime(uptime)}",
                ),
            )
        if sink_id is not None:
            logger.remove(sink_id)
        if sink is not None:
            await _cleanup_step("error log forwarding", sink.close())
        await _cleanup_step("buffered logs", log_forwarder.close())
        if bot is not None:
            await _cleanup_step("Telegram session", bot.session.close())
        close_db()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt, SystemExit:
        logger.info("ThinkMate bot stopped.")
