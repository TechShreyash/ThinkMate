# Requirements Document

Phase 11: Periodic memory consolidation (the "dreaming" pass).

## Introduction

ThinkMate's memory engine (Phase 3) extracts facts/beliefs/events from a single conversation segment whenever the chat buffer overflows, and compresses a user's profile when it exceeds `USER_MEMORY_BUDGET_CHARS`. Both passes are **localized**: they only ever see one window of recent activity. They cannot notice that a user gets anxious every exam season, slowly shifted careers over months, or consistently values reassurance after setbacks — patterns that only emerge when you look across the **whole** profile over a long horizon.

Phase 11 adds a scheduled background **consolidation** pass — the "dreaming" pass — that periodically reviews a user's complete profile (facts, beliefs, events, summary, style) across a long window to (a) refresh the profile summary and communication style, (b) merge and de-duplicate accumulated items, and (c) **synthesize a small, bounded set of durable, higher-level behavioral/identity insights** beyond what localized extraction can produce. It runs **fully off the hot path**, serialized under the existing per-user `memory_lock` (never racing the extractor or compressor), is rate-limited per user, does bounded work per wake, and — crucially — is **disabled by default** so nothing changes unless an operator opts in.

This mirrors the existing background-task and safety patterns already in the codebase:
- The **scheduler** follows `app/services/health.py::start_metrics_logger` — a single background loop started from `main.py` after `init_db()`, a no-op when its interval ≤ 0, self-healing on errors.
- The **per-user run** follows `app/services/memory_compressor.py::compress_user_memory` — one LLM call, a single-write apply via a `models` helper, the **never-wipe-on-failure** contract (a `None` result skips the write), deterministic budget enforcement afterward, and `metrics.incr` for observability.
- The **serialization** follows `app/services/user_task_manager.py::run_compressor` — a new `run_consolidator` that takes the per-conversation `memory_lock`.
- The **LLM call** follows `app/services/llm_service.py::compress_memory` / `_structured_call` — a new `consolidate_memory` structured call returning `MemoryConsolidation | None`.
- The **schema** follows `MemoryCompression` in `app/services/schemas.py` — a new `MemoryConsolidation`.
- The **config knobs** follow `app/config.py` `_env_*` helpers — all optional with safe defaults (`0`/disabled), introducing no new *required* config.

## Glossary

- **Consolidation run**: a single per-user pass that reads the full profile, makes one `consolidate_memory` LLM call, and applies a refreshed/merged profile plus synthesized insights in one write.
- **Scheduler**: the single background loop that periodically scans `user_profiles` for users *due* for consolidation and dispatches bounded work.
- **Due**: a user whose `last_consolidated_at` is older than `CONSOLIDATION_INTERVAL_SECS` (or is null/absent) **and** who has at least `CONSOLIDATION_MIN_ITEMS` total facts+beliefs+events.
- **Insight**: a durable, higher-level behavioral or identity observation synthesized across the whole history (e.g. "Tends to get stressed during exam season; values reassurance then"), distinct from a raw fact (atomic detail) or a belief (the user's own stated opinion/value).
- **`memory_lock`**: the per-conversation `asyncio.Lock` in `UserState` that serializes all background memory work (extractor, compressor, and now consolidator) so they never run concurrently for the same id.
- **Hot path**: everything between "a user's batch is ready" and "the reply is sent". Consolidation must add nothing to it.
- **Never-wipe contract**: a failed LLM call (returns `None`) must skip the memory-replacing write so existing memory is preserved (mirrors the compressor).

---

## Requirements

### Requirement 1: Consolidation scheduler (single background loop)

**User Story:** As an operator, I want a single scheduled background task that periodically finds users due for consolidation and processes a bounded number of them, so that long-horizon memory synthesis happens automatically without ever touching the hot path or overwhelming the LLM.

#### Acceptance Criteria

1.1 WHERE the scheduler lives THE SYSTEM SHALL provide a `start_consolidation_scheduler()` function (in `app/services/health.py`, alongside `start_metrics_logger`) that starts at most one background task and returns the task, mirroring the existing periodic-logger pattern.

1.2 WHEN `CONSOLIDATION_INTERVAL_SECS <= 0` THEN the system SHALL NOT start the scheduler task and SHALL return `None`, so the feature is a harmless no-op when disabled (the default).

1.3 WHEN the scheduler is enabled THEN it SHALL wake every `CONSOLIDATION_SCAN_INTERVAL_SECS` seconds and query `user_profiles` for users who are *due*: `last_consolidated_at` older than `CONSOLIDATION_INTERVAL_SECS` (or null/absent) AND total facts+beliefs+events ≥ `CONSOLIDATION_MIN_ITEMS`.

1.4 WHEN a scan finds due users THEN the system SHALL process at most `CONSOLIDATION_MAX_USERS_PER_SCAN` of them per wake, so each wake performs bounded work.

1.5 WHEN the scheduler dispatches a user THEN it SHALL do so via `UserTaskManager.run_consolidator(user_id)` so the run is serialized under that user's `memory_lock` (never racing the extractor/compressor).

1.6 WHEN a single user's consolidation raises THEN the scheduler SHALL log it and continue with the remaining due users (one failure must not abort the scan).

1.7 WHEN any scan iteration encounters an unexpected error THEN the scheduler loop SHALL log and continue on its next interval and SHALL NEVER crash the process (self-healing, mirroring `_metrics_logger_loop`).

1.8 WHEN the scheduler is enabled THEN `main.py` SHALL start it after `init_db()` and log that it started; WHEN disabled it SHALL start nothing and require no configuration.

1.9 WHERE the due-user query runs THE SYSTEM SHALL bound its own work (e.g. stop collecting once `CONSOLIDATION_MAX_USERS_PER_SCAN` qualifying users are found) rather than loading every profile into memory.

---

### Requirement 2: Per-user consolidation run (off the hot path, never-wipe)

**User Story:** As a user, I want my profile periodically reviewed as a whole, so that durable patterns are captured and my memory stays coherent — without ever losing existing memory if the pass fails.

#### Acceptance Criteria

2.1 WHERE the run lives THE SYSTEM SHALL provide `consolidate_user_memory(user_id)` (a new `app/services/memory_consolidator.py`) that opens its own `db_session()` and performs the full pass, mirroring `compress_user_memory`.

2.2 WHEN a consolidation run executes THEN it SHALL make exactly **one** LLM call (`llm_service.consolidate_memory`) over the user's full compiled profile.

2.3 WHEN the LLM call returns a valid `MemoryConsolidation` THEN the system SHALL apply it in a **single write** via a `models` helper: refreshed `profile_summary` and `communication_style`, merged/de-duplicated `facts`/`beliefs`/`events`, and the synthesized `insights`, then set `last_consolidated_at` to now.

2.4 WHEN the LLM call fails (returns `None`) THEN the system SHALL NOT write or clear any memory (the **never-wipe contract**); it SHALL leave the existing profile untouched and log a warning.

2.5 WHEN a consolidation has applied its result THEN the system SHALL enforce `USER_MEMORY_BUDGET_CHARS` afterward by reusing the existing deterministic single-pass budget enforcement (`memory_compressor._enforce_budget`), so the profile never ends a run over budget.

2.6 WHERE a consolidation run executes THE SYSTEM SHALL run it entirely off the hot path (in the background, under `memory_lock`) and SHALL NOT add any work, round-trip, or latency to the reply path.

2.7 WHEN `last_consolidated_at` is updated THEN it SHALL be set even on a successful no-op-shaped result (a valid but empty-ish consolidation still advances the timestamp) so a due user is not re-selected every scan; a failed (`None`) run SHALL NOT advance it.

2.8 WHEN the run completes THEN it SHALL never raise into the scheduler (it wraps its own body and logs), consistent with `compress_user_memory`.

---

### Requirement 3: Synthesized durable insights

**User Story:** As a user, I want ThinkMate to notice durable patterns in how I behave and what I care about over time, so that it can respond with that longer-term understanding instead of only reacting to the latest messages.

#### Acceptance Criteria

3.1 WHERE an insight is defined THE SYSTEM SHALL treat it as a durable, higher-level behavioral or identity observation synthesized across the whole profile (e.g. "Tends to get stressed during exam season; values reassurance then"), distinct from a raw fact or a user-stated belief.

3.2 WHERE insights are stored THE SYSTEM SHALL store them in a dedicated, bounded profile-level `insights` list on the `user_profiles` document (NOT folded into `beliefs`); see the design rationale for why a dedicated list is chosen over reusing `beliefs`.

3.3 WHERE the number of insights is bounded THE SYSTEM SHALL keep at most `MAX_INSIGHTS` insights per user, truncating the consolidation result to that cap on write so the list cannot grow without limit.

3.4 WHEN the compiled memory block is built (`memory_loader.compile_memory_text`) THEN the system SHALL render the insights in their own clearly-labelled section so the system prompt surfaces them and the bot actually uses them.

3.5 WHERE a profile has no insights (e.g. before the first consolidation, or the feature disabled) THE SYSTEM SHALL render the section as an explicit empty placeholder and SHALL behave exactly as before (the section is additive; `compile_memory_text` reads `insights` defensively with a default of `[]`).

3.6 WHERE budget enforcement runs THE SYSTEM SHALL treat insights as the highest-priority, durable content: the deterministic enforcer (which sheds oldest events, then beliefs, then facts) SHALL NOT drop insights, and the `MAX_INSIGHTS` cap keeps their contribution small and bounded.

---

### Requirement 4: Configuration knobs (optional, safe defaults)

**User Story:** As an operator, I want consolidation to be fully configurable and off by default, so that enabling it is a deliberate opt-in and the bot's behavior is unchanged until I turn it on.

#### Acceptance Criteria

4.1 WHERE consolidation config lives THE SYSTEM SHALL add the following optional keys to `app/config.py` using the existing `_env_*` helpers, each with a safe default and introducing no new *required* config:
- `CONSOLIDATION_INTERVAL_SECS: float` — per-user cadence; **default `0.0` = disabled**.
- `CONSOLIDATION_SCAN_INTERVAL_SECS: float` — scheduler wake period; default `3600.0`.
- `CONSOLIDATION_MAX_USERS_PER_SCAN: int` — max users processed per wake; default `50`.
- `CONSOLIDATION_MIN_ITEMS: int` — minimum facts+beliefs+events for a user to be worth consolidating; default `8`.
- `MAX_INSIGHTS: int` — maximum stored insights per user; default `5`.

4.2 WHERE the feature is off by default THE SYSTEM SHALL ensure that with `CONSOLIDATION_INTERVAL_SECS = 0.0` (the default) no scheduler runs, no LLM call is made, and no profile is modified — i.e. behavior is identical to before Phase 11.

4.3 WHEN any consolidation knob is unset in the environment THEN the system SHALL fall back to the documented default rather than failing to start.

4.4 WHERE the new keys are introduced THE SYSTEM SHALL document them in `docs/development/configuration.md` and mirror them in `.env.example`, consistent with `.agents/rules/document_changes.md`.

---

### Requirement 5: Metrics & observability

**User Story:** As an operator, I want consolidation activity surfaced through the existing metrics registry and logs, so that I can see how often the dreaming pass runs and whether it is succeeding.

#### Acceptance Criteria

5.1 WHEN a consolidation run starts THEN the system SHALL `metrics.incr("consolidation.runs")` (reusing the existing in-memory registry), consistent with how the compressor increments `compression.runs`.

5.2 WHEN a consolidation run succeeds (a result was applied) THEN the system SHALL `metrics.incr("consolidation.success")`, AND WHEN it fails (`None` result or exception) THEN it SHALL `metrics.incr("consolidation.failure")`.

5.3 WHEN a scheduler scan completes THEN it SHALL log a single summary line of what it did (e.g. number of due users found and number processed).

5.4 WHERE metrics are recorded THE SYSTEM SHALL only use cheap in-memory metric operations and SHALL NOT let a metrics failure break a consolidation run (metrics are additive, mirroring Phase 10).

---

### Requirement 6: Backward-compatibility & safety

**User Story:** As a maintainer, I want Phase 11 to be additive and safe, so that all existing behavior and tests are preserved and the feature cannot harm a running instance.

#### Acceptance Criteria

6.1 WHERE the feature is disabled by default THE SYSTEM SHALL leave all existing behavior unchanged when `CONSOLIDATION_INTERVAL_SECS = 0.0`, and the existing test suite SHALL pass unmodified.

6.2 WHERE consolidation serializes with other background work THE SYSTEM SHALL run it under the same per-user `memory_lock` as the extractor and compressor, so it can never race them or corrupt a concurrent write.

6.3 WHERE failure safety is required THE SYSTEM SHALL never wipe or partially clear memory on a failed run (Requirement 2.4), and SHALL never let a single user's failure abort a scan or crash the scheduler (Requirements 1.6, 1.7).

6.4 WHERE bounded work is required THE SYSTEM SHALL cap users processed per scan (`CONSOLIDATION_MAX_USERS_PER_SCAN`) and cap stored insights (`MAX_INSIGHTS`), so neither runtime work nor stored state grows without limit.

6.5 WHERE the hot path is concerned THE SYSTEM SHALL add no DB round-trip or LLM call to the reply path; consolidation runs only from the background scheduler.

6.6 WHERE new documents and arrays are introduced THE SYSTEM SHALL read them defensively (`doc.get("insights") or []`, `doc.get("last_consolidated_at")`) so existing profiles written before Phase 11 continue to work without a migration.

---

### Requirement 7: Consolidation schema & LLM call

**User Story:** As a developer, I want a structured consolidation schema and a dedicated LLM method, so that the dreaming pass returns validated, typed output that the apply step can persist safely.

#### Acceptance Criteria

7.1 WHERE the schema lives THE SYSTEM SHALL add a `MemoryConsolidation` model to `app/services/schemas.py`, mirroring `MemoryCompression`, with: optional `profile_summary`, optional `communication_style`, `consolidated_facts`, `consolidated_beliefs`, `consolidated_events` (reusing the existing `CompressedFact`/`CompressedBelief`/`CompressedEvent` shapes), an optional `emotional_state`, and a new bounded `insights` list of a small `ConsolidatedInsight` model (`content: str`).

7.2 WHERE the LLM call lives THE SYSTEM SHALL add `consolidate_memory(user_id, system_prompt, raw_memory_text) -> MemoryConsolidation | None` to `LLMService`, implemented via the existing `_structured_call` with `call_type="memory_consolidation"`, the extraction model, and the `None`-on-failure contract (mirroring `compress_memory`).

7.3 WHERE the prompt lives THE SYSTEM SHALL add `app/prompts/consolidation_prompt.py` (`SYSTEM_CONSOLIDATION_PROMPT`) following `compression_prompt.py`, instructing the model to review the full profile, merge/de-duplicate, refresh the summary/style, and synthesize at most `MAX_INSIGHTS` durable behavioral/identity insights, returning JSON matching the schema.

7.4 WHEN the consolidation prompt is assembled THEN the run SHALL inject the effective `MAX_INSIGHTS` cap into the prompt (mirroring how the compressor injects its target character budget), so the model is told how many insights it may emit.

---

### Requirement 8: Data model & CRUD

**User Story:** As a developer, I want the persistence helpers consolidation needs, so that finding due users and applying a result are atomic, single-write, and test-friendly.

#### Acceptance Criteria

8.1 WHERE the profile shape changes THE SYSTEM SHALL support a `last_consolidated_at` timestamp and an `insights` list on `user_profiles` documents, both optional and read defensively so pre-existing documents need no migration; `ensure_user` SHALL initialize `insights` to `[]` on insert (additive `$setOnInsert`).

8.2 WHERE due users are found THE SYSTEM SHALL add a `find_users_due_for_consolidation(db, *, interval_secs, min_items, limit)` helper that returns at most `limit` user ids whose `last_consolidated_at` is older than the cutoff (or null/absent) and whose facts+beliefs+events count ≥ `min_items`, bounding its own work while iterating.

8.3 WHERE a result is applied THE SYSTEM SHALL add an `apply_consolidation(db, user_id, consolidation)` helper that performs a single `$set` write of refreshed summary/style, merged facts/beliefs/events, the `insights` list truncated to `config.MAX_INSIGHTS`, the emotional state (when present), `last_consolidated_at = now`, and `updated_at = now` — mirroring `replace_user_memory`'s single-write style.

8.4 WHEN `apply_consolidation` writes insights THEN it SHALL truncate to at most `config.MAX_INSIGHTS` entries so the stored list is always bounded regardless of what the model returned.

---

### Requirement 9: Test coverage

**User Story:** As a maintainer, I want hermetic tests for the consolidation layer, so that the schema, CRUD, run, insights rendering, and scheduler are verified without external services.

#### Acceptance Criteria

9.1 WHERE tests are written THE SYSTEM SHALL use **mongomock + pytest-asyncio** per `tests/conftest.py` conventions (async mock wrappers, autouse DB patch), with no real LLM or network; the LLM SHALL be patched with `AsyncMock`.

9.2 WHEN the CRUD helpers are tested THEN the tests SHALL assert `find_users_due_for_consolidation` selects null/old/`< interval` users with `≥ min_items` and excludes recent or item-poor users and respects `limit`, and that `apply_consolidation` writes a single coherent profile, sets `last_consolidated_at`, and truncates `insights` to `MAX_INSIGHTS`.

9.3 WHEN the run is tested THEN the tests SHALL assert that a valid result is applied + budget-enforced + timestamped, that a `None` result **never** wipes memory and does **not** advance `last_consolidated_at`, and that the profile ends ≤ `USER_MEMORY_BUDGET_CHARS`.

9.4 WHEN insights rendering is tested THEN the tests SHALL assert `compile_memory_text` renders the insights section (and a placeholder when empty), and that a profile with no `insights` key behaves exactly as before.

9.5 WHEN the scheduler is tested THEN the tests SHALL assert it starts no task when `CONSOLIDATION_INTERVAL_SECS <= 0`, processes at most `CONSOLIDATION_MAX_USERS_PER_SCAN` due users when enabled, continues past a single user's failure, self-heals on an iteration error, and routes work through `run_consolidator` (memory_lock serialization).

9.6 WHEN the full suite is run after Phase 11 THEN every previously passing test SHALL still pass with no warnings and no external services, with the feature disabled by default.
