# Telegram Bot Integration Guide (aiogram 3.x)

This document details the Telegram interaction layer of ThinkMate, built on the asynchronous
framework **aiogram 3.x** with dependency injection and per-user batching.

---

## 🛠️ Modern aiogram 3.x Architecture

`aiogram 3.x` is built on `asyncio`. We keep business logic out of handlers and rely on:

1.  **Outer Middlewares** for system-wide concerns — rate limiting and database-session injection.
2.  **Context Dependency Injection** — the active MongoDB database (`db`) is passed to handlers via the middleware `data` payload.
3.  **A dedicated task manager** (`UserTaskManager`) that batches messages, serializes per-user work, and drives the "typing…" indicator across both the batching delay and generation.

> **Typing indicators are *not* a middleware.** An earlier design used an `AutoTypingMiddleware`
> keyed on a `long_operation` flag, but it never fired (no handler set the flag) and typing is
> better handled by `UserTaskManager`, whose typing loop spans the whole batch+generation window.
> The middleware was removed.

---

## 🧱 Middlewares (`app/handlers/middlewares.py`)

Two outer middlewares are registered on the update pipeline:

- **`ThrottlingMiddleware`** — a per-user sliding-window rate limiter, applied *before* any DB
  session is opened so floods are dropped cheaply. Its in-memory map is pruned periodically so
  it can't grow unbounded across many users.
- **`DbSessionMiddleware`** — yields the shared async MongoDB database and injects it as `db`.

```python
# app/handlers/middlewares.py
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from app.database.connection import db_session


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with db_session() as db:
            data["db"] = db
            return await handler(event, data)
```

The `motor` client is a process-wide singleton with its own connection pool, so the "session"
here is simply the shared database handle — there are no per-request connections to leak.

---

## 🔀 Registering Middlewares and Routers (`main.py`)

```python
# main.py
import asyncio
from aiogram import Bot, Dispatcher
from loguru import logger
from app.config import config
from app.handlers import main_router
from app.handlers.middlewares import DbSessionMiddleware, ThrottlingMiddleware
from app.database.connection import init_db, ping_db


async def main():
    logger.info("Verifying MongoDB connection...")
    await ping_db()                      # fail fast if the database is unreachable
    logger.info("Initializing MongoDB indexes...")
    await init_db()                      # compound + audit TTL indexes

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    dp.update.outer_middleware(ThrottlingMiddleware())  # throttle before DB work
    dp.update.outer_middleware(DbSessionMiddleware())   # then inject the db session
    dp.include_router(main_router)

    logger.info("Polling Telegram Bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
```

---

## ⚙️ Dependency-Injected Handlers

Handlers receive the MongoDB database (`AsyncIOMotorDatabase`) directly.

### 1. Commands (`app/handlers/commands.py`)

`/start`, `/help`, `/profile`, and `/reset` are implemented. `/reset` requires explicit
confirmation (`/reset confirm`) before wiping a user's stored state.

```python
from aiogram import Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import models

router = Router(name="commands")


@router.message(Command("start"))
async def cmd_start(message: Message, db: AsyncIOMotorDatabase):
    user = message.from_user
    if not user:
        return
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
    await message.answer(f"Hi {html.bold(user.first_name or 'there')}! 👋 ...", parse_mode="HTML")


@router.message(Command("reset"))
async def cmd_reset(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    if (command.args or "").strip().lower() != "confirm":
        await message.answer("⚠️ This erases everything I remember. To confirm, send: /reset confirm")
        return
    await models.reset_user(db, message.from_user.id)
    await message.answer("Done — I've cleared everything. 🌱")
```

### 2. Conversational Router (`app/handlers/messages.py`)

Text messages are guarded for length and enqueued for batching. Service/channel posts with
no real sender are ignored.

```python
from aiogram import Router, F
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.services.user_task_manager import user_task_manager

router = Router(name="messages")


@router.message(F.text)
async def handle_user_message(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    user_text = message.text or ""
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer("that's a lot of text 😅 keep it short ...")
        return
    await user_task_manager.enqueue_message(message.bot, message.from_user.id, user_text, message)
```

### 3. Dynamic Message Reactions — one call

The reply and the optional emoji reaction come from a **single** LLM call: `handle_message`
returns `(reply_text, reaction)`. The batch processor applies the reaction (already normalized
to Telegram's accepted set) and sends the reply. Reaction failures (e.g. a chat that disallows
reactions) are caught and never block delivery.

```python
# app/services/user_task_manager.py (batch processing snippet)
async with db_session() as db:
    reply_text, reaction = await handle_message(db, user_id, combined_text)

if reaction:
    try:
        await last_message.react(reaction=[ReactionTypeEmoji(emoji=reaction)])
    except Exception as react_err:
        logger.warning(f"Failed to send reaction {reaction!r}: {react_err}")

await last_message.answer(reply_text)
```

---

## 👥 Group Chat Routing *(Phase 9)*

In groups/supergroups the message router does **chat-type + identity routing** before enqueuing:

- **Addressed** (bot @mentioned, bot's name used, or a reply to the bot's message) → always
  reply, exactly like a DM.
- **Not addressed** → run the **ambient gate** (per-chat cooldown → cheap keyword scan →
  affinity-weighted probability) and only enqueue a chime-in for the few messages that survive.
  Every group message is still buffered cheaply for context and learning.
- **Channels** → ignored.

Two extra commands manage chattiness per group: `/quiet` (mode → quiet, suppress ambient) and
`/chatty` (mode → chatty, boost). Affinity and mode live in `chat_members` (see
[database.md](database.md)). DMs are unchanged (`chat_id == user_id`). Full design and the
no-LLM ambient funnel are in [group_chat.md](group_chat.md).

---

## 💡 Key Architectural Guidelines

1.  **No business logic in handlers** — handlers validate, then call services/models with the injected `db`.
2.  **No direct database clients in handlers** — the `motor` client/session is owned by `connection.py` and injected by `DbSessionMiddleware`.
3.  **Typing is owned by `UserTaskManager`**, not a middleware — it spans the batching delay and generation, and stops when the queue drains.
4.  **Throttle before DB work** — `ThrottlingMiddleware` runs on the outer update layer so floods never reach the database or the LLM.
