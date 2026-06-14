# Sliding Window Memory Engine Details

This document covers the core memory architecture of ThinkMate, detailing the sliding window extraction pipeline, memory loader block compilers, and memory compressors. All components are updated to use Pydantic models and MongoDB.

The *memory engine* is the subsystem that lets ThinkMate remember a user across conversations without ever feeding an ever-growing transcript to the language model. It does this with a **sliding window**: only the most recent messages are kept verbatim in a short-lived buffer, while older messages are distilled into a compact, long-lived **memory profile** (facts, beliefs, events, mood, and — later — behavioral insights). The compiled profile is the **memory block** that gets injected into the system prompt on each reply, so the bot stays informed while the prompt stays bounded.

Three priorities shape every design decision in this engine, in order: **responsiveness** (never block the user's reply on bookkeeping), **robustness** (never lose or corrupt memory, even during an LLM outage), and **minimizing LLM calls** (each call costs latency and money). Throughout this guide, the *hot path* means the per-message request/response flow that produces a reply; anything that can run after the reply is pushed off the hot path into a background task.

### What this guide covers

- **🛠️ Chat Manager Orchestration** — the per-message entry point that ties everything together: append the message, trigger background work when buffers or budgets are exceeded, compile the prompt, and make a single LLM call.
- **🔍 Memory Extraction Logic** — how a full buffer window is summarized into durable memory, with retries and a "trim anyway on failure" safety rule.
- **🧹 Memory Compression** — how an over-budget profile is shrunk without ever wiping memory on failure.
- **🔒 Shared Task Concurrency Lock** — the per-user lock that keeps extraction, compression, and consolidation from racing each other.
- **👥 Multi-Party Extraction in Groups** *(Phase 9)* — how group chats share one buffer but keep memory per user.
- **🌙 Phase 11 — Periodic consolidation** — the long-horizon "dreaming" pass that reviews the whole profile and synthesizes behavioral insights.
- **⏰ Phase 12 — Temporal context & emotional continuity** — small, additive features that give the bot a sense of time and a mood trend.

For sibling subsystems, see the group-chat flow in [group_chat.md](group_chat.md), the LLM schemas in [llm_integration.md](llm_integration.md), and the tunable keys in [configuration.md](configuration.md).

---

## 🛠️ Chat Manager Orchestration (`chat_manager.py`)

The orchestration process in [chat_manager.py](../../app/services/chat_manager.py) coordinates message updates, triggers memory extraction, compiles prompts, and runs chat generation:

```python
# app/services/chat_manager.py
import asyncio
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.llm_service import llm_service          # shared singleton
from app.services.memory_loader import build_memory_block
from app.prompts.system_prompt import build_system_prompt

async def handle_message(
    db: AsyncIOMotorDatabase, user_id: int, user_text: str
) -> tuple[str, str | None]:
    # 1. Append the user message; the returned array gives char count + active
    #    history in a single round-trip (no separate buffer reads).
    messages = await models.add_message_to_buffer(db, user_id, "user", user_text)
    buffer_chars = sum(len(m["content"]) for m in messages)
    active_history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # 2. Buffer overflow -> non-blocking background extraction.
    if buffer_chars >= config.CHAT_BUFFER_MAX_CHARS:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_extractor(user_id))

    # 3. Assemble the system prompt (persona is cached by mtime; see _load_persona).
    memory_block, needs_compression = await build_memory_block(db, user_id)
    system_prompt = build_system_prompt(_load_persona(), memory_block)

    # 4. ONE LLM call -> reply + optional reaction.
    reply_text, reaction = await llm_service.generate_reply_bundle(
        user_id, system_prompt, active_history
    )

    # 5. Persist the assistant reply.
    await models.add_message_to_buffer(db, user_id, "assistant", reply_text)

    # 6. Memory over budget -> rate-limited background compression.
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_compressor(user_id))

    return reply_text, reaction
```

> The persona file is read through `_load_persona`, which re-reads only when the file's mtime
> changes — preserving "edit persona without restart" while avoiding a blocking disk read on
> every message. The reply and reaction are produced in a single call, so the batch processor
> simply applies the returned reaction and sends the reply.

---

## 🔍 Memory Extraction Logic (`memory_extractor.py`)

The memory extraction pipeline in [memory_extractor.py](../../app/services/memory_extractor.py) extracts key details from conversation histories and saves them to the database. All LLM access goes through the shared `llm_service` singleton (one client/connection pool per process).

The extraction call is **retried up to `MAX_EXTRACTION_ATTEMPTS` (3) times**, and the buffer is **re-read on every attempt** so messages that arrive while a slow call is in flight are folded into the next attempt rather than missed. Success vs. failure is distinguished by `extract_memory` returning a value vs. `None` — an *empty* `MemoryExtraction` still counts as success (nothing was worth saving). If every attempt fails (e.g. an LLM outage), the oldest messages are trimmed anyway so the buffer stays bounded; memory is never written on a failed run.

```python
# app/services/memory_extractor.py
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service          # shared singleton
from app.services.memory_loader import build_memory_block
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT

MAX_EXTRACTION_ATTEMPTS = 3  # max extraction LLM calls per run; each re-snapshots the buffer

async def extract_and_trim(user_id: int):
    logger.info(f"Memory extraction started for user {user_id}.")
    keep_count = config.CHAT_BUFFER_TRIM
    try:
        for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
            async with db_session() as db:
                buffer_messages = await models.get_chat_buffer(db, user_id)
                if len(buffer_messages) <= keep_count:
                    return  # nothing left (a concurrent run may have trimmed it)

                trim_size = len(buffer_messages) - keep_count
                segment = buffer_messages[:trim_size]            # oldest messages
                current_memory_text, _ = await build_memory_block(db, user_id)
                instruction_prompt = (
                    f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                    f"=== CURRENT MEMORIES ===\n{current_memory_text}\n"
                )

                extraction = await llm_service.extract_memory(
                    user_id=user_id,
                    system_prompt=instruction_prompt,
                    user_history_text=_format_segment(segment),
                )
                if extraction is not None:                       # success (may be empty)
                    await models.save_extracted_memories(db, user_id, extraction)
                    await models.delete_oldest_buffer_messages(db, user_id, trim_size)
                    return
            logger.warning(f"Extraction attempt {attempt}/{MAX_EXTRACTION_ATTEMPTS} failed.")

        # Every attempt failed -> trim anyway so an outage can't grow the buffer unbounded.
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
            if len(buffer_messages) > keep_count:
                await models.delete_oldest_buffer_messages(db, user_id, len(buffer_messages) - keep_count)
    except Exception as e:
        logger.error(f"Extraction pipeline failed for user {user_id}: {e}")
```

---

## 🧹 Memory Compression (`memory_compressor.py`)

To prevent profile bloat and respect context limits, `memory_compressor.py` runs when the
compiled memory block exceeds `USER_MEMORY_BUDGET_CHARS` (default `4000`). It runs as a
background task (off the hot path) and uses the shared `llm_service` singleton.

Two correctness/efficiency properties matter here:

1. **Never wipe memory on failure.** `compress_memory` returns `None` when the LLM call fails;
   in that case the replace step is **skipped**, so existing memory is preserved.
2. **Single-pass budget enforcement.** Models can't count characters reliably, so after the LLM
   pass a deterministic enforcement drops the lowest-priority items (oldest events → beliefs →
   facts) until the block fits — computed **in memory from a single read** and persisted in
   **one write**, not a per-item read/write loop. A per-user cooldown
   (`COMPRESSION_COOLDOWN_SECS`) prevents a re-trigger loop.

```python
# app/services/memory_compressor.py
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service          # shared singleton
from app.services.memory_loader import build_memory_block
from app.prompts.compression_prompt import SYSTEM_COMPRESSION_PROMPT

async def compress_user_memory(user_id: int):
    try:
        async with db_session() as db:
            memory_text, _ = await build_memory_block(db, user_id)
            target = int(config.USER_MEMORY_BUDGET_CHARS * 0.8)
            system_prompt = (
                f"{SYSTEM_COMPRESSION_PROMPT}\n\n"
                f"TARGET CHARACTER BUDGET: {target} characters.\n"
                f"Your compressed memory profile MUST fit within {target} characters."
            )
            compression = await llm_service.compress_memory(user_id, system_prompt, memory_text)
            if compression is None:
                logger.warning(f"Compression failed for user {user_id}; keeping existing memory.")
                return                                     # never wipe on failure
            await models.replace_user_memory(db, user_id, compression)
            await _enforce_budget(db, user_id)             # single read + single write
    except Exception as e:
        logger.error(f"Compression failed for user {user_id}: {e}")
```

> The caller (`UserTaskManager.run_compressor`) enforces the per-user cooldown and acquires the
> shared `memory_lock` so compression never races the extractor.

---

## 🔒 Shared Task Concurrency Lock
Because extraction and compression are executed asynchronously, concurrency issues can arise where the extractor and compressor write or modify user memory simultaneously. 

To prevent data corruption, a unified `memory_lock = asyncio.Lock()` is initialized inside `UserState` inside the `UserTaskManager`. The manager acquires this lock before initiating both the `run_extractor` and `run_compressor` background tasks, guaranteeing sequential executions per user.

---

## 👥 Multi-Party Extraction in Groups *(Phase 9, implemented)*

In group chats the buffer is shared (`chat_id`-keyed) and each message carries `sender_id` +
`sender_name`, but **memory stays per `user_id`**. The single entry point
`extract_and_trim(chat_id)` dispatches DM vs. group with a distinct-human-sender heuristic
(`_is_group_buffer`: more than one distinct human `sender_id` among the buffered user turns ⇒
group), so no caller has to change — a DM has exactly one human sender and takes the original
single-party path unchanged.

The group path, `extract_and_trim_group(chat_id)`:

1. Reads the raw buffer (with sender attribution) and takes the segment to extract — everything
   except the most recent `CHAT_BUFFER_TRIM` messages — re-read on each of
   `MAX_EXTRACTION_ATTEMPTS` attempts so messages arriving mid-call fold into the next attempt.
2. Makes **one** `llm_service.extract_group_memory` call over the whole segment (rendered as
   `"SenderName: content"` lines), not one call per participant. It returns a
   `GroupMemoryExtraction` whose `updates` are tagged by participant **name**.
3. Maps each tagged name back to a `sender_id` using the segment's **own** normalized name→id
   map (`_build_name_id_map`). On duplicate display names, **first id wins**; names that can't be
   resolved are **skipped** rather than misattributed.
4. Saves each resolved update into that participant's profile via the same normalized, deduped
   `save_extracted_memories` CRUD, then **atomically trims** the processed segment.
5. If every attempt fails, the oldest messages are trimmed anyway (all-fail-still-trim), matching
   the DM contract, so an outage can't grow the buffer unbounded.

DMs are unchanged (a single participant). See [group_chat.md](group_chat.md) and the
`GroupMemoryExtraction` schema in [llm_integration.md](llm_integration.md).

---

## 🌙 Phase 11 — Periodic consolidation (the "dreaming" pass) *(implemented)*

Localized extraction (above) only ever sees one recent buffer window, and compression only fires
when the compiled profile is over budget. Neither can step back and look at the user's **whole**
profile over a long horizon. Phase 11 adds that long-horizon pass — a periodic background
"dreaming" step that reviews the complete profile to refresh the summary/style, merge and
de-duplicate items, and synthesize a small set of durable **behavioral insights** that only emerge
across the entire history.

It is modeled directly on compression: **one LLM call, a single-write apply, never-wipe-on-failure,
deterministic budget enforcement, and metrics**. It runs entirely off the hot path and is
**disabled by default** (see [configuration.md](configuration.md#-consolidation-phase-11)).

### Pipeline

The flow is: **scheduler → `run_consolidator` (under `memory_lock`) → one `consolidate_memory` call
→ `apply_consolidation` (single write) → `_enforce_budget`**.

1. **Scheduler** ([health.py](../../app/services/health.py)) — `start_consolidation_scheduler`
   starts a periodic loop (`_consolidation_loop`) when enabled, mirroring the Phase 10 metrics
   logger. It is:
   - **Periodic** — every `CONSOLIDATION_SCAN_INTERVAL_SECS` (default `3600`) it runs one scan.
   - **Disabled by default** — when `CONSOLIDATION_INTERVAL_SECS <= 0` the starter is a no-op and
     returns `None`, so the feature is entirely off unless explicitly enabled. `main.py` starts the
     scheduler after `init_db()`, under the same asyncio loop.
   - **Bounded per scan** — each `_run_consolidation_scan` processes at most
     `CONSOLIDATION_MAX_USERS_PER_SCAN` (default `50`) due users.
   - **Self-healing** — one user's failure is logged and skipped without aborting the scan, and any
     loop-iteration error is swallowed so the loop never crashes (it only exits on cancellation).
2. **`run_consolidator`** ([user_task_manager.py](../../app/services/user_task_manager.py)) —
   dispatches each due user under that conversation's shared `memory_lock`, so consolidation never
   races the extractor or compressor for the same id. No per-user cooldown is needed — cadence is
   governed by `last_consolidated_at` at scan time (see "due" below).
3. **`consolidate_user_memory`** ([memory_consolidator.py](../../app/services/memory_consolidator.py))
   — builds the memory block, makes **one** `consolidate_memory` LLM call, applies the result in a
   single write, then enforces the budget. It increments `consolidation.runs` on entry and
   `consolidation.success` / `consolidation.failure` on outcome; it never raises into the scheduler.
4. **`apply_consolidation`** ([models.py](../../app/database/models.py)) — a single-`$set` write
   (mirroring `replace_user_memory`) that refreshes summary/style (only when present), replaces
   facts/beliefs/events with the merged layouts, preserves the latest emotional state, writes the
   bounded `insights` list, and advances `last_consolidated_at` / `updated_at`.
5. **`_enforce_budget`** — the same deterministic, single-read/single-write enforcement reused from
   the compressor, so the consolidated profile still fits `USER_MEMORY_BUDGET_CHARS`.

```python
# app/services/memory_consolidator.py (essence)
async def consolidate_user_memory(user_id: int) -> None:
    metrics.incr("consolidation.runs")
    try:
        async with db_session() as db:
            memory_text, _ = await build_memory_block(db, user_id)
            consolidation = await llm_service.consolidate_memory(user_id, system_prompt, memory_text)
            if consolidation is None:
                metrics.incr("consolidation.failure")
                return                                  # never wipe — and don't advance the clock
            await models.apply_consolidation(db, user_id, consolidation)
            await _enforce_budget(db, user_id)          # single read + single write
            metrics.incr("consolidation.success")
    except Exception as e:
        metrics.incr("consolidation.failure")
```

### The never-wipe contract

Like compression, consolidation **never wipes memory on failure**: `consolidate_memory` returns
`None` when the LLM call fails or the JSON can't be validated. On `None` the write is **skipped**,
so existing memory is preserved. Crucially, a `None` result **also does not advance**
`last_consolidated_at` — because the clock is only set inside `apply_consolidation`, which is never
reached on failure. That means a failed run leaves the user **still due**, so the next scan retries
naturally rather than silently waiting a whole interval.

### Behavioral insights — a dedicated, bounded list

Insights are the unique value of this pass: synthesized, higher-level reads on how the user behaves
or who they are over time (e.g. "Tends to get stressed during exam season; values reassurance
then"). They are produced into a **dedicated `insights` list** on the user profile, distinct from
facts (atomic details the user shared) and beliefs (the user's own stated opinions):

- **Bounded** — `apply_consolidation` truncates to `MAX_INSIGHTS` (default `5`), and the prompt is
  also told the cap, so the list can never grow unbounded.
- **Rendered in the prompt** — `compile_memory_text`
  ([memory_loader.py](../../app/services/memory_loader.py)) emits a dedicated
  `=== BEHAVIORAL INSIGHTS ===` section (it reads `insights` defensively, showing
  `(No long-term insights yet)` when empty). `ensure_user` initializes `insights=[]` on insert.
- **Never dropped by budget enforcement** — the deterministic enforcer only sheds the
  lowest-priority items (oldest events → beliefs → facts). Insights are intentionally **not** in
  that drop order, so the hard-won long-horizon synthesis survives a tight budget.

**Why a dedicated list rather than folding insights into beliefs?** Beliefs are the user's *own*
stated convictions; an insight is *the bot's* synthesized inference about patterns. Keeping them
separate preserves that provenance distinction (so an inferred pattern is never mistaken for
something the user explicitly said), lets insights be capped and prioritized independently, and
keeps them safe from budget-driven eviction. The consolidation prompt enforces the same boundary —
it must not fold an insight into facts or beliefs.

### How "due" is determined

`find_users_due_for_consolidation` ([models.py](../../app/database/models.py)) returns up to `limit`
users that satisfy **both**:

- **Time** — `last_consolidated_at` is null/absent **OR** older than `now - CONSOLIDATION_INTERVAL_SECS`
  (this predicate runs in the Mongo query), and
- **Substance** — the user has at least `CONSOLIDATION_MIN_ITEMS` (default `8`) stored items, counted
  as `len(facts) + len(beliefs) + len(events)` (applied in Python, since array-length predicates
  aren't portable to the mongomock test backend).

Collection stops as soon as `limit` qualifying users are found, so the helper's own work is bounded.
The `CONSOLIDATION_MIN_ITEMS` floor avoids spending an LLM call "dreaming" over a profile too thin to
yield any durable pattern.

> See [configuration.md](configuration.md#-consolidation-phase-11) for every consolidation key, its
> default, and tuning guidance, and the `MemoryConsolidation` / `ConsolidatedInsight` schemas in
> [schemas.py](../../app/services/schemas.py).

---

## ⏰ Phase 12 — Temporal context & emotional continuity *(implemented)*

Phase 12 makes the bot feel less amnesiac between conversations with two small, **additive** memory
features that ride the existing pipeline — no new heavy machinery, no migration, and everything new
is read **defensively** so older profiles (written before Phase 12) render exactly as before. Both
features were designed under the same priority order as the rest of the engine:
**responsiveness → robustness → minimize LLM calls.** Neither adds an LLM call, and the hot path
gains at most **one** combined Mongo round-trip.

> The proactive check-in scheduler (the third Phase 12 engagement feature) lives off the hot path
> and is documented in [configuration.md](configuration.md#-proactive-check-ins-phase-12),
> [observability.md](observability.md#proactive-check-in-metrics-phase-12), and
> [telegram_bot.md](telegram_bot.md#-engagement-commands-phase-12-implemented).

### Temporal context — "now" and "last talked"

The model previously had no sense of *when* it was talking or how long it had been since the last
exchange. Phase 12 threads a small, optional time context into the system prompt:

- **A new `## ⏰ TIME CONTEXT` section** in [system_prompt.py](../../app/prompts/system_prompt.py).
  `build_system_prompt` gains an optional third parameter, `time_context: str = ""`, and renders the
  section **only when it is non-empty**. Existing two-argument calls (and the empty default) produce
  the prior prompt byte-for-byte, so nothing else changes.
- **`last_interaction_at`** — a new timestamp on the user profile. On the **DM hot path only**,
  `chat_manager.handle_message` records the current UTC time *and* reads the previous value in a
  **single combined round-trip** via `models.touch_and_get_last_interaction` (a `find_one_and_update`
  with `return_document=BEFORE`). It does **not** upsert — a user without a profile is a harmless
  no-op returning `None` — and it never runs on the group path (groups pass an empty `time_context`,
  so the group prompt is unchanged).
- **A coarse "last talked" gap.** A pure helper, `build_time_context(now, prev)`, renders the
  current UTC date/time plus a human gap in **coarse units** — minutes, hours, or days, never raw
  seconds. On a user's first-ever interaction (`prev is None`) it renders only the date/time and
  fabricates no gap.

Because the gap is computed from one timestamp and the section is default-empty, this adds no LLM
call and only the single combined read-then-set to the hot path.

### Emotional continuity — a bounded mood history

ThinkMate already tracked a *current* `emotional_state`, but overwrote it each time, so it could
never see a **trend**. Phase 12 keeps a short, bounded history:

- **Append on write.** Whenever `save_extracted_memories`
  ([models.py](../../app/database/models.py)) writes a new `emotional_state`, it also appends a
  matching entry (`{mood, intensity, trigger, detected_at}`) to a `mood_history` list — in the
  **same single `$set` write**, no extra round-trip. The list is bounded to `MAX_MOOD_HISTORY`
  (default `10`); once full, the oldest entry is dropped. `ensure_user` initializes
  `mood_history: []` on insert.
- **Render a trend.** Within the existing `=== CURRENT MOOD ===` block,
  `compile_memory_text` ([memory_loader.py](../../app/services/memory_loader.py)) appends a short
  oldest→newest trend line (a comma-joined list of recent mood words) after the current-mood line.
  It reads `mood_history` defensively, so a profile without one renders exactly as before — no extra
  line, no error.
- **Exempt from budget shedding.** `mood_history` is its own tiny, bounded list and is **not** part
  of the deterministic budget enforcer's drop order (oldest events → beliefs → facts). The rendered
  trend is only ever a handful of short words, so its contribution is small and capped; the enforcer
  never needs to (and never does) drop it.

### No migration, defensive reads

All new profile fields — `last_interaction_at`, `mood_history`, plus the proactive-feature fields
`onboarded`, `last_proactive_at`, and `proactive_enabled` — are **additive** and read defensively
(`doc.get("mood_history") or []`, `doc.get("last_interaction_at")`, `doc.get("proactive_enabled")`).
There is **no migration step**: a profile created before Phase 12 simply lacks the fields and is
treated as "never seen on the DM hot path / no mood history yet / eligible-but-not-yet-due," which
is exactly the desired default.

> See [configuration.md](configuration.md#-engagement--mood-history-phase-12) for `MAX_MOOD_HISTORY`
> and the proactive keys, and [telegram_bot.md](telegram_bot.md#-engagement-commands-phase-12-implemented)
> for the `/onboard`, `/pause`, and `/resume` commands.
