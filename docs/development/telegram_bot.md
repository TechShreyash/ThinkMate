# Telegram Bot Integration Guide (aiogram 3.x)

This document details the Telegram interaction layer of ThinkMate — the code that receives
messages from Telegram, decides how (and whether) to reply, and sends the answer back. It is
built on the asynchronous framework **aiogram 3.x** with dependency injection and per-user
batching.

Two terms used throughout are worth defining up front. *Dependency injection* means a handler
declares the resources it needs (such as the database) as parameters, and the framework supplies
them at call time instead of the handler reaching for globals. *Batching* means messages sent in
quick succession by the same user are collected and processed together, so a burst of short lines
becomes a single, coherent reply.

**What's in this doc:**

- **Architecture** — why business logic stays out of handlers, and who owns the "typing…" indicator.
- **Middlewares** — the two outer middlewares (rate limiting and database-session injection) and how they're registered.
- **Dependency-injected handlers** — the command handlers, the conversational router, and how a reply and its emoji reaction come from one LLM call.
- **Group chat routing** — how messages in groups are routed by chat type and whether the bot was addressed.
- **Engagement commands** — the Phase 12 DM commands (`/onboard`, `/pause`, `/resume`) and the `/start`/`/help` enhancements.
- **Interactive guide** — the button-driven `/guide` tour and the `callback_query` handler that powers it.
- **Key architectural guidelines** — the rules that keep the layer thin and predictable.

---

## 🛠️ Modern aiogram 3.x Architecture

`aiogram 3.x` is built on `asyncio`. We keep business logic out of handlers and rely on:

1.  **Outer Middlewares** for system-wide concerns — rate limiting and database-session injection.
    A *middleware* is a wrapper that runs around every update before (and after) the handler, so
    cross-cutting work happens in one place rather than being repeated in each handler.
2.  **Context Dependency Injection** — the active MongoDB database (`db`) is passed to handlers via the middleware `data` payload.
3.  **A dedicated task manager** (`UserTaskManager`) that batches messages, serializes per-user work, and drives the "typing…" indicator across both the batching delay and generation.

> **Typing indicators are *not* a middleware.** An earlier design used an `AutoTypingMiddleware`
> keyed on a `long_operation` flag, but it never fired (no handler set the flag) and typing is
> better handled by `UserTaskManager`, whose typing loop spans the whole batch+generation window.
> The middleware was removed.

---

## 🧱 Middlewares (`app/handlers/middlewares.py`)

Two outer middlewares are registered on the update pipeline. They run in order on every incoming
update, before it reaches a handler:

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

Startup wires the pieces together: it verifies the database is reachable, builds the indexes, then
registers the two middlewares (throttling first, so floods never reach the database) and the main
router before polling Telegram.

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

Handlers receive the MongoDB database (`AsyncIOMotorDatabase`) directly, so they never construct a
client of their own — the middleware hands it over as a parameter.

### 1. Commands (`app/handlers/commands.py`)

`/start`, `/help`, `/profile`, and `/reset` are implemented. `/reset` requires explicit
confirmation (`/reset confirm`) before wiping a user's stored state, which guards against an
accidental, irreversible erase. Two group-only commands — `/quiet` and `/chatty` — set the
speaker's ambient `mode` via `affinity_cache.set_mode`; in a DM
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
    # Back up the full profile to the Logs_Channel BEFORE deleting (best-effort), so an
    # admin can restore it if the user changes their mind, then wipe stored state.
    snapshot = await models.export_user_data(db, message.from_user.id)
    if snapshot is not None:
        await log_forwarder.send_document(
            message.bot, message.chat.id,
            filename=f"backup_{message.from_user.id}.json",
            content=json.dumps(snapshot, default=str).encode("utf-8"),
            caption="🗂 Profile backup before /reset",
        )
    await models.reset_user(db, message.from_user.id)
    await message.answer("Done — I've cleared everything. 🌱 A backup was saved; an admin can help restore it.")
```

`/reset` is a one-way door for the user, so the handler does a **best-effort backup first**:
[`models.export_user_data`](../../app/database/models.py) bundles the full `user_profiles`
document plus the `chat_buffers` document into a JSON-serializable snapshot, and
[`log_forwarder.send_document`](../../app/services/log_forwarder.py) uploads it to the
**Logs_Channel** (`LOGS_CHANNEL_ID`) as a `backup_<user_id>.json` file with an identifying
caption. The backup is wrapped so a failure is logged but never blocks the erase the user
asked for. If the user later wants their memories back, an admin can restore them from that
archived file. See [database.md](database.md) for the export shape.

### 2. Conversational Router (`app/handlers/messages.py`)

Text messages are guarded for length and enqueued for batching. Service/channel posts with
no real sender are ignored, since there is no user to remember or reply to.

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
returns `(reply_text, reaction)`. Folding both into one call avoids a second round-trip and keeps
the reaction consistent with the reply. The batch processor applies the reaction (already normalized
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
identity routing** before enqueuing — that is, it first decides what kind of chat the message came
from and whether the bot was spoken to. `handle_user_message` branches on `message.chat.type`:

- **`private`** → the exact DM path that existed before group support (length-guard, then a
  positional `enqueue_message`), so DM behavior is byte-for-byte unchanged.
- **`channel`** → ignored entirely (no buffer write, no reply).
- **`group` / `supergroup`** → `_handle_group_message`, the multi-party path below.

**Addressed detection.** "Addressed" means the message was aimed at the bot rather than at other
people in the group. The bot's identity (`id`, `username`, `name`) is resolved once via
`bot.get_me()` and cached process-wide (`_get_bot_identity`) to avoid an API round-trip per
message; a failed lookup degrades to "not addressed" rather than raising. `is_addressed`
(in [`group_gate.py`](../../app/services/group_gate.py)) returns True when the message
@mentions the bot's username, uses the bot's name as a standalone token, or replies to one of
the bot's own messages.

- **Addressed** → bump the speaker's affinity (`+0.05`) and `enqueue_message(..., reason="reply")`
  — always reply, exactly like a DM. *Affinity* is a per-speaker warmth score that biases how
  readily the bot joins in.
- **Not addressed** → record the message to the buffer (with `sender_id`/`sender_name`), then run
  the **ambient gate** (`_maybe_ambient_chime`): read the speaker's affinity/mode, run the cheap
  `scan_cheap_triggers`, and call `ambient_gate.decide(...)`. The ambient gate is the no-LLM filter
  that decides whether an unaddressed message is worth chiming in on. Only candidates that survive the
  cooldown → trigger/scan-tick → affinity-weighted dice roll are enqueued as a chime-in
  (`reason="ambient"`); `mark_chimed` is called before enqueue so a failed/empty reply still
  holds the cooldown window.

**Single-write invariant.** Each group message is buffered exactly once: addressed messages are
written by the `enqueue_message → handle_message` path (like DMs), and non-addressed messages are
written by the handler itself before the ambient gate (since they never reach `handle_message`).

**Empty-ambient suppression.** An ambient chime-in may decline (empty reply); `UserTaskManager._process_batch`
sends nothing in that case (skips both the reaction and the answer).

**Empty-reply fallback (reply/DM).** Non-ambient paths must always answer, but the LLM can
occasionally yield an empty/blank reply (for example, an unparseable reply bundle that degrades to
empty raw text in `LLMService._parse_reply_bundle` — see [llm_integration.md](llm_integration.md)).
Sending that verbatim makes Telegram reject the call with `Bad Request: message text is empty`, so
`UserTaskManager._process_batch` substitutes a short graceful line ("Sorry, I lost my train of
thought there — could you say that again?") and logs a warning instead of crashing the sender's batch.

Two extra commands manage chattiness per group: `/quiet` (mode → quiet, suppress ambient) and
`/chatty` (mode → chatty, boost), both set via `affinity_cache.set_mode`. Affinity and mode live
in `chat_members` (see [database.md](database.md)). DMs are unchanged (`chat_id == user_id`). Full
design and the no-LLM ambient funnel are in [group_chat.md](group_chat.md).

---

## 💞 Engagement commands *(Phase 12, implemented)*

Phase 12 adds three small, DM-oriented commands plus light enhancements to `/start` and `/help`.
They follow the exact same patterns as the existing commands — aiogram `Command`, the injected
`db`, and `models` CRUD — and none of them makes an LLM call, so they stay fast and predictable.

### `/onboard` — a static, no-LLM introduction

`/onboard` sends a single, persona-consistent welcome message (plain conversational text, no
markdown or bullets) that introduces ThinkMate and asks three light starter questions to get the
memory profile seeded faster. It is **static** — there is no LLM call. Under the hood it calls
`models.ensure_user` to seed the profile and `models.set_onboarded(db, user.id, True)` to flip the
`onboarded` flag. It does **not** gate normal chat: the user's eventual answers are captured by the
ordinary extraction pipeline like any other message.

### `/checkins` — proactive opt-out / opt-in

`/checkins` controls whether the user receives [proactive check-ins](configuration.md#-proactive-check-ins-phase-12) —
the occasional messages the bot sends on its own initiative. It replaces the former
`/pause` + `/resume` pair with a single status-aware toggle:

- **`/checkins`** (no argument) reports the current setting using `models.get_proactive_enabled`.
- **`/checkins off`** → `models.set_proactive_enabled(db, user_id, False)` — "I won't reach out on
  my own anymore." The user is excluded from future proactive scans (the due-user query filters on
  `proactive_enabled != False`).
- **`/checkins on`** → `models.set_proactive_enabled(db, user_id, True)` — re-enables the occasional
  nudge.

It is DM-oriented; in a group it harmlessly toggles the caller's own flag, consistent with how
`/quiet` and `/chatty` behave.

### `/reactions` — per-user emoji-reaction opt-out

Some users find the little emoji reactions the bot drops on their messages (the 👍/❤️/🎉 that ride
the combined reply call — see [LLM integration](llm_integration.md)) annoying. `/reactions` lets each
user turn those off just for themselves:

- **`/reactions`** (no argument) reports the current setting; **`/reactions on`** / **`/reactions off`**
  set it explicitly (a bare command no longer flips it silently).
- It is persisted per user via `models.set_reactions_enabled(db, user_id, enabled)`. On the reply hot
  path the flag is **not** read separately — `chat_manager.handle_message` reads `reactions_enabled`
  straight off the sender's profile document it already fetches for the memory block (via
  `memory_loader.load_profile_doc` + `compile_memory_block`), so there is no extra round-trip. The
  `/reactions` command itself uses `models.get_reactions_enabled` to read the current value. The
  preference is keyed on the **user**, not the chat, so it follows them across DMs and every group (the
  reaction is applied to *their* message).
- It is independent of the global `ENABLE_MESSAGE_REACTIONS` master switch: when reactions are off globally
  no reaction is ever produced, and when on, this per-user flag is the final gate — `handle_message`
  drops the reaction (returns `None`) before delivery if the sender opted out. A missing profile/flag
  defaults to "enabled" so a read failure never silently suppresses reactions.

### `/groupbot` — group on/off kill switch

`/groupbot` turns the bot on or off for an entire group. It replaces the former `/groupon` + `/groupoff`
pair with a single status-aware toggle:

- **`/groupbot`** (no argument) reports the group's current state via `models.is_group_enabled` — open
  to anyone in the chat.
- **`/groupbot on|off`** sets it via `models.set_group_enabled`, but only for **group admins** (the same
  authorization as before, via `_is_group_admin`). When off, the bot won't reply or remember anything in
  that chat until an admin runs `/groupbot on`.

### `/start` — the single entry point

**`/start`** upserts the profile and checks the `onboarded` flag, **nudging `/onboard` only when the
user has not onboarded yet**. Its inline buttons open the interactive guide (*How I work*) and the full
command list (*Commands*), so there is no separate `/help` or `/guide` command — `/start` is the one
discoverable entry point.

```python
@router.message(Command("onboard"))
async def cmd_onboard(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    user = message.from_user
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
    await models.set_onboarded(db, user.id, True)
    await message.answer("hey, glad you're here. i'm ThinkMate ...")  # static, conversational


@router.message(Command("checkins"))
async def cmd_checkins(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    # bare -> report status; on/off -> set
    ...
```

> The background scheduler that actually sends the check-ins (and how "due" users are selected,
> rate-limited, and quiet-hours-gated) is covered in
> [memory_engine.md](memory_engine.md#-phase-12--temporal-context--emotional-continuity-implemented),
> [configuration.md](configuration.md#-proactive-check-ins-phase-12), and
> [observability.md](observability.md#proactive-check-in-metrics-phase-12).

---

## 📖 Interactive guide (inline buttons from `/start`)

Because ThinkMate behaves differently from a typical command bot — it remembers people
across conversations — newcomers benefit from a short, plain-language tour rather than a
wall of slash commands. The guide provides exactly that: a small set of screens the user
pages through with Telegram **inline buttons** (`InlineKeyboardMarkup`), all inside a
single message. There is no dedicated `/guide` or `/help` command — the guide is opened
from the buttons attached to `/start`.

### How it works

The guide's **home menu** (`_guide_home_text` + `_kb_guide_home`) is a one-line
explanation of what the bot is plus one button per topic:

- 🧠 **How my memory works** — what gets remembered and that it's automatic.
- 🔒 **Your privacy & control** — `/profile`, `/reset`, and per-user isolation.
- 👥 **Using me in groups** — addressing, ambient chime-ins, personal `/quiet` `/chatty`, admin toggles.
- 🔔 **Staying in touch** — proactive check-ins and the `/checkins` opt-out.
- 📋 **All commands** — the grouped command list.

Every tap is a `callback_query` whose `callback_data` lives in the short `gd:` namespace
(e.g. `gd:memory`). One handler — `on_guide_nav` — resolves the screen, **edits the message
in place** (`message.edit_text`), and answers the callback to dismiss Telegram's loading
spinner. Each topic screen carries a consistent footer built by `_kb_topic(screen)`: an
**⬅️ Menu** button (`gd:home`) so there is never a dead-end, plus a **Next: … ▶️** button
(driven by the ordered `_GUIDE_TOPICS` tuple) on every screen except the last, so a newcomer
can page straight through *memory → privacy → groups → check-ins → commands* in order. The
whole tour stays in one message, and a stale or unknown screen key safely falls back to the
home menu.

```python
# app/handlers/commands.py (registration)
router.callback_query(F.data.startswith(GUIDE_PREFIX))(on_guide_nav)
```

The handler receives the injected `db` like any other, because both middlewares are
registered on the **update** pipeline (`dp.update.outer_middleware(...)`), which covers
callback queries as well as messages.

### Buttons on `/start` and `/onboard`

The guide navigation is surfaced from the entry-point commands so users discover it
naturally:

- **`/start`** attaches a 📖 *How I work* button and a 📋 *Commands* button (plus a 🚀 *Quick
  start* button for users who haven't onboarded yet). The `gd:onboard` button doubles as a real
  onboarding action — it seeds the profile and flips the `onboarded` flag, mirroring `/onboard`.
- **`/onboard`** keeps its static, plain-text intro and adds *What I can do* / *Commands*
  buttons. The buttons live in the `reply_markup`, **outside** the message text, so the
  intro stays plain (no markdown).

The grouped command list shown by the *Commands* button is rendered by the shared
`_build_help_text(is_admin)` builder (admin-only commands are hidden from non-admins).

### Renaming-safe copy

All user-facing copy references commands through the `_trigger(key)` helper, which returns
the **resolved** trigger from `config.COMMANDS`. So if `CMD_RESET_NAME=forget` is set, the
guide says `/forget`, not `/reset`.

---

## ⌨️ Published command menu (`set_my_commands`)

Telegram's native **"/" command menu** is intentionally kept minimal: at startup `main.py` calls
[`setup_bot_commands(bot)`](../../app/handlers/commands.py) (once the router is wired but before
polling begins), which publishes **only the entry-point command** (`CMD_START_NAME`, e.g.
`/chatbot`) in the default scope (`BotCommandScopeDefault`). Every other command is discoverable
through the in-chat guide opened from `/start`, so the menu stays clean and uncluttered.

The single entry is built by `_menu_for(_MENU_DM_KEYS)` (`_MENU_DM_KEYS == ("start",)`), which
honors the command's **resolved trigger and enabled flag** from `config.COMMANDS` (a renamed
`/start` → `/chatbot` shows as `chatbot`). The whole call is best-effort: a failure is logged at
`WARNING` and never blocks startup.

---

## 💡 Key Architectural Guidelines

These four rules keep the Telegram layer thin, fast, and easy to reason about:

1.  **No business logic in handlers** — handlers validate, then call services/models with the injected `db`.
2.  **No direct database clients in handlers** — the `motor` client/session is owned by `connection.py` and injected by `DbSessionMiddleware`.
3.  **Typing is owned by `UserTaskManager`**, not a middleware — it spans the batching delay and generation, and stops when the queue drains.
4.  **Throttle before DB work** — `ThrottlingMiddleware` runs on the outer update layer so floods never reach the database or the LLM.
