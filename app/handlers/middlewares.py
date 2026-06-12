from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.dispatcher.flags import get_flag
from app.database.connection import db_session

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
