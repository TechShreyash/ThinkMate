# Changelog

All notable changes to the ThinkMate project will be documented in this file.

## [2026-06-14] - DM Skip Bot Commands Bugfix: Spec + Exploratory/Preservation Tests

### Added
- **Bugfix spec `dm-skip-bot-commands`**: `bugfix.md` (requirements), `design.md` (root-cause + command-guard fix design, correctness properties), and `tasks.md` (DAG task plan) under `.kiro/specs/dm-skip-bot-commands/`.
- **Bug condition exploration test**: `tests/test_command_skip.py` — scoped property test (parametrized over 18 command-like strings) asserting the DM catch-all `handle_user_message` ignores bot commands (no enqueue, no answer). Confirms the bug on unfixed code.
- **Preservation property tests**: `tests/test_command_preservation.py` — 21 tests capturing baseline non-command behavior (conversational enqueue, `MAX_INPUT_CHARS` length guard, empty-sender early return) that must remain unchanged after the fix.

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
