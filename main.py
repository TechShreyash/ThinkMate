"""Application entrypoint: verify MongoDB, init indexes, register middlewares/routers,
and start long-polling the Telegram bot.
"""
import asyncio
from aiogram import Bot, Dispatcher
from loguru import logger
from app.config import config
from app.handlers import main_router
from app.handlers.middlewares import DbSessionMiddleware, ThrottlingMiddleware
from app.database.connection import init_db, ping_db
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
    if start_metrics_logger() is not None:
        logger.info(
            f"Periodic metrics logger started (every {config.METRICS_LOG_INTERVAL_SECS}s)."
        )

    # Optional periodic consolidation scheduler (Phase 11). No-op unless
    # config.CONSOLIDATION_INTERVAL_SECS > 0; runs under this asyncio loop.
    if start_consolidation_scheduler() is not None:
        logger.info(
            f"Consolidation scheduler started (scan every {config.CONSOLIDATION_SCAN_INTERVAL_SECS}s, "
            f"per-user every {config.CONSOLIDATION_INTERVAL_SECS}s)."
        )

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    # Optional proactive check-in scheduler (Phase 12). Needs the aiogram bot to
    # send, so it starts after `bot` is created. No-op unless
    # config.PROACTIVE_INTERVAL_SECS > 0; runs under this asyncio loop.
    if start_proactive_scheduler(bot) is not None:
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
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("ThinkMate bot stopped.")
