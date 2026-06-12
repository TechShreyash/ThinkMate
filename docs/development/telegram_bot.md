# Telegram Bot Integration Guide (aiogram 3.x)

This document details the configuration and implementation guidelines for the Telegram interaction layer of ThinkMate, using the asynchronous framework **aiogram 3.x** and modern enterprise best practices (such as dependency injection and automatic ChatAction middleware).

---

## 🛠️ Modern aiogram 3.x Architecture

Unlike synchronous bot frameworks, `aiogram 3.x` is built on top of `asyncio`. To write professional-grade bots, we avoid putting business logic, database setup, or typing actions directly inside handlers. Instead, we use:
1.  **Outer Middlewares**: To handle system-wide tasks like logging, rate limiting, and initializing database connections.
2.  **Context Dependency Injection**: Passing database connections and configurations directly to handlers via the middleware's `data` payload.
3.  **Handler Flags**: Using metadata tags on handlers to dynamically adjust bot behaviors (like starting a typing indicator automatically).

---

## 🧱 Setup & Dependency Injection Middlewares

We will implement two middlewares:
- `DbSessionMiddleware`: Opens a transactional SQLite connection, injects it into the handler, and safely closes it after execution.
- `AutoTypingMiddleware`: Checks if the handler is marked with a `long_operation` flag and automatically displays "typing..." in the chat window.

### 1. Database Connection Injection Middleware

Create a middleware in `app/handlers/middlewares.py` to auto-inject the database session:

```python
# app/handlers/middlewares.py
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from app.database.connection import get_db_connection

class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Open an async connection from the connection pool
        async with await get_db_connection() as db:
            # Inject connection object into handler parameters
            data["db"] = db
            # Execute handler pipeline (including other middlewares)
            result = await handler(event, data)
            # Connection is automatically closed by the context manager
            return result
```

---

### 2. Auto-Typing Middleware via Handler Flags

Typing indicators are essential to indicate processing. Rather than manually writing `ChatActionSender` contexts in every text handler, we use an inner middleware that detects custom flags:

```python
# app/handlers/middlewares.py (continued)
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.dispatcher.flags import get_flag

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
```

---

## 🔀 Registering Middlewares and Routers (`main.py`)

Hook the middlewares into the dispatcher inside your main entry point:

```python
# main.py
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
    asyncio.run(main())
```

---

## ⚙️ Clean, Dependency-Injected Handlers

Since our database connection (`db`) is injected via the middleware, our handlers can access it directly as a parameter.

### 1. Commands Handler (`app/handlers/commands.py`)

```python
# app/handlers/commands.py
from aiogram import Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiosqlite import Connection
from app.services.memory_loader import build_memory_block
from app.database import models

router = Router(name="commands")

@router.message(Command("start"))
async def cmd_start(message: Message, db: Connection):
    user_id = message.from_user.id
    username = message.from_user.username
    display_name = message.from_user.first_name
    
    # ensure_user uses the injected database connection
    await models.ensure_user(db, user_id, username, display_name)
    
    welcome_text = (
        f"Hi {html.bold(display_name)}! 👋\n\n"
        f"I am ThinkMate, an AI companion who remembers our past chats.\n"
        f"Use /profile to view what I remember, or /help to see all commands."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@router.message(Command("profile"))
async def cmd_profile(message: Message, db: Connection):
    user_id = message.from_user.id
    
    # Generate memory card using the active DB session
    profile_data = await build_memory_block(db, user_id)
    
    if not profile_data.strip():
        await message.answer("I don't have any saved memories for you yet. Let's chat more first!")
        return
        
    await message.answer(f"📋 {html.bold('My Memories of You:')}\n\n{html.code(profile_data)}", parse_mode="HTML")
```

### 2. Conversational Message Router (`app/handlers/messages.py`)

Using the `long_operation` flag in routing enables the auto-typing middleware automatically:

```python
# app/handlers/messages.py
from aiogram import Router, F
from aiogram.types import Message
from aiosqlite import Connection
from app.config import config
from app.services.chat_manager import handle_message

router = Router(name="messages")

# Set the flags dictionary containing "long_operation"
@router.message(F.text, flags={"long_operation": True})
async def handle_user_message(message: Message, db: Connection):
    user_id = message.from_user.id
    user_text = message.text

    # --- INPUT LENGTH GUARD ---
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
        )
        return  # Complete ignore (no buffer, no LLM call)

    # Run core orchestration via injected DB session
    reply_text = await handle_message(db, user_id, user_text)
    await message.answer(reply_text)
```

---

## 💡 Key Architectural Guidelines

1.  **Zero Manual Transactions in Handlers**: Handlers should never call `db.commit()` or open database sessions. Handlers call models or services, passing the injected `db` connection.
2.  **No Direct SQLite Connections**: Handlers never run `aiosqlite.connect()`. All connections are managed by the `DbSessionMiddleware` block to prevent file locks or leaks.
3.  **Strict Middleware Filtering**: Only register `AutoTypingMiddleware` on message routing pools. Command routines (like `/start`) respond instantly and do not require typing indicators.
