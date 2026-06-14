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
confirmation (`/reset confirm`) before wiping a user's stored state. Two group-only commands —
`/quiet` and `/chatty` — set the speaker's ambient `mode` via `affinity_cache.set_mode`; in a DM
they reply with a graceful no-op explanation (see [Group Chat Routing](#-group-chat-routing-phase-9-implemented)).

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

## 👥 Group Chat Routing *(Phase 9, implemented)*

In groups/supergroups the message router (`app/handlers/messages.py`) does **chat-type +
identity routing** before enqueuing. `handle_user_message` branches on `message.chat.type`:

- **`private`** → the exact DM path that existed before group support (length-guard, then a
  positional `enqueue_message`), so DM behavior is byte-for-byte unchanged.
- **`channel`** → ignored entirely (no buffer write, no reply).
- **`group` / `supergroup`** → `_handle_group_message`, the multi-party path below.

**Addressed detection.** The bot's identity (`id`, `username`, `name`) is resolved once via
`bot.get_me()` and cached process-wide (`_get_bot_identity`) to avoid an API round-trip per
message; a failed lookup degrades to "not addressed" rather than raising. `is_addressed`
(in [`group_gate.py`](../../app/services/group_gate.py)) returns True when the message
@mentions the bot's username, uses the bot's name as a standalone token, or replies to one of
the bot's own messages.

- **Addressed** → bump the speaker's affinity (`+0.05`) and `enqueue_message(..., reason="reply")`
  — always reply, exactly like a DM.
- **Not addressed** → record the message to the buffer (with `sender_id`/`sender_name`), then run
  the **ambient gate** (`_maybe_ambient_chime`): read the speaker's affinity/mode, run the cheap
  `scan_cheap_triggers`, and call `ambient_gate.decide(...)`. Only candidates that survive the
  cooldown → trigger/scan-tick → affinity-weighted dice roll are enqueued as a chime-in
  (`reason="ambient"`); `mark_chimed` is called before enqueue so a failed/empty reply still
  holds the cooldown window.

**Single-write invariant.** Each group message is buffered exactly once: addressed messages are
written by the `enqueue_message → handle_message` path (like DMs), and non-addressed messages are
written by the handler itself before the ambient gate (since they never reach `handle_message`).

**Empty-ambient suppression.** An ambient chime-in may decline (empty reply); `UserTaskManager._process_batch`
sends nothing in that case (skips both the reaction and the answer).

Two extra commands manage chattiness per group: `/quiet` (mode → quiet, suppress ambient) and
`/chatty` (mode → chatty, boost), both set via `affinity_cache.set_mode`. Affinity and mode live
in `chat_members` (see [database.md](database.md)). DMs are unchanged (`chat_id == user_id`). Full
design and the no-LLM ambient funnel are in [group_chat.md](group_chat.md).

---

## 💞 Engagement commands *(Phase 12, implemented)*

Phase 12 adds three small, DM-oriented commands plus light enhancements to `/start` and `/help`.
They follow the exact same patterns as the existing commands — aiogram `Command`, the injected
`db`, and `models` CRUD — and none of them makes an LLM call.

### `/onboard` — a static, no-LLM introduction

`/onboard` sends a single, persona-consistent welcome message (plain conversational text, no
markdown or bullets) that introduces ThinkMate and asks three light starter questions to get the
memory profile seeded faster. It is **static** — there is no LLM call. Under the hood it calls
`models.ensure_user` to seed the profile and `models.set_onboarded(db, user.id, True)` to flip the
`onboarded` flag. It does **not** gate normal chat: the user's eventual answers are captured by the
ordinary extraction pipeline like any other message.

### `/pause` and `/resume` — proactive opt-out / opt-in

These toggle whether the user receives [proactive check-ins](configuration.md#-proactive-check-ins-phase-12):

- **`/pause`** → `models.set_proactive_enabled(db, user_id, False)` — "I won't reach out on my own
  anymore." The user is excluded from future proactive scans (the due-user query filters on
  `proactive_enabled != False`).
- **`/resume`** → `models.set_proactive_enabled(db, user_id, True)` — re-enables the occasional
  nudge.

They are DM-oriented; in a group they harmlessly toggle the caller's own flag, consistent with how
`/quiet` and `/chatty` behave.

### Enhanced `/start` and `/help`

- **`/start`** still upserts the profile, but now checks the `onboarded` flag and **nudges
  `/onboard` only when the user has not onboarded yet**. Already-onboarded users get the normal
  pointer to `/profile` and `/help` instead.
- **`/help`** now lists the new `/onboard`, `/pause`, and `/resume` commands alongside the existing
  ones.

```python
@router.message(Command("onboard"))
async def cmd_onboard(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    user = message.from_user
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
    await models.set_onboarded(db, user.id, True)
    await message.answer("hey, glad you're here. i'm ThinkMate ...")  # static, conversational


@router.message(Command("pause"))
async def cmd_pause(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    await models.set_proactive_enabled(db, message.from_user.id, False)
    await message.answer("Got it — I won't reach out on my own anymore. ...")
```

> The background scheduler that actually sends the check-ins (and how "due" users are selected,
> rate-limited, and quiet-hours-gated) is covered in
> [memory_engine.md](memory_engine.md#-phase-12--temporal-context--emotional-continuity-implemented),
> [configuration.md](configuration.md#-proactive-check-ins-phase-12), and
> [observability.md](observability.md#proactive-check-in-metrics-phase-12).

---

## 💡 Key Architectural Guidelines

1.  **No business logic in handlers** — handlers validate, then call services/models with the injected `db`.
2.  **No direct database clients in handlers** — the `motor` client/session is owned by `connection.py` and injected by `DbSessionMiddleware`.
3.  **Typing is owned by `UserTaskManager`**, not a middleware — it spans the batching delay and generation, and stops when the queue drains.
4.  **Throttle before DB work** — `ThrottlingMiddleware` runs on the outer update layer so floods never reach the database or the LLM.
