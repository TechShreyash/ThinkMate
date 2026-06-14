# Design Document

## Overview

This feature adds three capabilities to ThinkMate's group path plus three cross-cutting
operational concerns, while preserving the chat-buffer single-write invariant, byte-for-byte
DM behavior, and degrade-never-raise error handling on every hot path:

1. **Identity capture/refresh** — every group message refreshes the sender's stored
   `username`/`display_name` via a read-before-write accessor that never touches memory
   fields.
2. **Identity-backed group extraction** — per-participant extraction persists against the
   same `user_id` that already holds captured identity, and never overwrites identity.
3. **Per-user memory in group replies** — the group reply combines the triggering sender's
   per-user memory block with the group-level block in the system prompt.
4. **Log_Forwarder + Error_Log_Sink** — a best-effort, recursion-safe component that
   forwards three explicit group-memory events to a configurable Telegram logs channel, plus
   a `loguru` sink that forwards every bot-wide `WARNING`+ log record to the same channel.
5. **Per-task LLM metrics + admin reporting** — `metrics.record_llm` is made canonical for
   all six LLM_Task_Types, and the admin `/metrics` report gains a dedicated "LLM calls by
   task" section grouped per task type with counts, success/failure, and latency aggregates.
6. **Environment-configurable commands** — each built-in command's trigger name and
   enabled state become configurable via environment variables through a Command_Config
   accessor and programmatic Command_Registry, with invalid/duplicate triggers degrading to
   defaults rather than crashing startup, and admin authorization preserved.

The design is intentionally additive. No DM code path changes behavior; all new work is
gated behind `is_group` / `chat_type` checks and wrapped so failures degrade. The metrics
and command-configuration changes are deployment-wide (they apply to DM and group alike) but
change neither command behavior nor DM reply behavior — they only change *which* triggers
are registered and *how* metrics are recorded and rendered.

## Architecture

```
Telegram update
      │
      ▼
handlers/messages.py :: _handle_group_message        (Group_Message_Handler)
  ├─ refresh_identity_if_changed(db, sender_id, …)    (Identity_Updater)  ── best-effort
  │     └─ log_forwarder.send(bot, "identity …")      (Log_Forwarder)
  ├─ addressed / implicit / ambient routing (unchanged)
  └─ enqueue_message(...) ─────────────────────────────┐
                                                        ▼
                              user_task_manager._process_batch
                                                        │ passes bot + message
                                                        ▼
              services/chat_manager.py :: handle_message (Chat_Manager)
                ├─ GROUP: build_memory_block(sender_id)  (Per_User_Memory_Block)
                │         build_memory_block(chat_id)     (Group_Memory_Block)
                │         build_system_prompt(persona, group_block, user_block=…)
                └─ DM: unchanged single-party assembly

  buffer overflow ─► run_extractor(chat_id, is_group=True)
                        └─ memory_extractor.extract_and_trim_group
                              ├─ save_extracted_memories(resolved_id, …)  (identity untouched)
                              │     └─ log_forwarder.send(bot, "extraction saved …")
                              └─ skip unresolved ─► log_forwarder.send(bot, "extraction skipped …")

  anywhere in the bot ─► logger.warning/error/... (loguru, level ≥ WARNING)
                            └─ Error_Log_Sink (logger.add(sink, level="WARNING"))
                                  └─ loop.call_soon_threadsafe(create_task(bot.send_message(…)))

  any LLM call ─► metrics.record_llm(task_type, ok, latency)   (Metrics_Registry)
                   └─ llm.<prefix>.calls / .success|.failure / .latency  (canonical 6 types)
                         └─ /metrics (admin) ─► _render_metrics ─► "LLM calls by task" section

  import time ─► register_commands(router)                      (Command_Registry)
                  └─ config.COMMANDS = resolve_command_config()  (Command_Config)
                        └─ router.message(Command(trigger))(handler) for each ENABLED command
```

## Components and Interfaces

### Identity_Updater — `app/database/models.py`

New accessor `refresh_identity_if_changed`, a read-before-write sibling of `ensure_user`
that touches only Identity_Fields:

```python
async def refresh_identity_if_changed(
    db: AsyncIOMotorDatabase, user_id: int, username: str, display_name: str
) -> dict | None:
    """Read the stored profile and write Identity_Fields only when absent or changed.

    Returns a small change descriptor (e.g. {"created": bool, "username": (old, new),
    "display_name": (old, new)}) when a write happened, else None. Never sets or clears
    any Memory_Field. Safe to call on every group message.
    """
    profile = await db["user_profiles"].find_one(
        {"_id": user_id}, {"username": 1, "display_name": 1}
    )

    if profile is None:
        # No profile yet: create one carrying the incoming identity, with the SAME
        # $setOnInsert memory-field skeleton ensure_user uses (empty memory, not empty
        # identity). Use upsert so a concurrent create is idempotent.
        await ensure_user(db, user_id, username, display_name)
        return {"created": True, "username": (None, username),
                "display_name": (None, display_name)}

    set_fields: dict = {}
    if username and profile.get("username") != username:
        set_fields["username"] = username
    if display_name and profile.get("display_name") != display_name:
        set_fields["display_name"] = display_name

    if not set_fields:
        return None  # already current -> no write (Req 1.5)

    set_fields["updated_at"] = _utcnow()
    await db["user_profiles"].update_one({"_id": user_id}, {"$set": set_fields})
    return {"created": False, **{k: (profile.get(k), v)
                                 for k, v in set_fields.items() if k != "updated_at"}}
```

Notes:
- Reads only the two identity fields (`{"username":1,"display_name":1}`) before deciding to
  write (Req 1.1).
- Empty incoming values do not overwrite a populated stored value (the `if username` /
  `if display_name` guards), so a momentarily missing Telegram field can't blank an
  existing identity.
- The `$set` never includes any Memory_Field, so memory is left intact (Req 1.6).

### Group_Message_Handler — `app/handlers/messages.py`

`_handle_group_message` gains one best-effort identity step, placed early (before routing)
so identity is fresh regardless of which routing branch the message takes. It uses the
existing `_display_name(message)` helper and `message.from_user.username`:

```python
sender_name = _display_name(message)
# Best-effort identity capture/refresh for EVERY group sender (Req 1.*).
try:
    change = await models.refresh_identity_if_changed(
        db, message.from_user.id,
        message.from_user.username or "",
        sender_name,
    )
    if change is not None:
        await log_forwarder.send(
            message.bot, message.chat.id,
            f"👤 identity {'created' if change['created'] else 'refreshed'} "
            f"for {message.from_user.id} in chat {message.chat.id}",
        )
except Exception as e:  # noqa: BLE001 - degrade, never raise on the hot path (Req 1.7)
    logger.debug(f"identity refresh failed for {message.from_user.id}: {e}")
```

This runs only on the group path; the DM branch is untouched (Req 5.3). It does not write
the chat buffer, so the Single_Write_Invariant is unaffected (Req 5.1).

### Group_Extractor — `app/services/memory_extractor.py`

The extractor already resolves participants via `_build_name_id_map` (built from buffer
sender attribution) and **skips** unresolved names rather than creating misattributed
profiles (Req 2.3). Two coordinated changes complete this requirement:

- Because identity is captured by the handler on every group message, any participant who
  spoke in the processed segment already has a profile carrying real Identity_Fields keyed
  by `sender_id`. The extractor saves Memory_Fields against that same `sender_id`
  (Req 2.1, 2.2) with no identity work of its own.
- `save_extracted_memories` must not write Identity_Fields. Today its only identity-touch
  is the fallback `ensure_user(db, user_id, "", "")` when the profile is absent, which would
  stamp empty identity. Replace that fallback with an identity-safe create that seeds only
  the memory skeleton (`$setOnInsert`) and leaves `username`/`display_name` unset:

```python
async def _ensure_memory_skeleton(db, user_id: int):
    """Create the memory-field skeleton if absent WITHOUT writing Identity_Fields."""
    now = _utcnow()
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$setOnInsert": {
            "profile_summary": "", "communication_style": "", "emotional_state": None,
            "facts": [], "beliefs": [], "events": [], "insights": [], "mood_history": [],
            "onboarded": False, "created_at": now,
        }},
        upsert=True,
    )
```

`save_extracted_memories` calls `_ensure_memory_skeleton` instead of `ensure_user(...,"","")`,
and its `$set` payload continues to carry only Memory_Fields (Req 2.4). In the normal flow
the profile already exists from identity capture, so this fallback rarely fires.

Logging is wired at the two extractor outcomes (Req 4.3, 4.4): on each `save_extracted_memories`
success emit a `memory-extraction-saved` event, and on each skip emit a
`memory-extraction-skipped` event, both via the Log_Forwarder using the process bot
reference (see below).

### Chat_Manager — `app/services/chat_manager.py`

The group branch of `handle_message` loads two memory blocks and combines them; the DM
branch is byte-for-byte unchanged (Req 3.6, 5.2).

```python
# 3. Assemble system prompt.
persona = _load_persona()

if is_group:
    # Group block keyed by chat_id (existing behavior).
    group_block, needs_compression = await build_memory_block(db, chat_id)
    # Per-user block for the TRIGGERING sender only (Req 3.1, 3.4). Degrade to
    # group-only on failure (Req 3.7).
    user_block = ""
    try:
        user_block, _ = await build_memory_block(db, sender_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"per-user memory load failed for sender {sender_id}: {e}")
        user_block = ""
    system_prompt = build_system_prompt(
        persona, group_block, time_context="", user_memory_text=user_block
    )
else:
    memory_block, needs_compression = await build_memory_block(db, chat_id)
    now = datetime.now(timezone.utc)
    prev = await models.touch_and_get_last_interaction(db, chat_id, now=now)
    time_context = build_time_context(now, prev)
    system_prompt = build_system_prompt(persona, memory_block, time_context=time_context)
```

Composition rules:
- Both blocks are included; the per-user block is **added**, never replaces the group block
  (Req 3.3, 3.5).
- Only the triggering `sender_id` gets a per-user block; no loop over participants (Req 3.4).
- `needs_compression` continues to track the **group** (`chat_id`) block, matching today's
  background-compression trigger semantics.

The Chat_Manager no longer forwards a per-reply event to the logs channel. Operational
visibility into replies comes from the bot's normal `WARNING`+ logging, which the
Error_Log_Sink forwards automatically. Threading the aiogram `bot` through `handle_message`
is therefore no longer required for forwarding; if `bot` is still passed for other uses it
remains DM-safe-defaulted (`bot: Bot | None = None`) and triggers no forwarding here.

### system_prompt — `app/prompts/system_prompt.py`

`build_system_prompt` gains an optional `user_memory_text` parameter. When present
(group path), it renders a distinct per-user section in addition to the existing memory
block; when empty (DM path, or per-user load failed), the output is identical to today so
DM behavior is preserved:

```python
def build_system_prompt(persona_content, active_memory_text, time_context="",
                        user_memory_text=""):
    base = DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        persona_content=persona_content,
        active_memory_text=active_memory_text or "No memories recorded yet. ...",
    )
    user_block = ""
    if user_memory_text and user_memory_text.strip():
        user_block = (
            "\n---\n\n## 🙋 MEMORIES OF THE PERSON SPEAKING NOW:\n"
            "These are your memories of the specific group member you are replying to. "
            "The block above is the shared group context; this block is about THIS person.\n\n"
            f"{user_memory_text.strip()}\n"
        )
    time_block = ""
    if time_context and time_context.strip():
        time_block = f"\n---\n\n## ⏰ TIME CONTEXT\n{time_context.strip()}\n"
    return base + user_block + time_block
```

The shared block is labeled and the per-user block is labeled distinctly so the model can
tell "the room" from "the person I'm answering."

### Log_Forwarder — `app/services/log_forwarder.py` (new)

A tiny best-effort forwarder for the **three explicit** group-memory events: identity
captured/refreshed, extraction-saved, and extraction-skipped. It is a no-op when the source
chat is the Logs_Channel itself (Req 4.10) and swallows all exceptions (Req 4.8). It forwards
no per-reply event — reply-level visibility comes from normal `WARNING`+ logging via the
Error_Log_Sink.

```python
"""Best-effort operational event forwarding to the configured Telegram logs channel.

Forwards exactly three explicit events (identity captured/refreshed, extraction-saved,
extraction-skipped). Every send is wrapped so a forwarding failure can never raise on a hot
path. Events whose SOURCE chat is the logs channel are dropped (Req 4.10). Forwarder logs are
bound with extra={"no_forward": True} so the Error_Log_Sink will not re-forward them (Req 4.9).
"""
from loguru import logger
from app.config import config

# Forwarder logs must never be re-forwarded by the Error_Log_Sink, so bind a marker.
_log = logger.bind(no_forward=True)

# Optional process-wide bot reference for callers that lack a Message (background
# extractor). Set once at startup; handlers pass message.bot directly.
_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


async def send(bot, source_chat_id: int | None, text: str) -> None:
    """Forward `text` to LOGS_CHANNEL_ID. No-op if disabled, recursive, or bot missing."""
    try:
        target = config.LOGS_CHANNEL_ID
        if not target:
            return
        # Anti-recursion: never forward events whose source chat is the logs channel (Req 4.10).
        if source_chat_id is not None and source_chat_id == target:
            return
        b = bot or _bot
        if b is None:
            return
        await b.send_message(chat_id=target, text=text)
    except Exception as e:  # noqa: BLE001 - discard failures (Req 4.8)
        _log.debug(f"log_forwarder send failed (discarded): {e}")
```

Bot availability:
- **Handlers** (`identity` event, group hot-path errors): `message.bot`.
- **Group_Extractor** (`saved`/`skipped` events): no `Message` exists in the background
  task, so it uses the process-wide reference. `main.py` calls
  `log_forwarder.set_bot(bot)` right after `bot = Bot(token=...)`.

`LOGS_CHANNEL_ID` is added to `app/config.py` following the existing pattern:

```python
LOGS_CHANNEL_ID: int = Field(default_factory=lambda: _env_int("LOGS_CHANNEL_ID", -1003933328659))
```

### Error_Log_Sink — `app/services/error_log_sink.py` (new)

A `loguru` sink that forwards every `WARNING`+ record emitted **anywhere** in the bot to the
Logs_Channel (Req 4.5). It is added **alongside** the existing loguru sinks (console +
`logs/bot.log` file sink) — it does not replace them. Because loguru invokes a sink
synchronously inside the originating logging call — frequently from threads with no running
event loop — the sink must (a) never block that call (Req 4.6), (b) never raise back into it
(Req 4.7), and (c) never forward records it (or the Log_Forwarder) produced, to avoid an
infinite forward loop (Req 4.9).

#### Registration — captured bot + main event loop, at startup in `main.py`

```python
# main.py, after the bot is constructed and inside the running async context.
import asyncio
from app.services.error_log_sink import make_error_log_sink

loop = asyncio.get_running_loop()          # the bot's main event loop
logger.add(
    make_error_log_sink(bot, loop),
    level="WARNING",                       # only WARNING and above (Req 4.5)
    filter=lambda r: not r["extra"].get("no_forward"),  # skip forwarder/sink records (Req 4.9)
    enqueue=False,                          # we do our own thread-safe dispatch
)
```

The factory closes over the `bot` reference and the **main** asyncio loop captured at
startup, so the sink can dispatch sends from any thread back onto that loop.

#### Dispatch — non-blocking, thread-safe scheduling onto the captured loop

The sink body runs synchronously wherever the log call happens. It must not `await` and must
not block, so it schedules the actual `bot.send_message(...)` coroutine onto the captured
loop and returns immediately. `loop.call_soon_threadsafe(...)` is used to hop onto the loop
thread, and inside that callback `asyncio.create_task(...)` schedules the send as a
background task:

```python
import contextvars

# Re-entry guard: set while the sink is dispatching, so any logging triggered by the sink
# (or by send_message internals) is not itself forwarded (Req 4.9).
_in_sink: contextvars.ContextVar[bool] = contextvars.ContextVar("in_error_log_sink", default=False)


def make_error_log_sink(bot, loop):
    def sink(message):
        # loguru passes a Message whose .record holds structured fields.
        try:
            record = message.record
            # 1) Re-entry / self-forward guard (Req 4.9).
            if _in_sink.get():
                return
            if record["extra"].get("no_forward"):
                return
            # 2) Level guard (defense-in-depth; logger.add already filters < WARNING) (Req 4.5).
            if record["level"].no < 30:  # WARNING == 30
                return

            text = (
                f"⚠️ {record['level'].name} | {record['name']}:{record['function']} | "
                f"{record['message']}"
            )

            def _dispatch():
                # Runs on the loop thread. Schedule the send; never block the logging call.
                async def _send():
                    token = _in_sink.set(True)
                    try:
                        await bot.send_message(chat_id=config.LOGS_CHANNEL_ID, text=text)
                    except Exception:  # noqa: BLE001 - never propagate (Req 4.7)
                        pass
                    finally:
                        _in_sink.reset(token)
                try:
                    asyncio.create_task(_send())
                except Exception:  # noqa: BLE001 - loop not running / shutting down
                    pass

            # 3) Hop onto the loop thread without blocking the originating logging call (Req 4.6).
            loop.call_soon_threadsafe(_dispatch)
        except Exception:  # noqa: BLE001 - the sink must NEVER raise into logging (Req 4.7)
            pass

    return sink
```

Why this is safe and non-blocking:
- `loop.call_soon_threadsafe` is the supported way to schedule work onto an event loop from
  any thread; it merely enqueues the callback and returns immediately, so the logging call is
  never blocked (Req 4.6). It works whether or not the calling thread has its own loop.
- The real I/O (`bot.send_message`) runs as a task on the bot's loop, decoupled from the log
  call. `run_coroutine_threadsafe(coro, loop)` is an equivalent alternative; we prefer
  `call_soon_threadsafe` + `create_task` because we never need to await the result and want a
  pure fire-and-forget with no returned future to manage.
- Every layer is wrapped: the outer sink body swallows all exceptions so nothing propagates
  back into the originating `logger.warning(...)` call (Req 4.7), and the inner `_send`
  swallows transport failures.

#### Anti-recursion / re-entry guard

Three independent mechanisms prevent the sink from forwarding its own (or the forwarder's)
output and looping forever (Req 4.9):

1. **`logger.add(..., filter=...)`** drops records bound with `extra={"no_forward": True}`
   before the sink ever sees them. The Log_Forwarder binds this marker on its own logs, and
   any failure-logging inside the sink path uses a `logger.bind(no_forward=True)` instance.
2. **`_in_sink` contextvar** is set to `True` while the send coroutine runs; if dispatching
   the send emits any `WARNING`+ record, the sink sees `_in_sink == True` and returns
   immediately. A contextvar is used so the guard is correctly scoped to the dispatch task
   and not shared incorrectly across unrelated tasks.
3. A failure inside the sink is **swallowed silently** (no `logger` call) or, when it must be
   recorded, emitted through a `logger.bind(no_forward=True)` instance so it cannot re-enter
   the sink.

#### Message formatting

A concise one-line record is forwarded: severity level name, the originating
logger/module and function, and the message text — e.g.
`⚠️ ERROR | app.services.chat_manager:handle_message | LLM call failed: timeout`.

#### Interaction with existing loguru setup

The project already configures a console sink and a `logs/bot.log` file sink. The
Error_Log_Sink is **added next to them** via an additional `logger.add(...)`; existing sinks
keep receiving all records at their configured levels. The new sink only changes where
`WARNING`+ records are *additionally* delivered (the Logs_Channel) and never removes or
reconfigures the file/console sinks.

### Metrics_Reporter — `app/services/metrics.py` + `app/handlers/commands.py`

This component makes per-task LLM-call accounting canonical and exposes it through the
admin-gated `/metrics` command (Req 6). It adds **no new storage** — it reuses the existing
in-memory `MetricsRegistry` counters and timers and the existing `record_llm` recording path.

#### Canonical LLM_Task_Type → prefix map (`metrics.py`)

`record_llm` already records, per call, `llm.<prefix>.calls`, `llm.<prefix>.success` or
`llm.<prefix>.failure`, and the `llm.<prefix>.latency` timer. The current `_LLM_TYPE_PREFIX`
maps only four of the six LLM_Task_Types:

| LLM_Task_Type | current prefix | metric names today |
|---|---|---|
| `chat_reply` | `reply` | `llm.reply.calls/.success/.failure/.latency` |
| `memory_extraction` | `extraction` | `llm.extraction.*` |
| `group_memory_extraction` | `group_extraction` | `llm.group_extraction.*` |
| `memory_compression` | `compression` | `llm.compression.*` |
| `memory_consolidation` | *(falls through, used as-is)* | `llm.memory_consolidation.*` |
| `proactive_checkin` | *(falls through, used as-is)* | `llm.proactive_checkin.*` |

The fall-through for the last two is functionally correct (counts are still recorded under
`llm.memory_consolidation.calls` / `llm.proactive_checkin.calls`), but it leaves the prefix
set implicit and inconsistent. The design makes all six explicit by completing the map and
exporting a single canonical, ordered source of truth that both the recorder and the reporter
consume:

```python
# metrics.py — one canonical, ordered mapping consumed by record_llm AND the reporter.
_LLM_TYPE_PREFIX: dict[str, str] = {
    "chat_reply": "reply",
    "memory_extraction": "extraction",
    "group_memory_extraction": "group_extraction",
    "memory_compression": "compression",
    "memory_consolidation": "consolidation",   # now explicit (Req 6.3)
    "proactive_checkin": "checkin",             # now explicit (Req 6.3)
}

# Public, ordered view for reporters: list[(task_type, prefix)].
LLM_TASK_TYPES: tuple[tuple[str, str], ...] = tuple(_LLM_TYPE_PREFIX.items())
```

`record_llm` is unchanged in shape — it still does `_LLM_TYPE_PREFIX.get(call_type, call_type)`
and still wraps its body so a metrics failure never raises into the LLM call site (Req 6.7).
Completing the map only changes the prefix used for the two previously-implicit types; any
unknown future `call_type` still falls through to its own string, so the recorder never drops
a call (Req 6.1, 6.2).

> **Migration note:** changing the two prefixes renames their counters from
> `llm.memory_consolidation.*` / `llm.proactive_checkin.*` to `llm.consolidation.*` /
> `llm.checkin.*`. Because the registry is process-wide and in-memory (no persistence), this
> takes effect cleanly on restart with no migration; any other reader of the raw snapshot
> (e.g. the `/health` summary) keys off counter names and is unaffected because it sums by
> pattern, not by these specific two names.

#### `_render_metrics` enhancement (`commands.py`)

`_render_metrics(snap)` gains a dedicated **"LLM calls by task"** section, rendered above the
existing raw counters/gauges/timers dump (which is retained for completeness). It iterates the
canonical `LLM_TASK_TYPES` so every task type appears in a stable order, and derives each
task's figures from the snapshot's `counters` and `timers`:

```python
from app.services.metrics import metrics, LLM_TASK_TYPES

def _render_llm_by_task(snap: dict) -> list[str]:
    counters = snap.get("counters", {}) or {}
    timers = snap.get("timers", {}) or {}
    lines = ["LLM calls by task:"]
    for task_type, prefix in LLM_TASK_TYPES:
        calls = counters.get(f"llm.{prefix}.calls", 0)
        success = counters.get(f"llm.{prefix}.success", 0)
        failure = counters.get(f"llm.{prefix}.failure", 0)
        lat = timers.get(f"llm.{prefix}.latency", {}) or {}
        avg = lat.get("avg", 0)
        mx = lat.get("max", 0)
        lines.append(
            f"  {task_type}: calls={calls} ok={success} fail={failure} "
            f"avg={avg} max={mx}"
        )
    return lines
```

`_render_metrics` calls `_render_llm_by_task(snap)` and prepends its lines, then appends the
existing counters/gauges/timers block unchanged. Key behaviors:

- **Every task type is listed**, even with no recorded calls, because the loop is driven by
  the canonical `LLM_TASK_TYPES` rather than by which counters happen to exist; a missing
  counter renders as `0` via the `.get(..., 0)` default (Req 6.4, 6.8).
- **Aggregates are shown when present.** `success`/`failure` come from counters and `avg`/`max`
  from the latency timer; when a task has no timer yet, `lat` is `{}` and `avg`/`max` render as
  `0` (Req 6.5, 6.8).
- **No new storage**: all figures are read from `metrics.snapshot()`.

#### Admin_Gate preserved

`cmd_metrics` keeps its existing first line `if not _admin_allowed(message): return` so the
report is rendered only for authorized requesters (Req 6.6). The reporting enhancement lives
entirely inside `_render_metrics`, downstream of the gate, so authorization is unchanged.

### Command_Config — `app/config.py`

Command_Config resolves, for each Built_In_Command, a `(trigger_name, enabled)` pair from the
environment using the existing `_env_str` / `_env_bool` helpers, defaulting to the command's
current name and `enabled=True` (Req 7.1, 7.2). It is the single source of truth the
Command_Registry consumes.

```python
# config.py
import re

# Canonical built-in command keys, in help-display order. The key is also the DEFAULT
# trigger name for that command.
_BUILTIN_COMMANDS: tuple[str, ...] = (
    "start", "onboard", "pause", "resume", "help",
    "profile", "reset", "quiet", "chatty", "health", "metrics",
)

# Telegram command name rule: 1-32 chars, letters/digits/underscore. Used to reject
# invalid configured trigger names (e.g. containing spaces, "/", or punctuation).
_CMD_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def resolve_command_config() -> dict[str, tuple[str, bool]]:
    """Resolve {command_key: (trigger_name, enabled)} for every Built_In_Command.

    Reads CMD_<KEY>_NAME (trigger override, default = key) and CMD_<KEY>_ENABLED
    (bool, default True). Invalid trigger names fall back to the default; a trigger
    that duplicates another command's resolved trigger falls back to the default for
    BOTH colliding commands. Never raises: any unexpected parse error yields the
    all-defaults mapping (Req 7.5, 7.7).
    """
    try:
        raw: dict[str, tuple[str, bool]] = {}
        for key in _BUILTIN_COMMANDS:
            name = _env_str(f"CMD_{key.upper()}_NAME", key).strip().lstrip("/")
            enabled = _env_bool(f"CMD_{key.upper()}_ENABLED", True)
            if not _CMD_NAME_RE.match(name):
                logger.warning(
                    f"command config: invalid trigger {name!r} for {key!r}; "
                    f"falling back to default {key!r}"
                )
                name = key
            raw[key] = (name, enabled)

        # Duplicate detection among ENABLED commands' resolved triggers. Any command
        # whose trigger collides with another's falls back to its own default (the key).
        # Defaults are unique by construction, so fallback always resolves the collision.
        seen: dict[str, list[str]] = {}
        for key, (name, enabled) in raw.items():
            if enabled:
                seen.setdefault(name, []).append(key)
        for name, keys in seen.items():
            if len(keys) > 1:
                logger.warning(
                    f"command config: trigger {name!r} duplicated by {keys}; "
                    f"falling back to default names for those commands"
                )
                for key in keys:
                    enabled = raw[key][1]
                    raw[key] = (key, enabled)  # default trigger == key
        return raw
    except Exception as exc:  # never crash startup (Req 7.7)
        logger.warning(f"command config parse failed; using all defaults: {exc}")
        return {key: (key, True) for key in _BUILTIN_COMMANDS}


class Config(BaseModel):
    ...
    # Resolved once at import; a plain dict {key: (trigger, enabled)}.
    COMMANDS: dict[str, tuple[str, bool]] = Field(default_factory=resolve_command_config)
```

Resolution rules and rationale:

- **Default trigger = key.** When `CMD_<KEY>_NAME` is unset, the command keeps its current
  name (Req 7.1). When `CMD_<KEY>_ENABLED` is unset, the command is enabled (Req 7.2).
- **Invalid trigger → default + warning.** A trigger failing `_CMD_NAME_RE` (spaces, slashes,
  punctuation, empty, >32 chars) is replaced by the default and a warning is logged; startup
  continues (Req 7.5).
- **Duplicate trigger → default for all colliding commands + warning.** Only *enabled*
  commands participate in collision detection (a disabled command claims no trigger). Because
  the per-command default is its unique key, falling colliding commands back to defaults is
  guaranteed to resolve the collision deterministically (Req 7.5).
- **Never raises.** The whole body is wrapped; any unexpected failure yields the all-defaults,
  all-enabled mapping so the bot always starts with its standard command surface (Req 7.7).
- A name→name *swap* between two commands (each taking the other's default) is not a
  duplicate — both resolved triggers remain distinct — so it is honored as-is; each trigger
  still maps to exactly one handler.

### Command_Registry — `app/handlers/commands.py`

The handlers are no longer bound with `@router.message(Command("..."))` decorators. Instead
each handler is a plain coroutine, and a `register_commands(router)` function binds the
enabled commands to their resolved triggers programmatically, driven by Command_Config.

```python
# commands.py — handlers defined WITHOUT decorators:
async def cmd_start(message: Message, db: AsyncIOMotorDatabase): ...
async def cmd_onboard(message: Message, db: AsyncIOMotorDatabase): ...
# ... etc, unchanged bodies ...
async def cmd_health(message: Message, db: AsyncIOMotorDatabase):
    if not _admin_allowed(message):
        return  # admin gate is INSIDE the handler, independent of trigger name (Req 7.6)
    ...
async def cmd_metrics(message: Message, db: AsyncIOMotorDatabase):
    if not _admin_allowed(message):
        return  # admin gate preserved regardless of configured trigger (Req 7.6)
    ...

# Static map: command_key -> (handler, help description). Order follows _BUILTIN_COMMANDS.
_COMMANDS: dict[str, tuple] = {
    "start":   (cmd_start,   "say hi and set up your profile"),
    "onboard": (cmd_onboard, "help me get to know you"),
    "pause":   (cmd_pause,   "stop me from messaging first"),
    "resume":  (cmd_resume,  "let me check in again"),
    "help":    (cmd_help,    "show this message"),
    "profile": (cmd_profile, "see what I remember about you"),
    "reset":   (cmd_reset,   "make me forget everything (with confirmation)"),
    "quiet":   (cmd_quiet,   "I'll chime in less in this group"),
    "chatty":  (cmd_chatty,  "I'll chime in more in this group"),
    "health":  (cmd_health,  "ops health report (admin)"),
    "metrics": (cmd_metrics, "ops metrics report (admin)"),
}


def register_commands(router: Router) -> None:
    """Bind each ENABLED Built_In_Command to its configured trigger (Req 7.3, 7.4)."""
    resolved = config.COMMANDS
    for key, (handler, _desc) in _COMMANDS.items():
        trigger, enabled = resolved.get(key, (key, True))
        if not enabled:
            logger.info(f"command {key!r} disabled by config; not registering")
            continue  # disabled -> unregistered -> no response to its trigger (Req 7.3)
        try:
            router.message(Command(trigger))(handler)
        except Exception as exc:  # extreme defense: bad trigger slipped through
            logger.warning(
                f"failed to register command {key!r} as {trigger!r} ({exc}); "
                f"registering under default {key!r}"
            )
            router.message(Command(key))(handler)


# Bind at import time so handlers/__init__.py picks up a fully-wired router.
register_commands(router)
```

Behavior:

- **Disabled commands are never registered**, so aiogram has no matching handler and the bot
  sends no response to that trigger (Req 7.3). DM and group alike simply ignore the command.
- **Renamed commands bind their existing behavior to the new trigger** — the handler body is
  unchanged; only the `Command(...)` filter value differs (Req 7.4).
- **Admin commands keep the Admin_Gate.** `_admin_allowed` is checked *inside* `cmd_health`
  and `cmd_metrics`, so authorization is independent of the configured trigger name and
  survives any rename (Req 7.6).
- **Registration runs at import** (bottom of `commands.py`), before `handlers/__init__.py`
  composes `main_router`, so the existing `include_router(commands_router)` wiring in
  `main.py` is unchanged. Alternatively `register_commands` can be called from `main()` after
  config load; import-time binding is chosen to keep the router self-contained and avoid a
  registration ordering dependency.

#### Dynamic `/help` text

`cmd_help` no longer hard-codes the command list. It renders one line per *enabled* command
using the resolved trigger and the description from `_COMMANDS`, so the help text always
reflects the configured (renamed / enabled) surface (Req 7.3, 7.4):

```python
async def cmd_help(message: Message):
    resolved = config.COMMANDS
    lines = [f"{html.bold('Here is what I can do:')}", ""]
    for key, (_handler, desc) in _COMMANDS.items():
        trigger, enabled = resolved.get(key, (key, True))
        if not enabled:
            continue
        # Admin-only commands are omitted from the public help in non-admin contexts.
        if key in ("health", "metrics") and not _admin_allowed(message):
            continue
        lines.append(f"/{trigger} — {desc}")
    lines += ["", "Mostly though, just talk to me. 🙂"]
    await message.answer("\n".join(lines), parse_mode="HTML")
```

A disabled command is omitted from `/help`; a renamed command appears under its new trigger.

## Data Models

### `user_profiles` identity vs memory fields

A User_Profile document is keyed by Telegram `user_id` (`_id`). Its fields split into two
disjoint groups that this feature keeps strictly separated:

| Group | Fields | Written by |
|-------|--------|------------|
| Identity_Fields | `username`, `display_name` | `ensure_user`, `refresh_identity_if_changed` only |
| Memory_Fields | `profile_summary`, `communication_style`, `emotional_state`, `facts`, `beliefs`, `events`, `insights`, `mood_history` | `save_extracted_memories`, `replace_user_memory`, `apply_consolidation` only |

Invariants enforced by this feature:
- An identity write never includes a Memory_Field key in its `$set` (Req 1.6).
- A memory write never includes `username`/`display_name` in its `$set`, and its
  absent-profile fallback seeds only the memory skeleton (Req 2.4).
- Profiles created during group extraction are keyed by the resolved `sender_id`, the same
  id identity capture used, so identity and memory converge on one document (Req 2.2).

## System-Prompt Composition Change

DM (`chat_type == "private"`): `build_system_prompt(persona, memory_block, time_context)` —
unchanged output, no per-user block.

Group: `build_system_prompt(persona, group_block, time_context="", user_memory_text=user_block)`
— the shared group block plus a clearly-labeled per-user block for the triggering sender.
When `user_block` is empty (load failed), the rendered prompt is exactly the group-only
prompt, which is the graceful-degradation target (Req 3.7).

## End-to-End Sequence of a Group Message

1. Update arrives; `handle_user_message` routes group/supergroup to `_handle_group_message`.
2. `_handle_group_message` computes `sender_name` and calls `refresh_identity_if_changed`
   (best-effort). On a write, a `identity` event is forwarded to the logs channel.
3. Spam/addressed/implicit/ambient routing runs unchanged. The message is buffered exactly
   once by the existing writer for its path (enqueue path or ambient drop path).
4. On an addressed/implicit/ambient-pass message, `enqueue_message` batches it; `_process_batch`
   groups by sender and calls `handle_message(db, chat_id, text, chat_type=..., sender_id=...,
   bot=last_message.bot)`.
5. `handle_message` (group branch) appends to the buffer, renders multi-party history, loads
   the group block (`chat_id`) and the triggering sender's per-user block (`sender_id`),
   composes the system prompt, and generates the reply.
6. The reply is returned and sent. No per-reply event is forwarded; any `WARNING`+ log
   emitted during the reply is forwarded automatically by the Error_Log_Sink.
7. On buffer overflow, `run_extractor(chat_id, is_group=True)` runs `extract_and_trim_group`:
   per-participant updates are saved against resolved `sender_id`s (with `saved`/`skipped`
   events forwarded), and the processed segment is atomically trimmed.

## Error Handling

### Degradation strategy

Every new operation sits on or near a hot path and follows the project's degrade-never-raise
contract:

- Identity read/write failure → caught in `_handle_group_message`; message processing
  continues (Req 1.7, 5.4).
- Per-user block load failure → caught in `handle_message`; reply is generated with the
  group block only (Req 3.7).
- Any group-memory operation failure on a hot path → caught locally; processing continues
  (Req 5.4). The failure surfaces operationally through the bot's `WARNING`+ logging, which
  the Error_Log_Sink forwards to the Logs_Channel.
- Log_Forwarder send failure → swallowed inside `send` (Req 4.8); callers never see it.
- Error_Log_Sink failure → the sink body and its inner send both swallow exceptions, so a
  forwarding failure never propagates back into the originating logging call (Req 4.7) and
  never blocks it (Req 4.6).
- Recursion guards → the Log_Forwarder drops events whose source chat is the logs channel
  (Req 4.10); the Error_Log_Sink filters out `no_forward`-marked records and uses an
  `_in_sink` re-entry guard so its own and the forwarder's records are never re-forwarded
  (Req 4.9).
- DM path → none of the above runs; behavior is byte-for-byte unchanged (Req 5.2, 5.3).
- Metrics recording failure → `record_llm` (and every `MetricsRegistry` mutator) wraps its
  body in `try/except` and logs at debug, so a metrics error never raises into the LLM call
  site (Req 6.7). `snapshot()` always returns a well-formed structure, so the reporter never
  sees a malformed snapshot; a task type with no recorded calls renders as `0` rather than
  raising (Req 6.8).
- Command config parse failure → `resolve_command_config` catches any unexpected error and
  returns the all-defaults, all-enabled mapping; per-command invalid or duplicate triggers
  fall back to that command's default with a logged warning. Startup always continues with a
  usable command surface (Req 7.5, 7.7). `register_commands` additionally guards each binding
  so an unexpected `Command(...)` failure falls back to the default trigger rather than
  aborting registration.

## Testing Strategy

Tooling: `pytest` + `hypothesis` for property tests; `mongomock`-backed async DB (the
existing test pattern that injects an in-memory `AsyncIOMotorDatabase`) for accessor and
flow tests. Each property test runs ≥100 iterations and is tagged with its design property.

Property tests (universal):
- Identity read-before-write: refresh only writes when absent/changed, never touches memory,
  and is a no-op when current.
- Memory/identity separation: `save_extracted_memories` never alters Identity_Fields across
  arbitrary extractions and arbitrary starting profiles.
- Group-reply composition: for any non-empty per-user and group blocks, the assembled prompt
  contains both, and an empty per-user block yields the group-only prompt verbatim.
- Log_Forwarder recursion safety: for any text, a send whose source chat equals
  `LOGS_CHANNEL_ID` performs no send; a failing transport never raises.
- Error_Log_Sink level filtering: for any record, the sink forwards iff the record's level is
  `WARNING`+ and ignores anything below.
- Error_Log_Sink re-entry safety: for any record, a record marked `no_forward` or emitted
  while `_in_sink` is set produces no forward (no infinite recursion).
- Error_Log_Sink never raises: for any record and any failing dispatch/transport, invoking
  the sink returns without raising back into the logging call.
- Metrics per-task counting: for any sequence of `record_llm` calls across the six
  LLM_Task_Types, the rendered report shows, per task type, a call count equal to the number
  of calls recorded for it (and success+failure counts summing to that total).
- Command enable/disable: for any subset of commands marked disabled, `register_commands`
  binds exactly the enabled commands and none of the disabled ones.
- Command rename: for any valid trigger override, the command binds under the new trigger and
  the help text lists it under that trigger.
- Command duplicate/invalid fallback: for any configuration that assigns an invalid trigger
  or duplicates another enabled command's trigger, the affected commands resolve to their
  default triggers and the resolved trigger set contains no duplicates.

Example/edge tests:
- Empty/None incoming username or display_name does not blank an existing identity.
- Unresolved participant name → skip (no profile created), resolved name → saved.
- Identity-safe memory skeleton creation leaves `username`/`display_name` unset.
- `record_llm("memory_consolidation", ...)` and `record_llm("proactive_checkin", ...)` record
  under the canonical `llm.consolidation.*` / `llm.checkin.*` prefixes, and the report lists
  both task types (verifying the previously-implicit fall-through is now explicit).
- A task type with zero recorded calls renders `calls=0` in the report rather than raising.
- `cmd_metrics` returns without rendering when `_admin_allowed` is False (admin gate intact).
- `cmd_health`/`cmd_metrics` remain admin-gated after being remapped to a custom trigger.
- A name swap (two commands taking each other's default name) is honored without fallback,
  since neither resolved trigger duplicates the other.

Regression (DM unchanged):
- A focused suite asserting the DM `handle_message` path produces the same system prompt,
  same buffer writes, and same return contract as before (no per-user block, no forwarding,
  no identity capture). This is the primary guard for Req 5.2/5.3.

Integration/smoke:
- `LOGS_CHANNEL_ID` loads from config with the documented default.
- Log_Forwarder `send` is invoked at each of the three explicit wiring points — identity,
  extraction-saved, extraction-skipped (verified with a mock bot), asserting calls happen and
  that failures are swallowed — not asserting Telegram delivery.
- Error_Log_Sink dispatch: with a mock `bot` and a fake/loop double (a stub whose
  `call_soon_threadsafe` runs the callback inline), assert that a `WARNING`+ log results in
  exactly one `bot.send_message` call, that a sub-`WARNING` log results in none, that a
  `no_forward`-bound log results in none, and that a `bot.send_message` that raises does not
  propagate. Verify the sink call itself returns immediately (non-blocking) by asserting it
  does not await the send.
- `resolve_command_config` loads from env: unset variables yield the all-defaults mapping
  (every command enabled under its own name); `CMD_HELP_ENABLED=false` disables `help`;
  `CMD_START_NAME=hello` remaps `start`; an invalid `CMD_START_NAME=" bad/name"` and a
  duplicate `CMD_PROFILE_NAME=help` each fall back to defaults; a malformed environment is
  swallowed and yields all-defaults.
- `register_commands` against a fresh `Router` registers exactly the enabled commands under
  their resolved triggers (assert via the router's registered handlers / a dispatch probe),
  and leaves disabled commands unmatched.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Identity write only on absent or changed

*For any* stored profile and any non-empty incoming `username`/`display_name`, calling `refresh_identity_if_changed` results in the stored Identity_Fields equaling the incoming values when they were absent or differed (creating the profile with the incoming identity when none existed).

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

### Property 2: No identity write when already current

*For any* profile whose stored Identity_Fields already equal the incoming values, `refresh_identity_if_changed` performs no identity write (returns no change and leaves the document unchanged).

**Validates: Requirements 1.5**

### Property 3: Identity writes never alter Memory_Fields

*For any* profile carrying arbitrary Memory_Fields, an identity refresh leaves every Memory_Field (`profile_summary`, `communication_style`, `emotional_state`, `facts`, `beliefs`, `events`, `insights`, `mood_history`) byte-for-byte unchanged.

**Validates: Requirements 1.6**

### Property 4: Memory writes never alter Identity_Fields

*For any* profile with set Identity_Fields and any extraction, `save_extracted_memories` leaves `username` and `display_name` unchanged.

**Validates: Requirements 2.4**

### Property 5: Extracted memory lands on the identity-bearing user_id

*For any* group segment whose participants were identity-captured, the Group_Extractor persists each resolved participant's Memory_Fields against the same `sender_id` that holds that participant's real (non-empty) Identity_Fields.

**Validates: Requirements 2.1, 2.2**

### Property 6: Unresolved participants are skipped without profile creation

*For any* extraction update whose participant name does not resolve to a `sender_id` in the segment's name→id map, the Group_Extractor creates no profile and writes no memory for that name.

**Validates: Requirements 2.3**

### Property 7: Group reply prompt contains both memory blocks

*For any* non-empty per-user memory block and group memory block, the assembled group system prompt contains both blocks; and when the per-user block is empty, the assembled prompt equals the group-only prompt.

**Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.7**

### Property 8: Log_Forwarder anti-recursion on source chat

*For any* event text, when the source chat equals `LOGS_CHANNEL_ID`, the Log_Forwarder sends nothing.

**Validates: Requirements 4.10**

### Property 9: Log_Forwarder never raises

*For any* event text, when the underlying send transport fails, the Log_Forwarder discards the failure and returns without raising.

**Validates: Requirements 4.8**

### Property 10: Error_Log_Sink forwards exactly the WARNING+ records

*For any* log record, the Error_Log_Sink forwards it to the Logs_Channel if and only if the record's severity level is `WARNING` or higher, and ignores any record below `WARNING`.

**Validates: Requirements 4.5**

### Property 11: Error_Log_Sink never re-forwards its own or the Log_Forwarder's records

*For any* log record marked `no_forward` (records produced by the Log_Forwarder or by the sink's own send path) or emitted while the sink's re-entry guard is set, the Error_Log_Sink performs no forward, so forwarding can never trigger further forwarding (no infinite recursion).

**Validates: Requirements 4.9**

### Property 12: Error_Log_Sink is non-blocking and never raises into the logging call

*For any* `WARNING`+ log record, invoking the Error_Log_Sink schedules the send onto the captured event loop and returns without blocking the originating logging call; and for any failing dispatch or transport, the sink discards the failure and never propagates an exception back into that logging call.

**Validates: Requirements 4.6, 4.7**

### Property 13: Per-task LLM call counting is exact

*For any* finite sequence of `record_llm(task_type, ok, latency)` calls drawn from the six LLM_Task_Types, the resulting snapshot records, for each task type, a `llm.<prefix>.calls` count equal to the number of calls made for that type, with `success + failure == calls` and the latency timer's `count == calls` for that type.

**Validates: Requirements 6.1, 6.2, 6.3**

### Property 14: Metrics report lists every task type with count and available aggregates

*For any* metrics snapshot, the rendered report contains exactly one "LLM calls by task" line per canonical LLM_Task_Type showing that type's call count (rendering `0` when no calls were recorded), and includes the success, failure, and latency aggregates alongside the count whenever those aggregates exist for the type — never raising for an absent task type.

**Validates: Requirements 6.4, 6.5, 6.8**

### Property 15: Metric recording never raises into the call site

*For any* `record_llm` invocation, including when an internal registry mutation fails, the call returns without propagating an exception back to the LLM call site.

**Validates: Requirements 6.7**

### Property 16: Command config defaults when env is unset

*For any* Built_In_Command whose `CMD_<KEY>_NAME` and `CMD_<KEY>_ENABLED` variables are unset, Command_Config resolves that command's trigger to its own key (current name) and its enabled state to `True`.

**Validates: Requirements 7.1, 7.2**

### Property 17: Disabled commands are not registered

*For any* subset of Built_In_Commands configured disabled, `register_commands` binds exactly the enabled commands to triggers and registers no handler for any disabled command (so its trigger draws no response).

**Validates: Requirements 7.3**

### Property 18: Renamed command binds unchanged behavior to the configured trigger

*For any* Built_In_Command assigned a valid override trigger name, `register_commands` binds that command's original handler callable under the configured trigger, leaving the handler behavior unchanged.

**Validates: Requirements 7.4**

### Property 19: Invalid or duplicate triggers fall back to defaults without crashing

*For any* command configuration containing invalid trigger names, triggers duplicating another enabled command's trigger, or an otherwise unparseable configuration, Command_Config resolves every affected command to its default trigger (its key), yields a set of enabled triggers with no duplicates, and returns without raising so startup continues.

**Validates: Requirements 7.5, 7.7**
