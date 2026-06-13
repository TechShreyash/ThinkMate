# Changelog

All notable changes to the ThinkMate project will be documented in this file.

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
