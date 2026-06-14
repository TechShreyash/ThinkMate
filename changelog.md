# Changelog

This file is the running history of notable changes to ThinkMate, the self-learning, long-term-memory Telegram AI companion. It exists so that contributors and open-source readers can see how the project grew, what shipped in each step, and why, without having to read through the commit log.

Entries are listed newest first. Each one is headed by its date and a short title naming the work it belongs to (most often a numbered development phase), and groups its details under conventional headings: **Added** for new capabilities, **Changed** or **Modified** for revisions to existing behavior, and **Fixed** for bug fixes. The version numbers, dates, file and identifier names, and the specifics of every entry below are recorded exactly as they happened.

## [2026-06-14] - Implicit Bot Addressing + Group Spam Protection: Implemented

### Added
- **No-LLM implicit-addressing gate** (`app/services/group_gate.py`) that lets the bot reply to follow-up group messages aimed at it without an explicit @mention, bounded by a recency window:
  - `ImplicitAddressGate` — per-chat in-memory state (`_bot_last_spoke`, `_human_since_bot`, `_last_implicit_reply`, `_last_seen`). `decide(...)` is a pure, never-raising predicate that classifies a message as implicit only inside both the time window (`GROUP_IMPLICIT_RECENCY_SECS`) AND the intervening-message bound (`GROUP_IMPLICIT_RECENCY_MAX_MSGS`), rejecting spam, no-bot-activity, and directed-at-other first. `cooldown_elapsed`/`mark_implicit_reply` enforce the per-chat `GROUP_IMPLICIT_COOLDOWN_SECS` so the bot replies implicitly at most once per window; `note_bot_spoke`/`note_human_message` track the window; `prune` drops idle chats.
  - **Spam protection** — `count_distinct_mentions`/`is_mass_tag_spam` (mass-tag detector, strict `>` threshold) and `SpamBurstDetector` (mention-stripped, case-folded near-duplicate greeting-burst detector using `difflib`, window + hard-cap bounded). Spam suppresses implicit classification and ambient cheap-trigger firing, while a genuine reply-to-bot still reaches the explicit path.
  - `is_directed_at_other` keeps the bot quiet when a non-addressed message replies to or @mentions another participant.
- **Router wiring** (`app/handlers/messages.py`): `_handle_group_message` classifies both spam shapes up front (each defensive), applies the spam-aware explicit decision, consults `implicit_gate.decide` before the ambient gate, and preserves the single-write buffer invariant and byte-for-byte DM behavior. `_maybe_ambient_chime` gains an `is_spam` guard. Recency commit point added in `user_task_manager.py` (`note_bot_spoke` on actual group send) with both new trackers added to the idle sweep.
- **Prompt changes**: extraction prompt now normalizes all stored facts/beliefs/events to English (translating, preserving proper nouns); system prompt strengthens reply language/script matching (Hinglish vs Devanagari, judged from recent context, independent of how memories are stored).
- **Config** (eight new env-overridable knobs): `GROUP_IMPLICIT_RECENCY_SECS`, `GROUP_IMPLICIT_RECENCY_MAX_MSGS`, `GROUP_IMPLICIT_COOLDOWN_SECS`, `GROUP_MASS_TAG_SPAM_THRESHOLD`, `GROUP_SPAM_BURST_SIMILARITY`, `GROUP_SPAM_BURST_COUNT`, `GROUP_SPAM_BURST_WINDOW_SECS`, `GROUP_SPAM_BURST_TRACK_MAX`.
- **Tests**: Hypothesis property tests (one per correctness property) plus router/helper/config/prompt example tests — `test_group_gate_helpers`, `test_group_config_smoke`, `test_group_router_implicit`, `test_prompt_language`, `test_recency_commit`, and the `test_prop_*` burst/implicit/spam suite.
- Docs: `docs/development/group_chat.md` and `configuration.md` updated for the implicit-addressing flow, spam protections, and new knobs.

## [2026-06-14] - Group User Memory + Ops (log forwarding, per-task metrics, configurable commands): Implemented

### Added
- **Per-person group memory** — group replies now compose a per-user memory block (keyed by `sender_id`) alongside the group block (keyed by `chat_id`). `build_system_prompt` gains an optional `user_memory_text` parameter that appends a distinctly-labeled per-user section after the group block, rendering byte-for-byte identical output (and an unchanged DM path) when empty. Per-user load failures degrade to group-only without raising.
- **Identity-safe model accessors** (`app/database/models.py`): `refresh_identity_if_changed` writes `username`/`display_name` only when absent or changed (never blanking populated values, never touching memory fields); `_ensure_memory_skeleton` replaces the blank-identity `ensure_user` fallback in `save_extracted_memories` so memory writes never alter identity fields. Group handler captures identity best-effort before routing; extractor resolves memory to identity-bearing user ids and skips unresolved participants without creating empty profiles.
- **Centralized log forwarding to a Telegram channel**:
  - `app/services/log_forwarder.py` — forwards the three explicit group-memory events (identity, extraction-saved, extraction-skipped) to `LOGS_CHANNEL_ID`, with source anti-recursion, a `no_forward` self-log marker, and swallow-on-failure.
  - `app/services/error_log_sink.py` — a loguru sink forwarding `WARNING`+ records to the channel via `loop.call_soon_threadsafe`, with a re-entry guard, `no_forward` skip, level guard, and full exception swallowing so it never blocks or raises into the logging call. Registered in `main.py` alongside the console/file sinks.
- **Per-task LLM metrics** (`app/services/metrics.py`): completed `_LLM_TYPE_PREFIX` for all six task types (including `memory_consolidation -> consolidation`, `proactive_checkin -> checkin`) and exported `LLM_TASK_TYPES` as the single ordered source of truth. `/metrics` now renders an "LLM calls by task" section (calls/success/failure/avg/max per task, `0` when absent).
- **Environment-configurable commands**: `resolve_command_config` reads `CMD_<KEY>_NAME`/`CMD_<KEY>_ENABLED` per built-in command, validating trigger names, falling back colliding/invalid triggers to defaults, and never raising at startup. `commands.py` converted to a `register_commands(router)` registry that binds enabled commands to resolved triggers, skips disabled ones, keeps the admin gate inside `cmd_health`/`cmd_metrics`, and renders `/help` dynamically. New `LOGS_CHANNEL_ID` and `COMMANDS` config.
- **Deployment**: added `Dockerfile`, `docker-compose.yml`, and `.dockerignore`.
- **Tests**: Hypothesis property + example tests for identity/memory separation, group extraction, prompt composition, log forwarder, error log sink, per-task metrics, and command config/registry.

## [2026-06-14] - Phase 12 Engagement & UX: Implemented

### Added
- **Four engagement features** (Phase 12 complete), all additive with safe defaults and no migration:
  - **Temporal context** — `build_system_prompt` gains an optional `time_context` rendering a `## ⏰ TIME CONTEXT` section; the DM hot path records `last_interaction_at` via a single combined `touch_and_get_last_interaction` round-trip and shows a coarse "last talked" gap (minutes/hours/days, never raw seconds, no gap on first contact). Group path unchanged (empty `time_context`).
  - **Emotional continuity** — a bounded `mood_history` list (capped at `MAX_MOOD_HISTORY`, default 10) appended in `save_extracted_memories` whenever a new `emotional_state` is written, rendered as a short trend line in the `=== CURRENT MOOD ===` block and exempt from budget shedding.
  - **Onboarding** — a static, no-LLM `/onboard` command that seeds memory and sets an `onboarded` flag; `/start` nudges `/onboard` only when not yet onboarded; `/help` lists the new commands.
  - **Proactive check-ins** — a background `start_proactive_scheduler(bot)` (mirrors the consolidation scheduler, takes the aiogram bot) that occasionally sends a one-LLM-call, memory-grounded nudge to inactive users. **Disabled by default** (`PROACTIVE_INTERVAL_SECS=0`), opt-outable (`/pause`/`/resume`), quiet-hours aware (UTC), per-user rate-limited, bounded per scan, and never fabricated/empty (`generate_checkin` returns `None` on an ungroundable profile, decline sentinel, or error; blocked users self-disable). New `app/prompts/checkin_prompt.py`.
  - Config (all optional, safe defaults; proactive OFF by default): `MAX_MOOD_HISTORY`, `PROACTIVE_INTERVAL_SECS`, `PROACTIVE_INACTIVITY_SECS`, `PROACTIVE_MIN_INTERVAL_SECS`, `PROACTIVE_MAX_PER_SCAN`, `PROACTIVE_MIN_ITEMS`, `PROACTIVE_QUIET_START_HOUR`, `PROACTIVE_QUIET_END_HOUR` — mirrored in `.env.example`/`configuration.md`.
  - Models: `touch_and_get_last_interaction`, `set_proactive_enabled`, `set_onboarded`, `set_last_proactive`, `find_users_due_for_proactive`; `ensure_user` initializes `mood_history=[]`/`onboarded=False`. LLM: `generate_checkin` (audited as `proactive_checkin`, so `llm.proactive_checkin.*` metrics appear for free). Metrics: `proactive.runs/sent/skipped/failed`.
  - **Tests** (51 new, full suite **234 passing**): `test_engagement_models`, `test_engagement_units`, `test_engagement_temporal`, `test_engagement_commands`, `test_proactive_scheduler`. All pre-existing tests pass; one brittle env-pinned assertion in `test_guards_and_compression` relaxed to a sane-floor check (honoring its own "env-tunable" comment).
- Docs: `configuration.md`, `.env.example`, `observability.md`, `memory_engine.md`, `telegram_bot.md`, `project_plan.md` (Phase 12 section + #3/#6 recorded as deferred), and `README.md` updated.

## [2026-06-14] - Phase 12 Engagement & UX: Spec

### Added
- **Engagement & UX feature spec** under `.kiro/specs/engagement/`: proactive check-ins (re-engagement scheduler, disabled by default, quiet-hours + rate-limit + opt-out, never fabricated), temporal context in the system prompt, emotional continuity (bounded `mood_history` trend), and an `/onboard` command (+ `/pause`/`/resume`). Documents #3 (relevance-ranked memory) and #6 (semantic retrieval) as future. `requirements.md` (13 EARS reqs), `design.md` (14 correctness properties), `tasks.md` (DAG, 6 waves).

## [2026-06-14] - Phase 11 Consolidation: Implemented (the "dreaming" pass)

### Added
- **Periodic memory consolidation** (Phase 11 complete) — a scheduled, off-hot-path background pass that reviews a user's whole profile to refresh the summary/style, merge & de-duplicate items, and synthesize durable behavioral **insights**. **Disabled by default** (`CONSOLIDATION_INTERVAL_SECS=0`).
  - `app/services/memory_consolidator.py`: `consolidate_user_memory` — one `consolidate_memory` LLM call, single-write apply, **never-wipe on failure** (a `None` result skips the write and does not advance `last_consolidated_at`), reuses `_enforce_budget`, increments `consolidation.runs/success/failure`.
  - `app/services/health.py`: `start_consolidation_scheduler`/`_consolidation_loop`/`_run_consolidation_scan` — periodic scan (mirrors the metrics logger), bounded to `CONSOLIDATION_MAX_USERS_PER_SCAN`, self-healing, dispatches via `run_consolidator` under `memory_lock`. Wired into `main.py`.
  - `app/database/models.py`: `find_users_due_for_consolidation` (null/old `last_consolidated_at` AND ≥ `CONSOLIDATION_MIN_ITEMS`) and `apply_consolidation` (single `$set`, insights truncated to `MAX_INSIGHTS`); `ensure_user` initializes `insights=[]`.
  - **Insights** stored in a dedicated bounded `insights` list (not folded into beliefs), rendered in `compile_memory_text`'s `=== BEHAVIORAL INSIGHTS ===` section, and never dropped by budget enforcement. New `MemoryConsolidation`/`ConsolidatedInsight` schemas, `LLMService.consolidate_memory`, and `app/prompts/consolidation_prompt.py`.
  - Config: `CONSOLIDATION_INTERVAL_SECS` (0=off), `CONSOLIDATION_SCAN_INTERVAL_SECS`, `CONSOLIDATION_MAX_USERS_PER_SCAN`, `CONSOLIDATION_MIN_ITEMS`, `MAX_INSIGHTS` (all optional, safe defaults; mirrored in `.env.example`/`configuration.md`).
  - **Tests** (25 new, full suite **183 passing**): `test_consolidation_models`, `test_consolidation_llm`, `test_consolidation_run`, `test_consolidation_scheduler`. All pre-existing tests pass unmodified.
- Docs: `memory_engine.md`, `configuration.md`, `README.md` updated; `project_plan.md` marks Phase 11 implemented — the full roadmap (Phases 0–11) is now complete.

## [2026-06-14] - Phase 11 Consolidation: Spec (the "dreaming" pass)

### Added
- **Consolidation feature spec** under `.kiro/specs/consolidation/`: a scheduled, off-hot-path background pass that periodically reviews a user's full profile to refresh the summary/style, merge/de-duplicate items, and synthesize a bounded set of durable behavioral **insights** (stored in a dedicated `insights` list, rendered in the system prompt, never dropped by budget enforcement). Disabled by default (`CONSOLIDATION_INTERVAL_SECS=0`). Reuses the metrics-logger scheduler pattern, the compressor's never-wipe contract, and `memory_lock` serialization. `requirements.md` (EARS, 9 reqs), `design.md` (9 correctness properties), `tasks.md` (DAG, 6 waves).

## [2026-06-14] - Phase 10 Observability: Implemented (metrics, health, runbook)

### Added
- **In-process metrics registry** (`app/services/metrics.py`): stdlib-only counters/gauges/timers with a `timer()` context manager, `record_llm()` helper, `snapshot()`, and `reset()`; every mutator is lock-guarded and never raises into a caller.
- **Hot-path instrumentation** (additive, non-behavioral): LLM calls + latency by type (`llm_service`), throttle drops (`middlewares`), queue drops + `conversations.active` gauge (`user_task_manager`), and extraction/compression run counts (`memory_extractor`/`memory_compressor`).
- **Health/readiness** (`app/services/health.py`): `liveness()` (no I/O, uptime + summary), async `readiness(db)` (single Mongo ping, never raises), and an optional periodic metrics logger wired into `main.py` (`METRICS_LOG_INTERVAL_SECS`, 0 = off).
- **Admin commands** `/health` and `/metrics` (`commands.py`) gated by `ADMIN_USER_IDS` with a safe DM-only default; report status/uptime/metrics + readiness with no LLM call.
- **Config**: optional `ADMIN_USER_IDS` and `METRICS_LOG_INTERVAL_SECS` (safe defaults; mirrored in `.env.example`/`configuration.md`).
- **Runbook** `docs/development/observability.md` (metric meanings, reading `llm_audit_log`, recognizing the LLM-throughput ceiling, tuning) + cross-links from README/architecture/performance_and_scaling; `project_plan.md` Phase 10 checked.
- **Tests** (33 new, full suite 158 passing): `test_metrics`, `test_metrics_instrumentation`, `test_health_and_command`, `test_metrics_logger`. All pre-existing tests pass unmodified.

## [2026-06-14] - Phase 10 Observability: Spec (Requirements + Design + Tasks)

### Added
- **Observability & ops spec** under `.kiro/specs/observability/`: `requirements.md` (EARS — in-memory metrics registry, hot-path instrumentation, health/readiness, admin `/health` command, optional periodic logger, runbook, tests), `design.md` (the fixed metric set, `metrics.py`/`health.py` interfaces, additive non-behavioral instrumentation, 9 correctness properties), and `tasks.md` (DAG plan, 7 waves). In-process, dependency-free, single-instance — not a Prometheus/OTel server.

## [2026-06-14] - Phase 9 Group Chat: Implemented (ambient gate, affinity, multi-party memory)

### Added
- **Group chat support** (Phase 9 complete). In groups/supergroups ThinkMate always replies when addressed (mention, bot-name token, or reply-to-bot) and otherwise runs a **no-LLM ambient gate** (`app/services/group_gate.py`: `AmbientGate` — per-chat cooldown → cheap trigger/scan-tick → affinity-weighted dice roll, `decide()` exposes the drop stage for observability), so it chimes in selectively at ≤ ~1 ambient LLM call per active group per cooldown window.
- **Per-(chat, user) affinity** in a new `chat_members` collection (`_id="{chat_id}:{user_id}"`, affinity 0–1, mode auto/quiet/chatty), fronted by an in-memory read-through/write-through `AffinityCache` (`app/services/affinity.py`). Signals: mention/reply-to-bot up, "back off" keywords down, an optional `affinity_delta` folded from the reply call, and explicit `/quiet` `/chatty` commands.
- **Multi-party memory extraction** (`extract_and_trim_group`): one LLM call over the group segment, attributed back to each participant via the segment's name→id map (first-id-wins on duplicate names; unresolved names skipped), saved per `user_id`. DM extraction unchanged.
- **Group schemas/LLM**: `ReplyBundle.affinity_delta`, `GroupMemoryUpdate`/`GroupMemoryExtraction`; `generate_reply_bundle(..., with_affinity=True)` and `extract_group_memory`.
- **Tests** (50 new, full suite 125 passing): `test_group_models`, `test_group_plumbing`, `test_group_routing`, `test_ambient_gate`, `test_affinity_and_commands`, `test_group_extraction`, `test_group_config_observability`.

### Changed
- **Buffer keyed by `chat_id`** with `sender_id`/`sender_name` per message (DM `chat_id == user_id`, on-disk shape compatible). `handle_message`/`enqueue_message` gained keyword-only chat context with DM-safe defaults; `messages.py` now branches private/channel/group with addressed detection and a single-write buffer invariant. **DM behavior is byte-for-byte unchanged** and all pre-existing tests pass unmodified.
- **Docs** synced to "implemented": `group_chat.md`, `database.md`, `telegram_bot.md`, `memory_engine.md`, `llm_integration.md`, `architecture.md`, `testing_guide.md`, `README.md`, and `project_plan.md` (Phase 9 checked).

## [2026-06-14] - Phase 9 Group Chat: Spec (Requirements + Design + Tasks)

### Added
- **Group-chat feature spec** under `.kiro/specs/group-chat/`: `requirements.md` (EARS criteria across DM preservation, group routing/identity, ambient gate, affinity, multi-party extraction, commands, config/observability), `design.md` (additive-plumbing architecture, `chat_members` data model, augmented buffer messages, the no-LLM ambient-gate funnel, 8 correctness properties, testing strategy), and `tasks.md` (DAG plan, 8 waves). Grounded in `docs/development/group_chat.md` and the actual current code. Top constraint: DMs remain byte-for-byte identical (`chat_id == user_id`).

### Changed (Phase 9 implementation — waves 1-2, DM behavior preserved)
- **Schemas** (`app/services/schemas.py`): `ReplyBundle` gains optional `affinity_delta`; new `GroupMemoryUpdate`/`GroupMemoryExtraction` for name-tagged multi-party extraction.
- **Buffer + chat_members** (`app/database/models.py`): `add_message_to_buffer` is now keyed by `chat_id` and stores `sender_id`/`sender_name` per message (defaults to `chat_id` in DMs, so DM docs stay compatible); new `get_chat_member`/`upsert_chat_member` CRUD over a `chat_members` collection (`_id="{chat_id}:{user_id}"`, affinity clamped to [0,1], mode validated).
- **Group gate** (`app/services/group_gate.py`, new): pure no-LLM helpers `is_addressed`, `scan_cheap_triggers`, `scan_negative_signal`, plus the `AmbientGate` funnel (cooldown → scan-tick → affinity dice → prune).
- **Affinity cache** (`app/services/affinity.py`, new): read-through/write-through in-memory cache over `chat_members` with idle pruning.
- **Orchestrator** (`app/services/chat_manager.py`): `handle_message` gains keyword-only chat context (`chat_type`, `sender_id`, `sender_name`, `reason`, `participants`) with DM-safe defaults; groups render multi-party history and obtain `affinity_delta`. `generate_reply_bundle` gains `with_affinity` (DM contract unchanged).
- **Tests**: `tests/test_group_models.py` (11) for buffer attribution + chat_members. Existing DM suite unchanged and green.

## [2026-06-14] - DM Skip Bot Commands Bugfix: Spec + Exploratory/Preservation Tests

### Added
- **Bugfix spec `dm-skip-bot-commands`**: `bugfix.md` (requirements), `design.md` (root-cause + command-guard fix design, correctness properties), and `tasks.md` (DAG task plan) under `.kiro/specs/dm-skip-bot-commands/`.
- **Bug condition exploration test**: `tests/test_command_skip.py` — scoped property test (parametrized over 18 command-like strings) asserting the DM catch-all `handle_user_message` ignores bot commands (no enqueue, no answer). Confirms the bug on unfixed code.
- **Preservation property tests**: `tests/test_command_preservation.py` — 21 tests capturing baseline non-command behavior (conversational enqueue, `MAX_INPUT_CHARS` length guard, empty-sender early return) that must remain unchanged after the fix.

### Fixed
- **DM catch-all no longer treats bot commands as conversation** (`app/handlers/messages.py`): added a command guard to `handle_user_message`. A message is treated as a command when its text starts with `/` OR a `bot_command` entity sits at offset 0, and is then ignored (no LLM reply, no enqueue to the memory pipeline). Unregistered slash commands like `/foo` previously fell through the `@router.message(F.text)` catch-all and were answered + saved to memory. The empty-sender guard, length guard, and conversational enqueue path are unchanged; text like `2/3` is still treated as conversation. Verified by 18 command-skip + 21 preservation tests (39 passing).
- **Docs corrected** (`docs/development/group_chat.md`): the "Behavior by chat type" Private (DM) row now states bot/slash commands are excluded from conversation, with a clarifying note that registered commands are handled by their handlers and unregistered slash commands are ignored (no reply, no enqueue). Full suite: 75 passing.

## [2026-06-14] - Documentation Overhaul: Unified Build Path, Performance/Scaling, Group-Chat Integration

### Added
- **Performance & scaling reference**: new `docs/development/performance_and_scaling.md` — hot-path invariants, per-batch cost model, efficiency do/don't rules, DB access patterns & indexes, bounded-memory table, the single-instance LLM-throughput ceiling, and a mechanical horizontal-scale migration path (StateStore → Redis, webhooks, Mongo sharding).
- **Group-chat config knobs documented**: `GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`, `GROUP_CONTEXT_SCAN_EVERY`, `AFFINITY_DEFAULT`, plus `ENABLE_MESSAGE_REACTIONS` and a connection-pool note, in `configuration.md` (fixes the broken cross-reference from `group_chat.md`).
- **`chat_members` collection + `chat_id`-keyed buffers** documented in `database.md` (sender attribution for multi-party group context).
- **pyproject hygiene**: real metadata, runtime deps mirroring `requirements.txt`, `requires-python >=3.12`, and `[tool.pytest.ini_options]` (`pythonpath`, `asyncio_mode`) so `uv run pytest` works directly.

### Modified
- **`project_plan.md` rewritten** as a single start-to-end build path (Phases 0–12) covering foundations → data → LLM → memory → orchestrator → Telegram → guards → hardening → tests → group chat → observability → future consolidation & horizontal scale, each with goals, files, design points, and acceptance criteria.
- **Stale code snippets corrected** to match the hardened implementation: `database.md` (atomic buffer ops, normalized/deduped single-write CRUD, current `connection.py`), `memory_engine.md` (shared `llm_service` singleton, compression-failure skip, single-pass budget enforcement, multi-party extraction), `llm_integration.md` (`extract_memory`/`compress_memory` return `None` on failure; group `affinity_delta`).
- **Group chat woven into the unified docs**: routing + `/quiet` `/chatty` in `telegram_bot.md`, a Group Chat section in `architecture.md`, a status banner in `group_chat.md`, and planned `test_group_chat.py` in `testing_guide.md`.
- **Factual drift fixed**: `setup_guide.md` (`MAX_INPUT_CHARS`/`MAX_RESPONSE_CHARS` 1000→2500/2000, Python 3.12), `README.md` (removed phantom `app/utils/`, updated docs index, group-chat & load features, Python 3.12), `configuration.md` (`LLM_EXTRACTION_MODEL` default is blank → reuses `LLM_MODEL`).
- **`hardening_plan.md`**: added Phase H (efficiency/resilience follow-ups) and a Phase 12 scale-out pointer.

## [2026-06-14] - Resilient Memory Extraction (Retry + Bounded-Buffer Trim)

### Added
- **Extraction retry loop**: `extract_and_trim()` in `memory_extractor.py` now retries the extraction LLM call up to `MAX_EXTRACTION_ATTEMPTS` (3) times. Each attempt **re-reads the buffer**, so messages that arrive while a slow call is in flight are folded into the next attempt instead of being missed.
- **Bounded-buffer guarantee on outage**: if all attempts fail, the oldest messages are trimmed anyway so the buffer can't grow without bound during an LLM outage (a deliberate trade — un-extracted memory is dropped rather than accumulating indefinitely).
- **Regression tests**: `test_extraction_retries_and_folds_in_new_messages` and `test_extraction_all_attempts_fail_still_trims` in `tests/test_hardening.py`.

### Modified
- **Failure signaling**: `LLMService.extract_memory()` now returns `MemoryExtraction | None` — `None` on a failed call (so the caller can retry), a valid (possibly empty) model on success. Previously a failed call was silently coalesced into an empty result and the buffer was trimmed regardless, permanently dropping the un-extracted segment on a transient outage.
- **Docs**: synchronized `architecture.md`, `memory_engine.md` (extractor snippet updated to the retry flow and the shared `llm_service` singleton), and `llm_integration.md`.

## [2026-06-12] - Optimize Message Processing, Queue Batching, Rate Limiting & Concurrency Locks

### Added
- **User Task Manager**: Added `app/services/user_task_manager.py` to manage per-user message batching delay, serialization locks, queue limits, and Telegram typing loops.
- **Throttling Middleware**: Implemented `ThrottlingMiddleware` in `app/handlers/middlewares.py` and registered it in `main.py` to drop spammers before opening SQLite sessions or starting handlers.
- **Hard Delay Deadline**: Added `MAX_BATCH_DELAY_SECS` configuration to prevent spammers from postponing response generations indefinitely.
- **Anti-Spam Queue Guard**: Implemented `MAX_QUEUED_MESSAGES` to drop messages if the queue exceeds the limit.
- **Integration Tests**: Added `tests/test_batching_and_concurrency.py` verifying rate limiting, batch delays, locks, and triggers.

### Modified
- **Character-Count Trigger**: Changed memory extraction to trigger based on total character count (`CHAT_BUFFER_MAX_CHARS`) instead of message count.
- **Queue Segment Extraction**: Updated `extract_and_trim()` in `memory_extractor.py` to extract from all buffer messages except the latest `CHAT_BUFFER_TRIM`.
- **Config and Environment**: Integrated the new batching and rate limiting variables in `app/config.py`, `.env`, and `.env.example`.
- **Relative Path Resolution**: Replaced all absolute local links (`file:///d:/ThinkMate/`) with relative repository paths across all markdown documentation files to ensure clean GitHub rendering.

## [2026-06-12] - Implement Character-Budget Memory Compression & Input/Output Guards

### Added
- **Background Memory Compression**: Spawns a non-blocking `compress_user_memory()` background task to consolidate user memory when size exceeds limit.
- **Compression Prompt & Schemas**: Added `app/prompts/compression_prompt.py` and `MemoryCompression` schemas in `schemas.py` to route profile summary, style, facts, events, and emotional log.
- **Character Budget Config**: Added `USER_MEMORY_BUDGET_CHARS` (default 10,000) and `CHARS_PER_TOKEN` (default 4) configurations.
- **Input Guard**: Ignores incoming user messages exceeding `MAX_INPUT_CHARS` (default 1,000 chars) with a friendly deflection reply.
- **Output Guard**: Derives `max_tokens` based on `MAX_RESPONSE_CHARS` (default 1,000 chars) to restrict LLM response lengths at the API level.
- **Commit Rules Agent Guideline**: Created `.agents/rules/commit_rules.md` to automatically update changelog on commits.
- **Automated Tests**: Created `tests/test_guards_and_compression.py` covering loader, guards, and DB memory replacements.

### Modified
- **Database Models**: Implemented transactional memory replacement in `models.py` (`replace_user_memory()`).
- **Commands Handler**: Updated `/profile` command in `commands.py` to output the consolidated 4-part user card.
- **Message Router**: Updated `messages.py` to run input guard validation and invoke chat manager orchestrator.
- **Persona Guidelines**: Hardened anti-abuse boundaries (disallowing code generation, structured outputs, essay writing), added length limits, and prompt injection filters in `persona.md`.
- **System Documentation**: Synchronized the new configurations and structures in `architecture.md`, `memory_engine.md`, `setup_guide.md`, `database.md`, `llm_integration.md`, `telegram_bot.md`, `project_plan.md`, and `README.md`.
