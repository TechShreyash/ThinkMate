"""Application entrypoint: verify MongoDB, init indexes, register middlewares/routers,
and start long-polling the Telegram bot.
"""
import asyncio
import contextlib
from aiogram import Bot, Dispatcher
from loguru import logger
from app.config import config
from app.handlers import main_router
from app.handlers.middlewares import DbSessionMiddleware, ThrottlingMiddleware
from app.database.connection import init_db, ping_db
from app.services import log_forwarder
from app.services.error_log_sink import make_error_log_sink
from app.services.health import (
    start_metrics_logger,
    start_consolidation_scheduler,
    start_proactive_scheduler,
)


async def main():
    logger.info("Verifying MongoDB connection...")
    try:
        await ping_db()
    except Exception as e:
        logger.error(f"Cannot reach MongoDB at startup: {e}")
        raise

    logger.info("Initializing MongoDB indexes...")
    await init_db()

    # Optional periodic metrics logger (Phase 10). No-op unless
    # config.METRICS_LOG_INTERVAL_SECS > 0; runs under this asyncio loop.
    background_tasks: list[asyncio.Task] = []

    metrics_task = start_metrics_logger()
    if metrics_task is not None:
        background_tasks.append(metrics_task)
        logger.info(
            f"Periodic metrics logger started (every {config.METRICS_LOG_INTERVAL_SECS}s)."
        )

    # Optional periodic consolidation scheduler (Phase 11). No-op unless
    # config.CONSOLIDATION_INTERVAL_SECS > 0; runs under this asyncio loop.
    consolidation_task = start_consolidation_scheduler()
    if consolidation_task is not None:
        background_tasks.append(consolidation_task)
        logger.info(
            f"Consolidation scheduler started (scan every {config.CONSOLIDATION_SCAN_INTERVAL_SECS}s, "
            f"per-user every {config.CONSOLIDATION_INTERVAL_SECS}s)."
        )

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    # Give the background extractor (and any caller without a Message) a
    # process-wide bot reference for best-effort Logs_Channel forwarding.
    log_forwarder.set_bot(bot)

    # Register the Error_Log_Sink alongside the existing console + logs/bot.log
    # sinks (without replacing them) so bot-wide WARNING+ records are forwarded
    # to the Logs_Channel. Capture the running loop so the synchronous sink can
    # hop back onto it from any thread. The filter drops records marked
    # `no_forward` (e.g. the Log_Forwarder's own logs) to avoid forward loops.
    loop = asyncio.get_running_loop()
    logger.add(
        make_error_log_sink(bot, loop),
        level="WARNING",
        filter=lambda r: not r["extra"].get("no_forward"),
        enqueue=False,
    )

    # Optional proactive check-in scheduler (Phase 12). Needs the aiogram bot to
    # send, so it starts after `bot` is created. No-op unless
    # config.PROACTIVE_INTERVAL_SECS > 0; runs under this asyncio loop.
    proactive_task = start_proactive_scheduler(bot)
    if proactive_task is not None:
        background_tasks.append(proactive_task)
        logger.info(
            f"Proactive check-in scheduler started (scan every {config.PROACTIVE_INTERVAL_SECS}s)."
        )

    dp = Dispatcher()

    # Throttle spammers before any DB session is opened, then inject the DB session.
    dp.update.outer_middleware(ThrottlingMiddleware())
    dp.update.outer_middleware(DbSessionMiddleware())

    dp.include_router(main_router)

    logger.info("Polling Telegram Bot...")
    try:
        # drop_pending_updates: on (re)start, discard any backlog Telegram queued
        # while the bot was down, so a redeploy never replays a flood of stale
        # messages. allowed_updates is narrowed to the update types our routers
        # actually handle, trimming needless getUpdates payload.
        await dp.start_polling(
            bot,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        # Graceful shutdown: stop background schedulers, then close the bot's HTTP
        # session. aiogram handles SIGINT/SIGTERM (handle_signals=True by default),
        # so this runs on Ctrl+C and on container stop/redeploy alike.
        logger.info("Shutting down: cancelling background tasks...")
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await bot.session.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("ThinkMate bot stopped.")
