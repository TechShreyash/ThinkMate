# Project Implementation Plan & Build Path

The single, authoritative, step-by-step path to build ThinkMate from an empty directory to the
full feature set — optimized for large load on a single instance. Each phase lists its **goal**,
the **files** it produces, the **key design points** (with links to the deep-dive doc), and the
**acceptance criteria** that prove it's done. Build phases in order; later phases assume earlier
ones exist.

> Companion docs: [architecture.md](architecture.md) ·
> [performance_and_scaling.md](development/performance_and_scaling.md) ·
> [database.md](development/database.md) · [llm_integration.md](development/llm_integration.md) ·
> [memory_engine.md](development/memory_engine.md) · [telegram_bot.md](development/telegram_bot.md) ·
> [group_chat.md](development/group_chat.md) · [configuration.md](development/configuration.md) ·
> [testing_guide.md](development/testing_guide.md) · [hardening_plan.md](development/hardening_plan.md)

---

## 🗺️ Roadmap

```
Phase 0  Foundations ............. project skeleton, config, logging, deps
Phase 1  Data layer .............. MongoDB connection, schema, indexes, atomic CRUD
Phase 2  LLM service ............. one client, json_object outputs, retries, audit
Phase 3  Memory engine .......... loader, extractor (retry), compressor (budget), prompts
Phase 4  Orchestrator ........... chat_manager: buffer -> memory -> one reply call
Phase 5  Telegram (DM) .......... entrypoint, middlewares, commands, message router, reactions
Phase 6  Guards & concurrency ... throttle, batching, queues, locks, input/output guards
Phase 7  Hardening & efficiency . bounded memory, atomic ops, single-pass budget, audit TTL
Phase 8  Tests .................. mongomock suite; hot-path, race, retry, guard regressions
Phase 9  Group chat ............. chat_id buffers, ambient gate, affinity, multi-party memory
Phase 10 Observability & ops .... metrics, health checks, runbook
Phase 11 Consolidation .......... periodic "dreaming" pass
```

Phases 0–8 deliver a production DM bot. Phase 9 adds group chat. Phase 10 adds the
observability/ops layer and Phase 11 the periodic consolidation pass. The exact efficiency
invariants every phase must respect live in
[performance_and_scaling.md](development/performance_and_scaling.md).

---

## Phase 0 — Foundations

**Goal:** a runnable, typed, logged skeleton with dependencies pinned.

**Files:** `pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore`, `app/__init__.py`,
`app/config.py`, `persona.md`, the `app/` package tree.

**Key design points**
- `pyproject.toml` declares real metadata and `requires-python` matching the runtime; runtime
  deps are listed there and/or in `requirements.txt` (aiogram, motor, openai, pydantic,
  python-dotenv, loguru, mongomock, pytest, pytest-asyncio). The two must not contradict.
- `config.py` is a single typed `Config` (Pydantic) loaded from env with per-field parsers and
  sane defaults; expose one importable `config` instance. Document every key in
  [configuration.md](development/configuration.md) and mirror it in `.env.example`.
- `app/__init__.py` configures `loguru` (stdout + rotating file under `logs/`, which is git-ignored).

**Acceptance**
- `uv run python -c "from app.config import config; print(config.MODEL_DUMP())"` style import works.
- `.env.example` and `configuration.md` list the *same* keys/defaults as `config.py`.

---

## Phase 1 — Data layer (MongoDB)

**Goal:** async connection singleton, indexes, and atomic per-user CRUD.

**Files:** `app/database/connection.py`, `app/database/models.py`, `app/database/__init__.py`.

**Key design points** (see [database.md](development/database.md))
- One lazy `AsyncIOMotorClient` singleton with `serverSelectionTimeoutMS`; `get_db()`,
  `db_session()` context manager, `ping_db()` (fail fast at startup), `init_db()` (indexes).
- Collections: `user_profiles` (`_id=user_id`), `chat_buffers` (`_id=chat_id`),
  `chat_members` (`_id="{chat_id}:{user_id}"`, Phase 9), `llm_audit_log` (compound + TTL index).
- `add_message_to_buffer` uses `find_one_and_update` with `$push`+`$slice` (hard cap) and
  returns the post-update array (char count + history in one round-trip). Timestamps via a
  strictly-monotonic ms clock.
- `delete_oldest_buffer_messages` trims **atomically** via `$pull` on a `created_at` cutoff
  (never read-slice-overwrite).
- `save_extracted_memories` / `replace_user_memory`: load arrays once, mutate in memory
  (normalized casefold/whitespace matching + dedup, hard deletes), write once with `$set`.

**Acceptance**
- CRUD unit tests pass on mongomock; trim preserves concurrently-appended messages; buffer
  never exceeds the hard cap.

---

## Phase 2 — LLM service & audit

**Goal:** one shared client, robust structured outputs, minimal calls, safe audit.

**Files:** `app/services/llm_service.py`, `app/services/schemas.py`, `app/services/reactions.py`.

**Key design points** (see [llm_integration.md](development/llm_integration.md))
- A single shared `LLMService` instance (`llm_service`) — one client/pool for the process.
- `LLM_STRUCTURED_MODE`: `json_object` default (schema appended to prompt + Pydantic validate),
  `native_parse` opt-in for true OpenAI. Never use the dead native-parse round-trip on proxies.
- `_with_retries`: bounded exponential backoff on transient errors only (timeout, connection,
  429, 5xx); 4xx not retried.
- `generate_reply_bundle` → `(reply, reaction)` in **one** `json_object` call; graceful
  fallback to plain reply on bad JSON. In groups it also returns an optional `affinity_delta`.
- `extract_memory` returns `MemoryExtraction | None` (`None` = failed, so the caller can retry;
  empty = success/nothing to save). `compress_memory` returns `MemoryCompression | None` (`None`
  = failed → caller skips the replace, so memory is never wiped).
- Audit via `_fire_log` (fire-and-forget), `datetime` timestamps (TTL-able), field truncation.

**Acceptance**
- Reply bundle parses reply+reaction; bad JSON degrades to plain reply. Failed structured calls
  return `None` and never raise into the caller. Audit writes don't block.

---

## Phase 3 — Memory engine

**Goal:** compile memory for prompts; extract on overflow; compress to budget.

**Files:** `app/services/memory_loader.py`, `app/services/memory_extractor.py`,
`app/services/memory_compressor.py`, `app/prompts/{system,extraction,compression}_prompt.py`.

**Key design points** (see [memory_engine.md](development/memory_engine.md))
- `build_memory_block` compiles profile/facts/beliefs/events/mood into one text block **and**
  returns a `needs_compression` flag — built once, used for both prompt and budget check.
- `extract_and_trim`: retry up to 3 times, **re-reading the buffer each attempt** (mid-call
  arrivals are folded in); save+trim on success; on total failure trim anyway to bound the
  buffer. Uses the shared `llm_service` singleton.
- `compress_user_memory`: LLM condenses to ~80% of budget; on failure (`None`) skip the
  replace. Budget enforcement is a **single-read, in-memory** drop of lowest-priority items
  (oldest events → beliefs → facts) then **one** write — not a per-item DB loop. Per-user
  cooldown prevents re-trigger loops.

**Acceptance**
- Extraction retries and includes mid-call messages; all-fail still trims; compression failure
  preserves existing memory; profile ends ≤ budget after enforcement in one write.

---

## Phase 4 — Orchestrator (`chat_manager`)

**Goal:** the hot path — one reply call, ≤3 round-trips, no inline heavy work.

**Files:** `app/services/chat_manager.py`.

**Key design points** (see [architecture.md](architecture.md) and the hot-path invariants in
[performance_and_scaling.md](development/performance_and_scaling.md))
- `handle_message(db, chat_id/user_id, text) -> (reply, reaction)`: append user msg (returns
  array) → if overflow, trigger background extraction → build memory block (persona from mtime
  cache) → `generate_reply_bundle` → append assistant reply → if over budget, trigger
  background compression.

**Acceptance**
- Exactly one chat LLM call and ≤3 Mongo round-trips per message; persona not re-read unless
  its mtime changed; extraction/compression only *triggered*, never awaited inline.

---

## Phase 5 — Telegram layer (DM)

**Goal:** wire aiogram with DI middleware, commands, message routing, reactions.

**Files:** `main.py`, `app/handlers/{__init__,middlewares,commands,messages}.py`.

**Key design points** (see [telegram_bot.md](development/telegram_bot.md))
- `main.py`: ping Mongo (fail fast), init indexes, register **outer** middlewares (throttle
  then DB session), include routers, start long-polling.
- `DbSessionMiddleware` injects the shared `db`. Typing is owned by `UserTaskManager`, not a
  middleware.
- Commands: `/start`, `/help`, `/profile`, `/reset` (confirm-gated). Group commands `/quiet`,
  `/chatty` arrive in Phase 9.
- `messages.py`: ignore senderless posts, enforce `MAX_INPUT_CHARS`, enqueue for batching.
- Reaction (already normalized) is applied to the user's message; failures never block delivery.

**Acceptance**
- Bot starts, `/start` upserts a profile, a chat returns a reply (and optional reaction),
  oversized inputs are deflected.

---

## Phase 6 — Guards & concurrency

**Goal:** protect the instance and serialize per-user work.

**Files:** `app/services/user_task_manager.py`, `app/handlers/middlewares.py` (throttle).

**Key design points** (see [telegram_bot.md](development/telegram_bot.md), perf doc)
- `UserTaskManager`: per-user `UserState` (chat_lock, memory_lock, queue, batch/typing tasks),
  message coalescing with `MESSAGE_BATCH_DELAY_SECS` and a hard `MAX_BATCH_DELAY_SECS` deadline,
  `MAX_QUEUED_MESSAGES` cap, and a typing loop spanning batch+generation.
- `ThrottlingMiddleware`: per-user sliding-window limiter applied **before** any DB session;
  in-memory map self-prunes.

**Acceptance**
- Rapid messages coalesce into one reply; deadline forces processing under a flood; queue caps;
  throttle drops excess before DB/LLM work; background tasks serialize via `memory_lock`.

---

## Phase 7 — Hardening & efficiency

**Goal:** make every structure bounded and every routine single-pass. (See
[hardening_plan.md](development/hardening_plan.md) for the itemized checklist and rationale.)

**Key design points**
- Bounded memory: idle `UserState` eviction (`USER_STATE_TTL_SECS`), throttle-map pruning,
  persona mtime cache, buffer `$slice` cap, audit TTL.
- Fewer/robust LLM calls: merged reply+reaction, retries with backoff, no dead native-parse.
- Atomic buffer trim; normalized dedup; single-pass deterministic budget enforcement;
  compression-failure safety; extraction retry + bounded-trim.
- Audit off the hot path with `datetime` timestamps + TTL; startup Mongo ping.

**Acceptance**
- Memory stays flat under a synthetic 50k-user soak (only the active working set resident);
  zero deprecation warnings; all hot-path invariants from the perf doc hold.

---

## Phase 8 — Tests

**Goal:** fast, hermetic coverage with mongomock (never the production cluster).

**Files:** `tests/conftest.py`, `tests/test_*.py`, `tests/run_llm_live.py` (manual only).

**Key design points** (see [testing_guide.md](development/testing_guide.md))
- Async mongomock wrappers in `conftest.py`; autouse fixtures patch the DB and disable reactions.
- Cover: CRUD + hard deletes, atomic trim race, buffer cap, normalized dedup, build-memory +
  compression flag, single-pass budget enforcement, compression-failure no-wipe, extraction
  retry + mid-call fold-in + all-fail trim, batching/coalescing, deadline, queue cap, throttle,
  memory-lock serialization, reply+reaction parsing/normalization.

**Acceptance**
- `uv run python -m pytest` green; no warnings; no external services required.

---

## Phase 9 — Group chat, ambient replies & affinity

**Goal:** behave well in groups without spamming or abusing the LLM. (Full design in
[group_chat.md](development/group_chat.md).)

**Files:** group routing + identity helpers in handlers, `chat_members` CRUD in `models.py`,
ambient-gate logic in `user_task_manager.py`/a new `group_gate.py`, `/quiet` `/chatty` commands.

**Key design points**
- Buffers keyed by `chat_id` (DM: `chat_id==user_id`, unchanged); each buffered message carries
  `sender_id`+`sender_name` for multi-party context.
- Reply when addressed (mention / name / reply-to-bot); otherwise run the **ambient gate**:
  per-chat cooldown → cheap keyword scan (no LLM) → affinity-weighted dice roll → ≤1 LLM call.
- Memory stays per `user_id`; group extraction is multi-party (one call, updates tagged by
  participant, mapped back via the segment's name→id map).
- Affinity in `chat_members` (read-through cache); signals: mentions/engagement up, "stop/quiet"
  keywords down, plus `affinity_delta` piggybacked on the reply JSON. `/quiet` and `/chatty`
  set mode.

**Acceptance**
- DMs unchanged; groups reply when addressed; ambient chime-ins respect cooldown/affinity and
  cost ≤ ~1 LLM call per active group per window; multi-party extraction attributes memory
  correctly; `quiet` suppresses ambient.

---

## Phase 10 — Observability & ops

**Goal:** make the running instance measurable and operable.

**Key design points**
- Metrics: LLM call counts/latency, hot-path round-trips, queue depth, active `UserState`
  count, throttle drops, extraction/compression runs, audit write lag. Expose via logs and/or a
  metrics endpoint (Prometheus/OTel).
- Health: startup ping already fails fast; add a lightweight liveness signal.
- Runbook: how to read `llm_audit_log`, tune budgets/batching, and recognize the LLM ceiling.

**Acceptance**
- Operators can answer "are we near the ceiling?" from metrics; audit queries use the compound
  index.

**Status: implemented.** Delivered as a dependency-free, in-process layer — an in-memory metrics
registry (`app/services/metrics.py`), additive hot-path instrumentation, liveness/readiness
helpers (`app/services/health.py`), an admin `/health` (and `/metrics`) command gated by
`ADMIN_USER_IDS` (DM-only default), and an optional periodic logger (`METRICS_LOG_INTERVAL_SECS`).
Full metric catalog and runbook in [observability.md](development/observability.md). An external
Prometheus/OTel sink can be added later if a metrics backend is introduced.

---

## Phase 11 — Periodic consolidation ("dreaming")

A scheduled background pass that reviews facts/beliefs/events across the user's whole profile to
synthesize behavioral trends and durable profile insights — beyond what localized per-overflow
extraction can see. Runs under `memory_lock`, fully off the hot path, and is disabled by default.

**Status: implemented.** Delivered as a periodic scheduler (`start_consolidation_scheduler` in
`app/services/health.py`, started from `main.py` after `init_db()`) that finds due users
(`find_users_due_for_consolidation`) and dispatches each through `run_consolidator` under the shared
`memory_lock`. `consolidate_user_memory` (`app/services/memory_consolidator.py`) makes **one**
`consolidate_memory` LLM call, applies the result in a single write (`apply_consolidation`), and
reuses the deterministic budget enforcer — never wiping memory on failure and not advancing
`last_consolidated_at` when a run fails. Synthesized **behavioral insights** live in a dedicated,
bounded `insights` list (capped at `MAX_INSIGHTS`), rendered in the
`=== BEHAVIORAL INSIGHTS ===` section and never dropped by budget enforcement. Disabled by default
(`CONSOLIDATION_INTERVAL_SECS=0`). Full design in
[memory_engine.md](development/memory_engine.md#-phase-11--periodic-consolidation-the-dreaming-pass-implemented);
keys in [configuration.md](development/configuration.md#-consolidation-phase-11).

---

## Build order checklist

- [x] Phase 0 Foundations
- [x] Phase 1 Data layer
- [x] Phase 2 LLM service & audit
- [x] Phase 3 Memory engine
- [x] Phase 4 Orchestrator
- [x] Phase 5 Telegram (DM)
- [x] Phase 6 Guards & concurrency
- [x] Phase 7 Hardening & efficiency
- [x] Phase 8 Tests
- [x] Phase 9 Group chat
- [x] Phase 10 Observability & ops
- [x] Phase 11 Consolidation

> Note: the current repository now implements Phases 0–11 — the full roadmap (DM bot, hardened,
> group chat, the observability/ops layer, and the periodic consolidation "dreaming" pass). There
> is no remaining forward-looking phase. This plan is written so the project could also be rebuilt
> cleanly from scratch in this exact order.
