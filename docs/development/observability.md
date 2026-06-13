# Observability & Ops Runbook

> The operator's guide to ThinkMate's in-process observability layer (Phase 10). It explains
> every metric the bot records, how to read them through the `/health` and `/metrics` admin
> commands, how to inspect the `llm_audit_log`, and — most importantly — **how to tell whether
> you are near the LLM-throughput ceiling** and what to tune in response. Read this alongside
> [performance_and_scaling.md](performance_and_scaling.md) (the capacity model),
> [database.md](database.md) (the audit-log schema and indexes), and
> [configuration.md](configuration.md) (every tuning knob).

## Table of Contents
1. [What this layer is (and isn't)](#what-this-layer-is-and-isnt)
2. [The metric set](#the-metric-set)
3. [The in-memory metrics registry](#the-in-memory-metrics-registry)
4. [Health & readiness helpers](#health--readiness-helpers)
5. [The optional periodic metrics logger](#the-optional-periodic-metrics-logger)
6. [Admin commands: `/health` and `/metrics`](#admin-commands-health-and-metrics)
7. [Reading the `llm_audit_log`](#reading-the-llm_audit_log)
8. [Recognizing the LLM-throughput ceiling](#recognizing-the-llm-throughput-ceiling)
9. [Tuning budgets & batching in response](#tuning-budgets--batching-in-response)
10. [Related docs](#related-docs)

---

## What this layer is (and isn't)

ThinkMate runs as **one long-polling process** whose practical ceiling is **LLM throughput**,
not the Python event loop or MongoDB (see
[performance_and_scaling.md](performance_and_scaling.md#the-single-instance-ceiling)). Phase 10
adds a lightweight, in-process observability layer so an operator can answer one question
quickly: **"are we near the ceiling?"**

**What it is:**
- A dependency-free, process-wide, in-memory metrics registry
  ([`app/services/metrics.py`](../../app/services/metrics.py)) — counters, gauges, and
  timer/histogram-lite aggregates, stdlib only.
- Cheap hot-path instrumentation that adds **no** DB or LLM round-trip.
- Liveness/readiness helpers ([`app/services/health.py`](../../app/services/health.py)).
- Admin `/health` and `/metrics` commands ([`app/handlers/commands.py`](../../app/handlers/commands.py)).
- An optional periodic metrics logger.

**What it is NOT:**
- It is **not** a Prometheus/OpenTelemetry server. There is no scrape endpoint, no external
  time-series database, no exporter. The metrics live in memory in this one process and reset
  when it restarts.
- The external metrics sink (Prometheus/OTel) is a **future** step — it is Step 5 of the
  [horizontal-scale migration path](performance_and_scaling.md#horizontal-scale-migration-path-future)
  (Phase 12), not part of this layer.

Because everything is in-memory and single-instance, treat the numbers as a **live operational
snapshot** for the running process, not a historical record. For history, use the periodic
logger (a log time series) or the `llm_audit_log` (per-call detail).

[Back to top](#table-of-contents)

---

## The metric set

All metric names are drawn from a **small fixed set** (so registry memory is bounded) and use a
dotted namespace. The table below lists every metric, its type, where it is recorded, what it
means, and how to read a **healthy** vs. a **concerning** value.

> Types: **counter** = monotonically increasing total; **gauge** = latest point-in-time value;
> **timer** = a latency aggregate holding `count` / `sum` / `max`, from which `avg` is derived.

### LLM call metrics

The reply call is the dominant cost on the hot path; the others are amortized background work.
For every LLM call type, three counters (`.calls`, `.success`, `.failure`) and one latency timer
are recorded via the `metrics.record_llm(call_type, ok=…, latency=…)` helper.

| Metric name | Type | Recorded at | Meaning | Healthy | Concerning |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `llm.reply.calls` | counter | `llm_service.generate_reply_bundle` | Total reply-bundle calls (one per message batch). | Tracks active conversation volume. | — (volume signal; read with latency). |
| `llm.reply.success` | counter | same | Reply calls that returned a usable bundle. | ≈ `llm.reply.calls`. | Diverging from `.calls`. |
| `llm.reply.failure` | counter | same | Reply calls that raised or returned the failure sentinel. | ~0, or a tiny fraction. | Rising / a growing share of `.calls` → endpoint stress or bad output. |
| `llm.reply.latency` | timer | wraps the reply call | Reply latency (`avg`, `max`). | `avg` steady and well under the batch budget. | `avg`/`max` rising → **LLM endpoint saturating** (primary ceiling signal). |
| `llm.extraction.calls` / `.success` / `.failure` | counter | `_structured_call` (`memory_extraction`) | Single-party memory-extraction volume/outcomes. | failures ~0. | rising failures → extraction model/endpoint stress. |
| `llm.extraction.latency` | timer | wraps extraction call | Extraction latency. | steady. | rising → shared endpoint pressure. |
| `llm.group_extraction.calls` / `.success` / `.failure` | counter | `_structured_call` (`group_memory_extraction`) | Group memory-extraction volume/outcomes. | failures ~0. | rising failures. |
| `llm.group_extraction.latency` | timer | wraps group extraction call | Group extraction latency. | steady. | rising. |
| `llm.compression.calls` / `.success` / `.failure` | counter | `_structured_call` (`memory_compression`) | Memory-compression volume/outcomes. | failures ~0 (a failed compression is skipped, never wipes memory). | rising failures. |
| `llm.compression.latency` | timer | wraps compression call | Compression latency. | steady. | rising. |

### Hot-path & background metrics

| Metric name | Type | Recorded at | Meaning | Healthy | Concerning |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `throttle.drops` | counter | `ThrottlingMiddleware.__call__` | Messages rejected because a user exceeded `RATE_LIMIT_MAX_REQUESTS` in the window. | Low / flat. | Sustained climb → a flood/abuse source, or a too-tight limit for real usage. |
| `queue.drops` | counter | `UserTaskManager.enqueue_message` | Messages dropped because a conversation's queue hit `MAX_QUEUED_MESSAGES`. | ~0. | Rising → a conversation **can't drain in time** (replies slower than inflow → a ceiling signal). |
| `conversations.active` | gauge | `get_state` (create) + `_evict_idle` | Resident `UserState` count = the active working-set size. | Stable, tracks concurrency. | Climbing without leveling → more concurrent load than the loop can comfortably serve. |
| `extraction.runs` | counter | `run_extractor` (past the guard) | Background extraction runs that actually proceeded (skipped runs are not counted). | Proportional to chat volume. | Spiking far above chat volume → thrashing / a misconfigured threshold. |
| `compression.runs` | counter | `run_compressor` (past the cooldown/lock guard) | Background compression runs that actually proceeded. | Occasional, gated by `COMPRESSION_COOLDOWN_SECS`. | Frequent → memory budget too tight (see tuning). |
| `audit.write.latency` | timer *(optional)* | `llm_service._log_llm_call` | Audit-write lag to `llm_audit_log` (fire-and-forget, off the hot path). | Low; **may be absent** if audit instrumentation is not wired or no audit write has occurred yet. | Rising → MongoDB write pressure. |

> **Note on `audit.write.latency`:** this timer is optional. If it is not present in a snapshot,
> it simply means no audit write has been observed (or the optional instrumentation isn't
> enabled) — its absence is not an error. The summary and `/metrics` output degrade gracefully
> when a metric is missing.

### `snapshot()` shape

A snapshot is a plain dict with three well-formed sections (empty sections when nothing has been
recorded). Timers carry a derived `avg = sum / count`:

```json
{
  "counters": { "llm.reply.calls": 12, "llm.reply.success": 11, "llm.reply.failure": 1,
                "throttle.drops": 3, "queue.drops": 0,
                "extraction.runs": 2, "compression.runs": 1 },
  "gauges":   { "conversations.active": 7 },
  "timers":   { "llm.reply.latency": { "count": 12, "sum": 18.4, "max": 3.1, "avg": 1.533 } }
}
```

[Back to top](#table-of-contents)

---

## The in-memory metrics registry

Source: [`app/services/metrics.py`](../../app/services/metrics.py). A single process-wide
singleton, `metrics`, is the only thing callers touch.

**API surface:**

| Method | Purpose |
| :--- | :--- |
| `incr(name, n=1)` | Add `n` (default 1) to a counter; auto-creates at 0 on first use. |
| `set_gauge(name, value)` | **Replace** the gauge's value (does not accumulate). |
| `observe(name, value)` / `record_latency(name, seconds)` | Feed a timer aggregate: `count += 1`, `sum += value`, `max = max(prev, value)`. |
| `timer(name)` | Context manager that records the wrapped block's wall-clock duration **exactly once**, in a `finally` — so it records even when the block raises, and the exception propagates unchanged. |
| `record_llm(call_type, *, ok, latency)` | Convenience that maps a `call_type` to `llm.<type>.calls` + `.success`/`.failure` + `.latency` in one call. |
| `snapshot()` | Return the `{counters, gauges, timers}` dict (timers include derived `avg`). |
| `reset()` | Clear all state (used for test isolation). |

**Design properties that matter operationally:**

- **Stdlib only.** Built on `threading`, `time`, and `contextlib` plus `loguru` for debug logs —
  no third-party metrics dependency.
- **Never raises into a caller.** Every mutator wraps its body in `try/except` and logs at debug
  on failure. A metrics error can never break a reply, a drop decision, or a background job.
- **Cheap & atomic.** Each mutation takes a brief `threading.Lock` so a record is applied
  atomically; the lock is uncontended on a single event loop.
- **Bounded.** Callers only ever use the fixed metric set above, so registry memory cannot grow
  without limit. Unknown names auto-create empty rather than raising, but no caller uses them.
- **Not a server.** Again: this registry is in-memory and resets on process restart. The external
  Prometheus/OTel sink is the future Phase 12 step, not this.

[Back to top](#table-of-contents)

---

## Health & readiness helpers

Source: [`app/services/health.py`](../../app/services/health.py). Two cheap probes plus a shared
compact summary formatter over `metrics.snapshot()`.

### `liveness()`

Synchronous, **performs no I/O**. Returns:

```python
{ "status": "ok", "uptime_secs": 1234.5, "summary": { ... } }
```

- `uptime_secs` is measured from `_PROCESS_START`, captured once at module import.
- `summary` is the compact view (total LLM calls, reply `avg`/`max` latency, throttle/queue
  drops, active conversations, extraction/compression runs) shared by the command and the logger.
- On any unexpected internal error it degrades to `{"status": "degraded"}` rather than raising.

### `readiness(db)`

Asynchronous. Runs a **single** MongoDB `ping` (delegated to
[`connection.ping_db`](../../app/database/connection.py)) and **never raises**:

```python
{ "ready": True,  "mongo": "ok" }                                   # ping succeeded
{ "ready": False, "mongo": "error", "reason": "<str>" }             # any failure, incl. timeout
```

It catches everything — including server-selection timeouts — so a degraded database surfaces as
a `ready: False` report instead of an exception.

[Back to top](#table-of-contents)

---

## The optional periodic metrics logger

Source: `start_metrics_logger()` in [`app/services/health.py`](../../app/services/health.py),
wired from [`main.py`](../../main.py) after `init_db()`.

- Controlled by **`METRICS_LOG_INTERVAL_SECS`** (see [configuration.md](configuration.md)).
  Default `0.0` = **disabled** (no task is started; a harmless no-op).
- When the interval is `> 0`, a single background task logs one compact summary line per interval:

  ```
  [metrics] {'llm_calls_total': 42, 'reply_latency_avg': 1.53, 'reply_latency_max': 3.1,
             'throttle_drops': 0, 'queue_drops': 0, 'conversations_active': 7,
             'extraction_runs': 3, 'compression_runs': 1}
  ```

- It performs **no DB or LLM call** — it only formats the in-memory snapshot.
- Each iteration is wrapped so an error is logged and the loop continues; it never crashes the
  process. Cancellation (shutdown) exits the loop cleanly.

This gives you a **time series in the logs** without anyone having to issue a command — useful for
spotting trends (e.g. reply latency creeping up over an hour).

[Back to top](#table-of-contents)

---

## Admin commands: `/health` and `/metrics`

Source: [`app/handlers/commands.py`](../../app/handlers/commands.py). Both are registered as
commands (never treated as conversation) and reply with plain text built only from the in-memory
snapshot plus, for `/health`, a single Mongo ping — **no LLM call**.

### `/health`

Combines `liveness()` + `await readiness(db)` into a readable report:

```
🩺 ThinkMate health
status: ok
uptime_secs: 1234.5
readiness: mongo: ok (ok)

metrics summary:
  llm_calls_total: 42
  reply_latency_avg: 1.53
  reply_latency_max: 3.1
  throttle_drops: 0
  queue_drops: 0
  conversations_active: 7
  extraction_runs: 3
  compression_runs: 1
```

If the Mongo ping fails, the readiness line degrades (e.g. `mongo: degraded (<reason>)`) instead
of erroring — the report still renders.

### `/metrics`

Dumps the raw snapshot summary (all counters, gauges, and timers) under the **same**
authorization rule as `/health`:

```
📊 ThinkMate metrics

counters:
  llm.reply.calls: 42
  ...
gauges:
  conversations.active: 7
timers:
  llm.reply.latency: count=42 avg=1.53 max=3.1
  ...
```

### Authorization: the `ADMIN_USER_IDS` gate and the DM-only default

Both commands pass through `_admin_allowed(message)`:

- **When `ADMIN_USER_IDS` is non-empty** (a comma-separated list of Telegram user ids; see
  [configuration.md](configuration.md)): only those `from_user.id`s are honored. Everyone else is
  silently declined (fail closed — no report leaks).
- **When `ADMIN_USER_IDS` is unset or empty** (the default): the command is honored **only in
  private chats (DMs)**. This safe default ensures a status report is **never broadcast into a
  group**.

To lock the commands to specific operators in production, set `ADMIN_USER_IDS` to their numeric
Telegram ids. To keep the casual DM-only behavior, leave it unset.

[Back to top](#table-of-contents)

---

## Reading the `llm_audit_log`

When you need per-call detail (the exact prompt, raw output, parsed JSON, status, and error) the
in-memory metrics aren't enough — that's what the `llm_audit_log` collection is for. Its full
schema lives in [database.md](database.md#3-llm_audit_log-collection).

**Indexes that make reads fast:**

- A **compound index** `("user_id", 1), ("timestamp", -1)` — built for the canonical access
  pattern: *filter by `user_id`, sort by `timestamp` descending* (newest first).
- A **TTL index** on `("timestamp", 1)` with `expireAfterSeconds = AUDIT_LOG_RETENTION_DAYS × 86400`,
  so entries auto-expire after the retention window (default 30 days; see
  [configuration.md](configuration.md)). This bounds storage growth — old entries vanish on their
  own, so a query only ever sees recent history.

**Example: the latest 20 LLM calls for one user** (matches the compound index exactly — filter on
`user_id`, sort on `timestamp` descending):

```python
cursor = (
    db["llm_audit_log"]
    .find({"user_id": 12345678})
    .sort("timestamp", -1)
    .limit(20)
)
async for entry in cursor:
    print(entry["timestamp"], entry["call_type"], entry["status"])
```

Equivalent in the `mongosh` shell:

```javascript
db.llm_audit_log
  .find({ user_id: 12345678 })
  .sort({ timestamp: -1 })
  .limit(20)
```

**Narrowing further:**

- Only failures for a user: add `"status": "failed"` to the filter — useful when
  `llm.<type>.failure` counters are climbing and you want the tracebacks.
- By call type: add `"call_type": "chat_reply"` (or `"memory_extraction"`,
  `"group_memory_extraction"`, `"memory_compression"`).

Always lead the filter with `user_id` and sort by `timestamp` descending so the query rides the
compound index instead of scanning. Because the TTL prunes old rows, the working set stays small.

[Back to top](#table-of-contents)

---

## Recognizing the LLM-throughput ceiling

The single-instance ceiling is almost always **LLM throughput** (see
[performance_and_scaling.md](performance_and_scaling.md#the-single-instance-ceiling)). The metrics
above are exactly the saturation signals that tell you when you're approaching it. Watch for these
**together** — any one in isolation can be noise; sustained movement across several is the tell:

| Signal (metric) | What it looks like near the ceiling |
| :--- | :--- |
| `llm.reply.latency` (`avg` and `max`) | **Rising** — the LLM endpoint is taking longer to answer, so each reply call queues behind the last. This is the primary signal. |
| `queue.drops` | **Climbing** — conversations can't drain within their batch budget because replies are slow; messages pile up past `MAX_QUEUED_MESSAGES` and get dropped. |
| `throttle.drops` | **Climbing** — often a flood/abuse source, but combined with the above it can mean genuine load is outpacing the rate limit. |
| `conversations.active` | **Growing and not leveling off** — more concurrent working set than the single event loop can comfortably serve. |
| `llm.*.failure` counters | **Rising share of `.calls`** — the endpoint is shedding load (timeouts, 429s, 5xx), a classic saturation symptom. |

**How to confirm** which of the three documented ceilings you've hit
([the single-instance ceiling](performance_and_scaling.md#the-single-instance-ceiling)):

1. **LLM endpoint saturated** (the usual case): `llm.reply.latency` up *and* `queue.drops` up
   *and/or* `llm.reply.failure` up. The fix is a **faster/parallel LLM endpoint**, not more bot
   replicas.
2. **Event loop can't drain batches**: `conversations.active` high with latency rising even though
   the LLM endpoint itself is responsive. This is the rarer "scale horizontally" trigger.
3. **MongoDB pressure**: `audit.write.latency` rising (when present), or readiness pings getting
   slow/flaky. Check the Atlas tier IOPS/connections.

[Back to top](#table-of-contents)

---

## Tuning budgets & batching in response

Once you've recognized the ceiling, these knobs (full reference in
[configuration.md](configuration.md)) let you trade responsiveness, cost, and load. The
priority order is always **responsiveness → robustness → minimize LLM calls** (see
[performance_and_scaling.md](performance_and_scaling.md#design-goals--priorities)).

**To absorb bursty load and cut reply-call volume** (fewer, fatter batches → fewer LLM calls):

| Knob | Effect | Direction when saturating |
| :--- | :--- | :--- |
| `MESSAGE_BATCH_DELAY_SECS` | How long to wait after a user's last message before replying. | **Raise** (e.g. `2.5`) to coalesce rapid-fire messages into one reply call. |
| `MAX_BATCH_DELAY_SECS` | Hard deadline from the first message in a batch. | **Raise** modestly to allow larger batches, but keep responsiveness acceptable. |
| `MAX_QUEUED_MESSAGES` | Per-conversation queue cap before `queue.drops` increments. | **Raise** to tolerate brief inflow spikes without dropping; **don't** raise so far it masks a real ceiling. |

**To reduce background LLM load** (extraction/compression compete with replies for the endpoint):

| Knob | Effect | Direction when saturating |
| :--- | :--- | :--- |
| `CHAT_BUFFER_MAX_CHARS` | Buffer size before extraction runs. | **Raise** to run extraction less often (fewer `extraction.runs`), trading memory freshness for fewer calls. |
| `USER_MEMORY_BUDGET_CHARS` | Compiled-memory budget before compression runs. | **Raise** to compress less often (fewer `compression.runs`). Keep ≥ ~600 (the empty template alone is ~380 chars). |
| `COMPRESSION_COOLDOWN_SECS` | Minimum gap between compression runs per user. | **Raise** to space compression out further under load. |
| `LLM_EXTRACTION_MODEL` | Model used for extraction/compression. | Point at a **smaller/faster** model so background work doesn't starve replies. |

**To shed abusive load** (when `throttle.drops` is climbing from a flood, not genuine traffic):

| Knob | Effect | Direction |
| :--- | :--- | :--- |
| `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECS` | Per-user rate limit. | **Tighten** to reject floods sooner. |
| `MAX_INPUT_CHARS` | Inbound message size cap. | **Lower** to block pasted-log/essay dumps that inflate token cost. |

**The real fix for an LLM endpoint ceiling** is a faster or parallel endpoint, or a smaller
reply model — the knobs above buy headroom but don't change the endpoint's throughput. Only scale
the bot horizontally when the **event loop or MongoDB** is the proven bottleneck (signals 2 and 3
above), following the
[migration path](performance_and_scaling.md#horizontal-scale-migration-path-future).

[Back to top](#table-of-contents)

---

## Related docs

- [README.md](../../README.md) — project overview and entry point.
- [architecture.md](../architecture.md) — system topology and data flow.
- [performance_and_scaling.md](performance_and_scaling.md) — the capacity model, hot-path
  invariants, saturation signals, and the future metrics-sink step.
- [configuration.md](configuration.md) — every tuning knob, including `ADMIN_USER_IDS` and
  `METRICS_LOG_INTERVAL_SECS`.
- [database.md](database.md) — the `llm_audit_log` schema, the compound index, and the TTL.
- [project_plan.md](../project_plan.md) — phase-by-phase plan (Phase 10 = observability & ops).

[Back to top](#table-of-contents)
