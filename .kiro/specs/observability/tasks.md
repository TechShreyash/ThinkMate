# Implementation Plan

## Overview

This plan delivers Phase 10 (observability & ops) as an additive, in-process, single-instance layer over the existing bot — **not** a Prometheus/OTel server. Work proceeds bottom-up: a dependency-free in-memory metrics registry first, then cheap non-behavioral instrumentation spread across disjoint hot-path files (so they parallelize), then health/readiness helpers and an admin `/health` command, an optional periodic metrics logger, the runbook plus cross-linked doc updates, and finally a full-suite checkpoint. Every implementation task is paired with a test task using **mongomock + pytest-asyncio** per `tests/conftest.py`. Instrumentation is additive only (same signatures, same return values, metrics never break a path) so the existing suite passes unmodified. Tasks are grouped into parallelizable waves where dependencies allow; implementation tasks touch disjoint files within a wave.

## Task Dependency Graph

```mermaid
graph TD
    T11[1.1 metrics.py registry + timer + snapshot/reset]
    T12[1.2 Tests: counters/gauges/timers/snapshot/reset]
    T31cfg[2.1 config: ADMIN_USER_IDS + METRICS_LOG_INTERVAL_SECS]
    T21[3.1 Instrument llm_service calls + latency]
    T22[3.2 Instrument middlewares throttle drops]
    T23[3.3 Instrument user_task_manager queue drops + active gauge]
    T24[3.4 Instrument extraction/compression run counts]
    T25[3.5 Tests: instrumentation increments]
    T41[4.1 health.py liveness + readiness]
    T42[4.2 /health (+ /metrics) admin command]
    T43[4.3 Tests: readiness + command auth]
    T51[5.1 Optional periodic metrics logger + wire in main]
    T52[5.2 Tests: periodic logger]
    T61[6.1 Runbook docs/development/observability.md]
    T62[6.2 Cross-linked doc updates + .env.example/config]
    T7[7. Checkpoint - full suite green]

    T11 --> T12
    T11 --> T21
    T11 --> T22
    T11 --> T23
    T11 --> T24
    T21 --> T25
    T22 --> T25
    T23 --> T25
    T24 --> T25
    T11 --> T41
    T31cfg --> T42
    T41 --> T42
    T42 --> T43
    T41 --> T43
    T31cfg --> T51
    T41 --> T51
    T51 --> T52
    T25 --> T61
    T43 --> T61
    T52 --> T61
    T61 --> T62
    T12 --> T7
    T25 --> T7
    T43 --> T7
    T52 --> T7
    T62 --> T7
```

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1.1", "2.1"] },
    { "wave": 2, "tasks": ["1.2", "3.1", "3.2", "3.3", "3.4", "4.1"] },
    { "wave": 3, "tasks": ["3.5", "4.2", "5.1"] },
    { "wave": 4, "tasks": ["4.3", "5.2"] },
    { "wave": 5, "tasks": ["6.1"] },
    { "wave": 6, "tasks": ["6.2"] },
    { "wave": 7, "tasks": ["7"] }
  ]
}
```

## Tasks

- [ ] 1. Metrics registry foundation

  - [ ] 1.1 Implement the in-memory metrics registry
    - Create `app/services/metrics.py` with a `MetricsRegistry` class and a module-level singleton `metrics`, using the Python standard library only (no third-party dependency)
    - Implement `incr(name, n=1)` (counter, auto-create at 0), `set_gauge(name, value)` (replace), `observe(name, value)` / `record_latency(name, value)` (timer aggregate: count+1, sum+=value, max=max(prev,value)), and a `timer(name)` context manager (via `contextlib.contextmanager` + `time.perf_counter()`) that records the block's duration exactly once in a `finally` so it records on exceptions and re-raises unchanged
    - Implement `snapshot()` returning `{"counters": {...}, "gauges": {...}, "timers": {name: {count, sum, max, avg}}}` (empty-but-well-formed when nothing recorded), `reset()` to clear all state, and a `record_llm(call_type, *, ok, latency)` convenience that maps to `llm.<type>.calls`/`.success`/`.failure` + `llm.<type>.latency`
    - Guard each mutator with a brief `threading.Lock` and wrap every mutator body in `try/except Exception` → debug-log → return, so a metrics failure can never raise into a caller; keep callers limited to the fixed metric set so memory stays bounded
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 2.8_

  - [ ] 1.2 Tests: registry counters, gauges, timers, snapshot, reset
    - In a new `tests/test_metrics.py` (pytest-asyncio per `tests/conftest.py`), assert `incr` accumulates with default and explicit `n`, `set_gauge` replaces rather than accumulates, and `observe`/`record_latency` build `count`/`sum`/`max` with `snapshot()` deriving `avg = sum/count`
    - Assert the `timer(name)` context manager records exactly one observation on normal exit, and on a raised exception records once AND propagates the original exception; assert `snapshot()` has the three sections, an empty registry returns empty sections without raising, and `reset()` clears everything
    - Add a fixture that calls `metrics.reset()` before/after each test for isolation
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.10, 7.1, 7.2_

- [ ] 2. Configuration knobs

  - [ ] 2.1 Add optional observability config keys
    - In `app/config.py`, add `ADMIN_USER_IDS: set[int]` parsed from a comma-separated `ADMIN_USER_IDS` env var (ignore blanks, coerce to `int`, default empty set) and `METRICS_LOG_INTERVAL_SECS: float` (default `0.0` = periodic logger disabled), via small env parsers consistent with the existing `_env_*` helpers
    - Introduce no new *required* configuration; both keys must have safe defaults so the bot runs unchanged when they are unset
    - _Requirements: 4.3, 4.4, 5.1, 6.6_

- [ ] 3. Hot-path instrumentation (additive, disjoint files)

  - [ ] 3.1 Instrument LLM calls + latency in `llm_service`
    - In `app/services/llm_service.py`, wrap the reply call in `generate_reply_bundle` with `metrics.timer("llm.reply.latency")` and call `metrics.record_llm("chat_reply", ok=..., latency=...)` on the existing success and `except` branches; wrap the call in `_structured_call` similarly, using its `call_type` parameter so `memory_extraction` / `group_memory_extraction` / `memory_compression` are all covered in one place
    - Optionally wrap the `_log_llm_call` insert with `metrics.timer("audit.write.latency")` to surface audit write lag; keep all return values, the `(reply, reaction[, affinity_delta])` contract, `_fire_log`, and control flow byte-for-byte unchanged (instrumentation is additive and must never raise)
    - _Requirements: 2.1, 2.2, 2.7, 2.8, 2.9_

  - [ ] 3.2 Instrument throttle drops in `middlewares`
    - In `app/handlers/middlewares.py`, on the early-return branch of `ThrottlingMiddleware.__call__` where a message is dropped because `len(window) >= RATE_LIMIT_MAX_REQUESTS`, add `metrics.incr("throttle.drops")` before the `return`
    - Leave the warn-once behavior, sliding-window bookkeeping, and pruning unchanged
    - _Requirements: 2.3, 2.7, 2.8, 2.9_

  - [ ] 3.3 Instrument queue drops + active-conversation gauge in `user_task_manager`
    - In `app/services/user_task_manager.py`, add `metrics.incr("queue.drops")` on the `len(state.pending_messages) >= MAX_QUEUED_MESSAGES` early-return branch of `enqueue_message`
    - Set `metrics.set_gauge("conversations.active", len(self._states))` after a new `UserState` is created in `get_state` and after stale states are removed in `_evict_idle`; keep batching/typing/eviction behavior unchanged
    - _Requirements: 2.4, 2.5, 2.7, 2.8, 2.9_

  - [ ] 3.4 Instrument extraction/compression run counts
    - In `app/services/user_task_manager.py`'s `run_extractor` add `metrics.incr("extraction.runs")` and in `run_compressor` add `metrics.incr("compression.runs")` at the point each run actually proceeds (past the cooldown/lock guards, so skipped runs are not counted) — and/or at the start of `extract_and_trim*` in `app/services/memory_extractor.py` and `compress_user_memory` in `app/services/memory_compressor.py`, picking one consistent placement
    - Keep all extraction/compression behavior, contracts, and trim logic unchanged
    - _Requirements: 2.6, 2.7, 2.8, 2.9_

  - [ ] 3.5 Tests: instrumentation increments expected metrics
    - In a new `tests/test_metrics_instrumentation.py` (mongomock + pytest-asyncio, `metrics.reset()` fixture), drive `ThrottlingMiddleware` past the limit (as in `test_throttling_middleware`) and assert `throttle.drops` increments by the number of drops; drive `enqueue_message` past `MAX_QUEUED_MESSAGES` (as in `test_user_task_manager_queue_limit_guard`) and assert `queue.drops` increments
    - With the LLM patched via `AsyncMock`, run `handle_message` and assert the per-type LLM counter and latency timer moved (and the success/failure split is correct when the call is forced to raise); assert `conversations.active` equals `len(_states)` after `get_state`/eviction, and that `run_extractor`/`run_compressor` bump `extraction.runs`/`compression.runs` only when the run proceeds
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 7.5_

- [ ] 4. Health, readiness & admin command

  - [ ] 4.1 Implement `health.py` (liveness + readiness)
    - Create `app/services/health.py` with a module-level `_PROCESS_START` timestamp captured once at import, `liveness()` returning `{"status": "ok", "uptime_secs": ..., "summary": {<compact metrics summary>}}` with no I/O, and async `readiness(db)` that runs a single Mongo `ping` and returns `{"ready": True, "mongo": "ok"}` on success or `{"ready": False, "mongo": "error", "reason": ...}` on failure — wrapped so it never raises (including server-selection timeouts)
    - Add a shared compact summary formatter over `metrics.snapshot()` (total LLM calls, reply avg/max latency, throttle/queue drops, active conversations, extraction/compression runs) reused by the command and the periodic logger; degrade to a minimal `{"status": "degraded"}`/not-ready result on any unexpected internal error
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ] 4.2 Add `/health` (and optional `/metrics`) admin command
    - In `app/handlers/commands.py`, add a `/health` handler that builds a readable text report from `liveness()` + `await readiness(db)` (and optionally `/metrics` for the raw snapshot summary), reading only the in-memory snapshot and one Mongo ping (no LLM call)
    - Add an `_admin_allowed(message)` gate: if `config.ADMIN_USER_IDS` is non-empty, allow only those `from_user.id`s; otherwise apply the safe default of replying only in private chats (DMs) so a report is never broadcast to a group; register the command(s) in the commands router and the `main.py` command list, consistent with the existing `/quiet`/`/chatty` style
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [ ] 4.3 Tests: readiness + command authorization
    - In a new `tests/test_health_and_command.py` (mongomock + pytest-asyncio), assert `readiness(db)` returns ready on the mock DB and a degraded (non-raising) dict when the ping is patched to raise; assert `liveness()` returns `status="ok"`, a numeric `uptime_secs`, and a summary, with no DB access
    - Assert `/health` in a DM (default, empty `ADMIN_USER_IDS`) replies once and calls no LLM; with `config.ADMIN_USER_IDS = {123}`, an allowed id gets a reply while a different id / a group chat is declined or ignored; assert the readiness-failure path still yields a (degraded) report
    - _Requirements: 3.3, 3.4, 4.1, 4.3, 4.4, 4.7, 7.3, 7.4_

- [ ] 5. Optional periodic metrics logger

  - [ ] 5.1 Implement and wire the periodic logger
    - In `app/services/health.py`, add async `start_metrics_logger()` that, when `config.METRICS_LOG_INTERVAL_SECS > 0`, starts a single background task logging the compact snapshot summary once per interval (no DB/LLM call) and wraps each iteration in `try/except` so the loop never crashes the process; return `None` (no task started) when the interval ≤ 0
    - Call it from `main.py` after `init_db()` so the logger starts with the app when enabled and is a no-op when disabled
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [ ] 5.2 Tests: periodic logger
    - In `tests/test_health_and_command.py` (or a dedicated test), with a tiny `METRICS_LOG_INTERVAL_SECS` assert exactly one log line is emitted per interval (capture via loguru/caplog where practical) and no DB/LLM call occurs; assert that with interval ≤ 0 no task is started, and that an error raised inside one iteration is logged and the loop continues
    - _Requirements: 5.2, 5.3, 5.4_

- [ ] 6. Runbook & documentation

  - [ ] 6.1 Write the observability runbook
    - Create `docs/development/observability.md` documenting every metric name and meaning (with healthy vs. concerning values), how to read `llm_audit_log` via its compound `(user_id, 1),(timestamp, -1)` index and TTL, how to recognize the LLM-throughput ceiling using the saturation signals from `performance_and_scaling.md`, how to tune budgets/batching in response, and how to use the `/health` and `/metrics` commands (including the `ADMIN_USER_IDS` default and the optional periodic logger)
    - Include a navigation header / table of contents and cross-links per `.agents/rules/document_changes.md`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [ ] 6.2 Cross-link docs and update config references
    - Cross-link the new runbook from `README.md`, `docs/architecture.md`, and `docs/development/performance_and_scaling.md` (Step 5 — stateless audit & metrics), and mark Phase 10 progress in `docs/project_plan.md`
    - Document the new `ADMIN_USER_IDS` and `METRICS_LOG_INTERVAL_SECS` keys in `docs/development/configuration.md` and mirror them in `.env.example`
    - _Requirements: 6.5, 6.6_

- [ ] 7. Checkpoint - ensure the full suite passes
  - Run the full test suite (`uv run pytest` or the project's configured command) and confirm every test passes with no warnings and no external services, including all pre-existing tests unmodified
  - Confirm the hot-path invariants still hold: one reply LLM call per batch, ≤3 Mongo round-trips, and that instrumentation adds only cheap in-memory operations (no DB/LLM round-trip) and never alters observable behavior
  - _Requirements: 2.7, 2.9, 7.6_

## Notes

- **Additive & non-behavioral is the top constraint.** Every instrumentation call is a side-effect-only statement beside existing logic; signatures, return values, and control flow are unchanged, and all metric mutators swallow their own errors so a metrics failure can never break a reply, a drop decision, or a background job (Requirement 2.8/2.9). Task 7 enforces that the existing suite passes unmodified.
- **Not a metrics server.** This is an in-process, single-instance layer (stdlib only, bounded fixed metric set). It surfaces the Phase 10 signals via the `/health` command and optional log lines — the Prometheus/OTel sink is the future Phase 12 step in `performance_and_scaling.md`.
- **Zero hot-path I/O.** Instrumentation only touches in-memory, lock-guarded dicts; no DB/LLM round-trip is added, so the ≤3-round-trip / one-LLM-call invariants from `performance_and_scaling.md` hold.
- **Disjoint files parallelize.** Tasks 3.1–3.4 edit `llm_service.py`, `middlewares.py`, `user_task_manager.py`, and the extractor/compressor respectively, so they run in the same wave without conflict; each is validated by the paired instrumentation test (3.5).
- **Safe admin default.** When `ADMIN_USER_IDS` is empty, `/health` answers only in DMs so a status report is never broadcast into a group; setting `ADMIN_USER_IDS` restricts it to specific operators.
- **Test conventions.** All tests use mongomock + pytest-asyncio per `tests/conftest.py`; the LLM is patched with `AsyncMock` (as in `tests/test_batching_and_concurrency.py`), and a `metrics.reset()` fixture isolates metric state between tests.
- The runbook is a Phase 10 deliverable (scope item 6); doc cross-linking and config mirroring follow `.agents/rules/document_changes.md`.
