import asyncio
from aiogram import Bot, Dispatcher
from loguru import logger
from app.config import config
from app.handlers import main_router
from app.handlers.middlewares import DbSessionMiddleware, AutoTypingMiddleware
from app.database.connection import init_db

async def main():
    logger.info("Initializing SQLite tables...")
    await init_db()

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    # Register Global Middlewares
    # DbSessionMiddleware must be registered on the outer update layer
    dp.update.outer_middleware(DbSessionMiddleware())
    # AutoTypingMiddleware is registered as an inner middleware on messages
    dp.message.middleware(AutoTypingMiddleware())

    # Register main router containing all sub-routers
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
