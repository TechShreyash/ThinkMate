# Requirements Document

Phase 10: Observability & ops.

## Introduction

ThinkMate runs as a single long-polling process whose practical ceiling is **LLM throughput**, not the Python event loop or MongoDB (see `docs/development/performance_and_scaling.md`). Today the only operational visibility is structured `loguru` logging and the `llm_audit_log` collection. Phase 10 adds a lightweight, in-process observability layer so an operator can answer one question quickly: **"are we near the ceiling?"**

This is deliberately **not** a Prometheus/OTel server. It is a dependency-free, process-wide, in-memory metrics registry plus health/readiness helpers and an admin command, all cheap enough to live on (or beside) the hot path without violating the hot-path invariants. The build target follows the Phase 10 section of `docs/project_plan.md`.

Core ideas:
- A new `app/services/metrics.py` singleton (`metrics`) holds counters, gauges, and timer/histogram-lite aggregates (count + sum + max → avg/max). Stdlib only, bounded by a small fixed set of metric names.
- The hot path is instrumented with cheap in-memory increments/observations only — never a DB or LLM call added for metrics: LLM calls by type + success/failure + latency (`llm_service`), throttle drops (`middlewares`), queue drops + active-conversation gauge (`user_task_manager`), and extraction/compression run counts.
- A new `app/services/health.py` exposes `liveness()` (process up + uptime + metrics summary) and async `readiness(db)` (Mongo ping ok/fail, never raises).
- A `/health` (and optional `/metrics`) admin command in `commands.py` reports liveness + a metrics summary as text, optionally gated by an optional `ADMIN_USER_IDS` config.
- An optional periodic background task logs the snapshot every N seconds.
- A runbook `docs/development/observability.md` explains what the metrics mean and how to recognize the LLM-throughput ceiling.

## Glossary

- **Metric registry**: the process-wide in-memory store of counters, gauges, and timers exposed via the `metrics` singleton.
- **Counter**: a monotonically increasing integer (e.g. total LLM calls); incremented via `incr(name, n)`.
- **Gauge**: a point-in-time value that can go up or down (e.g. active conversation count); set via `set_gauge(name, value)`.
- **Timer / histogram-lite**: a latency aggregate recording count + sum + max for a name, enough to compute avg and max; fed via `observe(name, value)` / `record_latency(name, secs)` / a `timer(name)` context manager.
- **Snapshot**: a plain `dict` of all current counters/gauges/timers returned by `snapshot()`, safe to log or render.
- **Liveness**: a check that the process itself is up and responsive (no external dependency).
- **Readiness**: a check that external dependencies (MongoDB) are reachable, so the instance can actually serve.
- **Hot path**: everything between "a user's batch is ready" and "the reply is sent" (see `performance_and_scaling.md`); instrumentation must add no DB/LLM round-trip here.

---

## Requirements

### Requirement 1: In-memory metrics registry

**User Story:** As an operator, I want a cheap, dependency-free metrics registry inside the process, so that I can observe throughput and saturation without adding infrastructure.

#### Acceptance Criteria

1.1 WHERE metrics are recorded THE SYSTEM SHALL provide a single process-wide singleton `metrics` in `app/services/metrics.py` implemented with the Python standard library only (no third-party dependency).

1.2 WHEN `incr(name, n=1)` is called THEN the system SHALL increase the named counter by `n` (default 1), creating it at 0 on first use.

1.3 WHEN `set_gauge(name, value)` is called THEN the system SHALL store `value` as the current value of the named gauge, replacing any previous value.

1.4 WHEN `observe(name, value)` (or `record_latency(name, value)`) is called THEN the system SHALL update the named timer aggregate's count (+1), sum (+value), and max (= max(prev, value)), so that average and maximum can be derived.

1.5 WHEN the `timer(name)` context manager is used around a block THEN the system SHALL record the block's wall-clock duration as an `observe(name, duration)` exactly once, including when the block raises (the exception SHALL propagate unchanged).

1.6 WHEN `snapshot()` is called THEN the system SHALL return a plain `dict` containing all current counters, gauges, and timer aggregates (count, sum, max, and derived avg), suitable for logging or rendering.

1.7 WHEN `reset()` is called THEN the system SHALL clear all counters, gauges, and timers back to empty, so that tests are isolated from one another.

1.8 WHERE the registry is used from concurrent async tasks on a single event loop THE SYSTEM SHALL keep individual record operations safe from corruption (no partially-applied update) using a lightweight lock or atomic operation, while remaining non-blocking enough for the hot path.

1.9 WHERE metric names are recorded THE SYSTEM SHALL draw them from a small fixed set so that registry memory is bounded and cannot grow without limit.

1.10 IF `snapshot()` is called when no metrics have been recorded THEN the system SHALL return an empty-but-well-formed structure (empty sections) rather than raising.

---

### Requirement 2: Hot-path instrumentation (non-behavioral)

**User Story:** As an operator, I want the hot path instrumented at the points that matter, so that I can see LLM volume/latency, throttle and queue drops, active conversations, and background-job runs.

#### Acceptance Criteria

2.1 WHEN any LLM call is made via `llm_service` (reply bundle, memory extraction, group extraction, compression) THEN the system SHALL increment a per-call-type counter and record the call's latency to a per-call-type timer.

2.2 WHEN an LLM call succeeds THEN the system SHALL increment a per-call-type success counter, AND WHEN an LLM call fails (raises or returns the failure sentinel) THEN the system SHALL increment a per-call-type failure counter.

2.3 WHEN the `ThrottlingMiddleware` drops a message because the per-user rate limit is exceeded THEN the system SHALL increment a throttle-drop counter.

2.4 WHEN `UserTaskManager.enqueue_message` drops a message because the per-conversation queue is at `MAX_QUEUED_MESSAGES` THEN the system SHALL increment a queue-drop counter.

2.5 WHEN the set of active conversation states changes (a state is created or idle states are evicted) THEN the system SHALL set an active-conversation gauge to the current `len(_states)`.

2.6 WHEN a background memory extraction run starts (single-party or group) THEN the system SHALL increment an extraction-run counter, AND WHEN a background compression run starts THEN the system SHALL increment a compression-run counter.

2.7 WHERE instrumentation runs on the hot path THE SYSTEM SHALL use only cheap in-memory metric operations and SHALL NOT add any MongoDB round-trip or LLM call (the hot-path invariants in `performance_and_scaling.md` remain: one reply LLM call, ≤3 Mongo round-trips).

2.8 IF a metric operation itself fails for any reason THEN the system SHALL NOT change the observable behavior of the instrumented code path (a metrics failure must never break a reply, a drop decision, or a background job).

2.9 WHERE existing behavior is concerned THE SYSTEM SHALL keep all instrumented functions' return values, signatures, and side effects unchanged (instrumentation is additive only), so the existing test suite passes unmodified.

---

### Requirement 3: Health & readiness signals

**User Story:** As an operator, I want liveness and readiness checks, so that I can tell whether the process is up and whether its dependencies are reachable.

#### Acceptance Criteria

3.1 WHERE health checks live THE SYSTEM SHALL provide `liveness()` and async `readiness(db)` in `app/services/health.py`.

3.2 WHEN `liveness()` is called THEN the system SHALL return a `dict` reporting status `"ok"`, the process uptime in seconds, and a compact summary derived from the metrics snapshot, without performing any I/O.

3.3 WHEN `readiness(db)` is called AND MongoDB responds to a `ping` THEN the system SHALL return a `dict` indicating the database is reachable (e.g. `{"ready": true, "mongo": "ok"}`).

3.4 WHEN `readiness(db)` is called AND the MongoDB `ping` fails or times out THEN the system SHALL return a `dict` indicating not-ready with the failure reason and SHALL NOT raise.

3.5 WHERE uptime is reported THE SYSTEM SHALL measure it from a process-start timestamp captured once at import/initialization.

3.6 WHEN either health function encounters an unexpected internal error THEN the system SHALL degrade gracefully to a well-formed "degraded"/"not ready" result rather than propagating an exception to the caller.

---

### Requirement 4: Admin health/metrics command

**User Story:** As an operator chatting with the bot, I want a `/health` command, so that I can read the live status and a metrics summary and judge whether we are near the LLM ceiling.

#### Acceptance Criteria

4.1 WHEN an authorized user sends `/health` THEN the system SHALL reply with a readable text report combining liveness (status + uptime) and a metrics-snapshot summary (LLM counts/latency, throttle drops, queue drops, active conversations, extraction/compression runs).

4.2 WHEN `/health` is handled THEN the system SHALL also include readiness (MongoDB ping result) using the injected `db` session, reporting a degraded result rather than failing if the ping fails.

4.3 WHERE an optional `ADMIN_USER_IDS` config (comma-separated user ids) is set AND non-empty THE SYSTEM SHALL only honor `/health` (and `/metrics`) for those user ids, and SHALL ignore or politely decline the command for everyone else.

4.4 WHERE `ADMIN_USER_IDS` is unset or empty THE SYSTEM SHALL apply the documented safe default: respond only in private chats (DMs) and not in groups, so the report is never broadcast to a group.

4.5 WHEN a `/metrics` command is provided (optional) THEN the system SHALL reply with the raw metrics snapshot summary as text, under the same authorization rule as `/health`.

4.6 WHEN the admin commands are registered THEN the system SHALL route them as commands (never treated as conversation or an ambient trigger) and SHALL add them to the bot command list consistent with the existing command style.

4.7 WHEN building the report THE SYSTEM SHALL only read the in-memory snapshot and a single Mongo ping, adding no LLM call.

---

### Requirement 5: Optional periodic metrics logging

**User Story:** As an operator, I want the metrics snapshot logged periodically, so that I have a time series in the logs even without issuing a command.

#### Acceptance Criteria

5.1 WHERE periodic logging is enabled THE SYSTEM SHALL run a single background task that logs the metrics snapshot summary every `METRICS_LOG_INTERVAL_SECS` seconds.

5.2 WHEN the periodic logger is running THEN the system SHALL emit exactly one log line per interval and SHALL NOT perform any DB or LLM call.

5.3 WHERE the periodic logger is configured off (interval ≤ 0 or feature disabled) THE SYSTEM SHALL not start the task, and its absence SHALL not affect any other behavior.

5.4 WHEN the periodic logger task encounters an error THEN the system SHALL log and continue (the loop SHALL never crash the process).

---

### Requirement 6: Runbook & documentation

**User Story:** As an operator or contributor, I want a runbook, so that I can interpret the metrics, read the audit log efficiently, and recognize/tune around the LLM ceiling.

#### Acceptance Criteria

6.1 WHERE the runbook lives THE SYSTEM SHALL add `docs/development/observability.md` describing every metric name, what it means, and what a healthy vs. concerning value looks like.

6.2 WHEN the runbook covers the audit log THEN it SHALL explain how to read `llm_audit_log` using its compound `(user_id, 1),(timestamp, -1)` index and its TTL.

6.3 WHEN the runbook covers capacity THEN it SHALL explain how to recognize the LLM-throughput ceiling (the saturation signals from `performance_and_scaling.md`) and how to tune budgets/batching in response.

6.4 WHEN the runbook covers operations THEN it SHALL document the `/health` and `/metrics` commands, the `ADMIN_USER_IDS` default, and the optional periodic logger.

6.5 WHERE documentation cross-linking is required (per `.agents/rules/document_changes.md`) THE SYSTEM SHALL cross-link the runbook from `README.md`, `docs/architecture.md`, and `docs/development/performance_and_scaling.md`, and SHALL mark Phase 10 progress in `docs/project_plan.md`.

6.6 WHERE any new config key is introduced (e.g. `ADMIN_USER_IDS`, `METRICS_LOG_INTERVAL_SECS`) THE SYSTEM SHALL document it in `docs/development/configuration.md` and mirror it in `.env.example`.

---

### Requirement 7: Test coverage

**User Story:** As a maintainer, I want hermetic tests for the observability layer, so that the metrics, health checks, command, and instrumentation are verified without external services.

#### Acceptance Criteria

7.1 WHERE tests are written THE SYSTEM SHALL use **mongomock + pytest-asyncio** per `tests/conftest.py` conventions (async mock wrappers, autouse DB patch), with no real LLM or network.

7.2 WHEN the metrics registry is tested THEN the tests SHALL assert counters increment, gauges set/replace, timers aggregate count/sum/max/avg correctly, the `timer` context manager records on success and on exception, `snapshot()` shape is correct, and `reset()` isolates tests.

7.3 WHEN readiness is tested THEN the tests SHALL assert it returns ready on a working mock DB and degrades gracefully (no raise) when the ping fails.

7.4 WHEN the `/health` command is tested THEN the tests SHALL assert it replies with a report, honors the `ADMIN_USER_IDS` / DM-only authorization default, and adds no LLM call.

7.5 WHEN instrumentation is tested THEN the tests SHALL assert that a throttle drop increments the throttle-drop counter, a queue drop increments the queue-drop counter, and an LLM call increments its per-type counter and records latency (using `AsyncMock` for the LLM as in `tests/test_batching_and_concurrency.py`).

7.6 WHEN the full suite is run after Phase 10 THEN every previously passing test SHALL still pass with no warnings and no external services.
