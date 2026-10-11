# Observability and operations

ThinkMate exposes Telegram `/health` and `/metrics` commands, cumulative metrics,
local logs, LLM audit records, and optional forwarding to a Telegram logs destination.
This runbook describes what those signals measure and their privacy/diagnostic limits.
See [configuration](configuration.md) for environment values and
[performance and scaling](performance_and_scaling.md) for capacity constraints.

## Startup and readiness

[`main.py`](../../main.py) pings MongoDB before polling and then initializes audit
indexes. An unreachable database or failure of the compound-index creation stops
startup. Audit TTL-index failure only logs a warning. Metrics restore is best-effort.
Persona validation warns on a missing file and allows a fallback persona. There is
no startup LLM request that verifies models or structured-output compatibility.

[`health.py`](../../app/services/health.py) provides two probes:

| Probe | What it checks | Limit |
| --- | --- | --- |
| `liveness()` | In-process uptime and a compact metrics summary; no I/O | Does not prove Telegram polling, LLM availability, or successful delivery. |
| `readiness(db)` | Calls the singleton Mongo client admin `ping` | The argument is currently unused. Does not test writes, indexes, LLM, or Telegram. Errors return `ready: false` with a reason. |

There is no HTTP health endpoint or Prometheus/OpenTelemetry exporter. `/health` is
an HTML Telegram report combining these probes. `/metrics` renders task-level LLM
outcomes, counters, gauges, and timers from the in-memory registry with no LLM call.

## Who can read reports

Both commands use the gate in [`commands.py`](../../app/handlers/commands.py):

- Empty/unset `ADMIN_USER_IDS` allows **any private-chat user** to read reports.
- A non-empty set allows only listed sender IDs, **in private chats or groups**.
- Senderless and unauthorized requests are silently ignored.

The default protects against broadcasting group reports, but does not restrict reports
to operators. Set explicit operator IDs when that restriction is needed; issue reports
in a DM when they contain information you do not want posted to a group. Command
renaming/disabling uses the same gate. See [command configuration](configuration.md#command-environment-variables).

## Metric catalog

[`metrics.py`](../../app/services/metrics.py) holds one thread-safe process registry.
Counters accumulate, gauges replace the latest value, and timers store `count`, `sum`,
`max`, and derived `avg`. Failures are swallowed so instrumentation does not break
application work. Names are created on demand; current callers use a small fixed set.

Each LLM family has `.calls`, `.success`, `.failure`, and `.latency`:

| Prefix | Service call type |
| --- | --- |
| `llm.reply` | `chat_reply` |
| `llm.extraction` | `memory_extraction` |
| `llm.group_extraction` | `group_memory_extraction` |
| `llm.compression` | `memory_compression` |
| `llm.memory_consolidation` | `memory_consolidation` |
| `llm.proactive_checkin` | `proactive_checkin` |

A `.calls` count represents a completed service operation, **not every HTTP attempt**.
Retries and native-parse fallback occur inside that operation; latency includes their
waits. Successful reply parsing can fall back to raw text, and a successful check-in
request can decline to send anything. These counters therefore do not prove delivery
or semantic reply quality.

| Metric | Meaning |
| --- | --- |
| `throttle.drops` | Per-user sliding-window rate-limit rejects. |
| `throttle.stale_dropped` | Messages older than the startup cutoff. |
| `queue.drops` | Messages rejected at a conversation's pending queue cap. |
| `conversations.active` | Resident conversation-state count, including retained idle state; not an online-user count. |
| `extraction.runs` | Entry into an extraction pipeline, including one that later finds nothing to process. |
| `compression.runs` | Entry into the compression pipeline after scheduler/cooldown gating. |
| `consolidation.runs` / `.success` / `.failure` | Consolidation pipeline entries and apply outcomes; separate from LLM call outcomes. |
| `proactive.runs` | Scans entered, including scans skipped for quiet hours. |
| `proactive.sent` | Check-ins delivered and subsequent bookkeeping completed. |
| `proactive.skipped` | Due-user attempts whose generation returned no usable text. |
| `proactive.failed` | Failed attempts, including delivery or per-user processing errors. |

There is currently **no `audit.write.latency` instrumentation**, no queue-wait/delivery
latency, no percentile histogram, and no global LLM concurrency gauge. Use logs and
provider/database diagnostics for those questions.

Example registry snapshot:

```json
{
  "counters": {"llm.reply.calls": 12, "llm.reply.success": 11, "llm.reply.failure": 1},
  "gauges": {"conversations.active": 7},
  "timers": {"llm.reply.latency": {"count": 12, "sum": 18.4, "max": 3.1, "avg": 1.533}}
}
```

## Persistence and periodic logging

The registry is restored from the `metrics_state` singleton document
(`_id: "metrics:singleton"`) at startup, checkpointed every
`METRICS_PERSIST_INTERVAL_SECS` (default 300), and flushed during the polling shutdown
path. Setting the interval to `<= 0` disables only periodic checkpoints. All persistence
is best-effort: a crash can lose changes after the last successful checkpoint.

Counters and timer aggregates continue across restarts when restore succeeds; uptime
starts fresh on import. Gauges restored from a checkpoint can be stale until their
owners update them. `load_state()` adds counters/timers to existing state, so repeated
loads would double-count; normal startup calls it once. Multiple bot instances would
overwrite the same singleton and are unsupported.

`METRICS_LOG_INTERVAL_SECS > 0` enables periodic `[metrics]` summary lines without a
DB/LLM request. The default `0` disables this logger. Snapshot averages/maxima are
cumulative, not a recent-window view; compare successive checkpoints or log samples
before inferring trends. [`health.py`](../../app/services/health.py) owns both loops.

## Logs and stored content

| Destination | Content | Retention/control |
| --- | --- | --- |
| Console | INFO and above, including actionable group routes with message previews | Process/service output configuration. |
| `logs/bot.log` | DEBUG and above, including routing IDs/names, message previews, and errors | 10 MB rotation and 10-day retention in [`app/__init__.py`](../../app/__init__.py). |
| `llm_audit_log` | User/conversation ID, call type, timestamp, status, input/output lengths, error traceback | Best-effort TTL index; code default 30 days. |
| Telegram `LOGS_CHANNEL_ID` | Lifecycle notices, extraction-update summaries containing memory content, selected WARNING+ records; diagnostic traces if enabled | Telegram destination access/retention, independent of audit TTL. |
| Telegram reset backup | JSON export of the user's profile and buffer before confirmed `/reset` | Best-effort upload; independent of MongoDB deletion. |

The normal audit `inputs` and `outputs` contain character lengths, not full prompts or
completions. Its `error` field contains a traceback; provider/validation errors can
include sensitive fragments. Local route traces contain text previews even when
`FORWARD_DIAGNOSTICS=false`. That switch controls extra Telegram forwarding, not local
log creation. Treat logs, destination membership, and backups as access to user data.

[`log_forwarder.py`](../../app/services/log_forwarder.py) sends directly at low volume
and groups bursts into a periodically uploaded `logs.txt`. Buffers have no hard size
cap, and failures can retain buffered content. Missing destination or bot disables
sending. Forwarding rejects events originating in the destination chat to avoid loops.
[`error_log_sink.py`](../../app/services/error_log_sink.py) filters self-forwarded and
common permission/blocked noise; explicit operational events use a separate path.

Reset backup delivery is best-effort and deletion proceeds when upload fails. The
current reset confirmation text claims a backup was saved even when forwarding was
unset/unsuccessful; verify the actual upload before assuming recovery is available.
`/reset` does not erase audit rows, shared group transcripts, or Telegram/log copies.

## Inspecting LLM audit records

[`llm_service.py`](../../app/services/llm_service.py) schedules audit inserts separately
from reply generation. They can fail or be lost at shutdown; they are diagnostic data,
not a complete transaction ledger. There is no final audit-task drain in `main.py`.

Use the compound `(user_id ascending, timestamp descending)` index for recent calls:

```javascript
// In mongosh, after selecting the configured database:
db.llm_audit_log.find({user_id: 12345678}).sort({timestamp: -1}).limit(20)
// Only failures:
db.llm_audit_log.find({user_id: 12345678, status: "failed"})
  .sort({timestamp: -1}).limit(20)
// Verify audit retention was configured:
db.llm_audit_log.getIndexes()
```

Group reply/extraction audits use the group chat ID in `user_id`. Status is `success`
or `failed`; call types are listed above. See [database](database.md) for schemas.
TTL is index-dependent and asynchronous; changing the environment does not guarantee
an existing index is altered successfully. Check startup warnings and the actual index.

## Troubleshooting

| Symptom | First checks | Useful change |
| --- | --- | --- |
| No polling after startup | Mongo connection/index errors, token construction, startup logs | Correct settings before changing concurrency. |
| `/health` ready but replies fail | LLM model/URL/auth, reply failure counts, provider logs | Test endpoint capability in an isolated environment. |
| Slow replies and queue drops | Reply latency trend, actual provider throughput, loop/database latency | Adjust batching cautiously; lower competing scheduler volume if needed. |
| Many throttle drops | Identify users/chats and rate-limit values | Tighten for abusive bursts or relax if ordinary typing is rejected. |
| Frequent compression | Profile budget, retained metadata, cooldown | Increase practical budget/cooldown after examining profile sizes. |
| No proactive check-ins | Quiet UTC window, interaction age, opt-out/streak, item count, model decline | Check eligibility; missing sends need not be failure. |
| Audit retention absent | Startup TTL warning and `getIndexes()` | Correct/reconcile the existing index with a database maintenance change. |
| Logs destination floods | Diagnostic flag, repeated warnings, forwarding buffer | Turn off verbose forwarding and fix the underlying failure. |

These are hypotheses to investigate, not proof of a particular bottleneck. Current
metrics cannot distinguish upstream queuing, network delay, event-loop pressure, and
Mongo latency on their own. See [performance and scaling](performance_and_scaling.md)
for measurement and the limitations of running multiple replicas.

[Documentation index](../README.md)
