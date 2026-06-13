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


async def main():
    logger.info("Verifying MongoDB connection...")
    try:
        await ping_db()
    except Exception as e:
        logger.error(f"Cannot reach MongoDB at startup: {e}")
        raise

    logger.info("Initializing MongoDB indexes...")
    await init_db()

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
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
