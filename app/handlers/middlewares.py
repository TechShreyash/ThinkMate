from typing import Callable, Dict, Any, Awaitable
import time
from collections import defaultdict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, Update
from aiogram.utils.chat_action import ChatActionSender
from aiogram.dispatcher.flags import get_flag
from app.config import config
from app.database.connection import db_session

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()
        self.users = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Handle update-level or message-level event
        message = None
        if isinstance(event, Update) and event.message:
            message = event.message
        elif isinstance(event, Message):
            message = event

        if not message or not message.from_user:
            return await handler(event, data)

        user_id = message.from_user.id
        now = time.time()

        # Filter timestamps within the sliding window
        self.users[user_id] = [t for t in self.users[user_id] if now - t < config.RATE_LIMIT_WINDOW_SECS]

        if len(self.users[user_id]) >= config.RATE_LIMIT_MAX_REQUESTS:
            # Warn on initial breach
            if len(self.users[user_id]) == config.RATE_LIMIT_MAX_REQUESTS:
                try:
                    await message.answer("⚠️ *Slow down!* You are sending messages too fast. Please wait a moment.", parse_mode="Markdown")
                except Exception:
                    pass
            # Log timestamp to extend throttle window if they continue spamming
            self.users[user_id].append(now)
            return

        self.users[user_id].append(now)
        return await handler(event, data)

class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Open an async connection using the db_session context manager
        async with db_session() as db:
            # Inject connection object into handler parameters
            data["db"] = db
            # Execute handler pipeline
            result = await handler(event, data)
            return result

class AutoTypingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Ensure the event is a message
        if not isinstance(event, Message):
            return await handler(event, data)

        # Check if the handler was flagged with "long_operation"
        long_op = get_flag(data, "long_operation")
        if long_op:
            bot = data["bot"]
            async with ChatActionSender.typing(bot=bot, chat_id=event.chat.id):
                return await handler(event, data)
        
        return await handler(event, data)
