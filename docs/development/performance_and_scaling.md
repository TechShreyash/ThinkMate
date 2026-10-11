# Performance and scaling

ThinkMate runs one aiogram long-polling process, keeps conversation state in memory,
and stores profiles/buffers in MongoDB. This page describes implementation costs and
constraints. The repository does not contain a sustained-load benchmark that proves a
particular user capacity; historical claims of 50,000+ supported users are not a
capacity guarantee. See [architecture](../architecture.md) for the system flow and
[observability](observability.md) for available measurements.

## Reply work and request counts

The typical private-chat pipeline in
[`chat_manager.py`](../../app/services/chat_manager.py) is:

1. Append combined user text and return the capped buffer in one MongoDB operation.
2. Read memory-item count to choose the extraction threshold; schedule extraction when due.
3. Load the profile and compile memory, check the persona cache, and update/read the
   prior interaction timestamp.
4. Request a reply bundle containing reply text and an optional reaction.
5. Append the assistant text and schedule compression when needed.
6. Deliver the reply, optionally apply a Telegram reaction, and stop typing.

The successful `handle_message` DM path currently uses **five MongoDB operations**:
two buffer appends, an item-count read, a profile read, and an interaction-timestamp
update/read. That count excludes routing, command work, background jobs, audits,
metrics persistence, and Telegram requests. There is no enforced three-query budget.

A DM batch normally uses one reply service operation. A group batch is split by sender
in [`user_task_manager.py`](../../app/services/user_task_manager.py), so it can use one
reply operation **per distinct sender**. Group routing also reads the enable/mode
settings, refreshes sender identity, consults member affinity, loads both group and
sender memory, and may write affinity changes. Non-addressed group traffic can still
incur database work even when no reply is generated. See [group chat](group_chat.md).

A service operation can contain multiple HTTP requests: transient retries, and for
memory calls in `native_parse` mode a fallback request. Reply and reaction share one
request format; reaction delivery itself is a Telegram API request. Typing indicators
are repeated Telegram calls while processing is pending.

## What reduces cost

| Mechanism | What it saves | Trade-off |
| --- | --- | --- |
| Per-conversation batching | Several quick messages from one sender can share a reply operation | Adds idle wait; group senders remain separate. |
| Reply/reaction bundle | No second LLM reaction operation | JSON parsing can fall back to raw output. |
| Ambient/implicit gates | Most group reply decisions use local predicates before generation | Group buffering/settings/identity work still occurs. |
| Cached persona content | Reuses text until file modification time changes | Filesystem `getmtime` still runs; cache miss/change reads the file. |
| Affinity cache | Avoids member DB reads for warm entries | Cold reads/default writes and write-through changes remain. |
| Background memory jobs | Extraction/compression/consolidation do not block on the reply's direct call chain | They still compete for database, event-loop, and LLM resources. |
| Off-path audit writes | Reply does not await audit insert | Writes are best-effort, not durable. |

No explicit output-token cap or global LLM request semaphore is configured. A character
cap on delivered responses cannot limit tokens already generated. Actual inference
cost depends on input size, output size, request attempts, provider limits, and all
background operations. See [LLM integration](llm_integration.md).

## Queueing and locks

`UserTaskManager` keys state by conversation/chat ID. `chat_lock` covers the batch's
reply and delivery work; `memory_lock` coordinates extraction, compression, and
consolidation for that state. Work encountering a held memory lock is skipped rather
than put into a persistent job queue.

`MAX_QUEUED_MESSAGES` caps pending messages **per chat**, not all messages/process work.
`MAX_BATCH_DELAY_SECS` is checked on enqueue to stop repeatedly postponing a busy batch;
it is not a strict timer deadline and does not cap time waiting for a lock or upstream
reply. Slow generations can keep one conversation busy for much longer than the
batching delay. LLM timeouts apply per request and retries extend elapsed time.

Locks do not coordinate separate processes. Group extraction can write participant
profiles while holding the group's memory lock; a DM or another group touching the
same participant holds a different lock. The current design therefore does not
provide complete per-person serialization across conversations. Read-modify-write
memory updates deserve concurrency regression tests when changed.

## Bounds and remaining growth

| Structure | Current control | Limit |
| --- | --- | --- |
| Pending chat queue | `MAX_QUEUED_MESSAGES` | No global count limit or durable replay. |
| Conversation state | Idle eviction after `USER_STATE_TTL_SECS`, swept at `max(60, TTL / 2)` | Active/locked states remain; no hard maximum active-state count. Sweeper starts lazily on enqueue. |
| Throttle timestamps/warn flags | Access-driven stale pruning | Window-based bound depends on activity, not a global cap. |
| Ambient/implicit/spam gate maps | Gate pruning and task-manager sweep hooks; burst history has a per-sender cap within each chat | Not a fixed maximum chat/member count. |
| Affinity cache | Sweep prunes entries idle for `USER_STATE_TTL_SECS * 4` | Active members remain; no fixed entry cap. |
| Chat buffer | MongoDB `$slice` to `CHAT_BUFFER_HARD_CAP` messages | Count cap is not a total-character/token cap. |
| Compiled memory | Compression and deterministic removal of events, beliefs, then facts | Preserved metadata can remain over a small budget; not a database document-size bound. |
| Mood/insights | `MAX_MOOD_HISTORY` / `MAX_INSIGHTS` | Content length is not strictly bounded by list count. |
| LLM audits | Best-effort TTL index | Depends on index creation and Mongo expiry; profiles are not TTL-expired. |
| Log-forwarder burst buffer | Periodic flush to Telegram | No hard cap; failures can retain content. |

These policies reclaim idle work; they do not make memory use independent of load.
Extraction makes up to three attempts at the pipeline level, each potentially with
LLM HTTP retries. Exhaustion trims older text even when no memory was saved.
Compression/consolidation preserve existing memory on LLM failure. See
[memory engine](memory_engine.md) and [safeguards](hardening_plan.md).

## Database access

[`models.py`](../../app/database/models.py) uses IDs for most direct lookups:

| Collection | Key/access |
| --- | --- |
| `user_profiles` | User ID, or negative group chat ID for shared memory; direct reads and memory writes. |
| `chat_buffers` | Chat ID; atomic appends and timestamp-cutoff trim. |
| `chat_members` | String `chat_id:user_id`; affinity/mode reads and writes. |
| `group_settings` | Chat ID; enable and ambient-mode settings. |
| `llm_audit_log` | Generated ID; compound user/timestamp index and TTL index. |
| `metrics_state` | Fixed singleton ID; best-effort snapshot upsert. |

Consolidation and proactive due-user queries scan matching profiles and check item
counts in Python. Limits cap qualifying users returned/processed, not necessarily
all documents examined. There are no dedicated scheduler indexes beyond Mongo's
`_id` index. Assess scan work and query plans before assuming all access is one cheap
ID lookup. Collection keys describe application access, not strict tenant isolation:
participant memory is intentionally shared across that person's conversations.

[`connection.py`](../../app/database/connection.py) reuses one Motor client and sets a
10-second server-selection timeout. Driver pool options can be set in `MONGODB_URI`;
measure active queries, latency, and database limits before changing them. See
[database](database.md) for schema/index details.

## Measure before scaling

Use a development deployment and representative traffic to measure:

- Time from update reception to delivered reply, including queue/lock waits.
- LLM latency/errors, request attempts, provider rate limits, and background call volume.
- Mongo query/write latency, scheduler scans, connection usage, and document sizes.
- Event-loop delay, memory use, resident conversation state, and forwarding backlog.
- Drop counts and what fraction affect legitimate traffic.

Current timers are cumulative service-operation averages/maxima. They do not measure
end-to-end delivery, recent percentiles, provider concurrency, or audit-write latency.
Compare successive samples and provider/database diagnostics. LLM throughput is a
plausible bottleneck, but the code alone cannot establish that it is the only or first
limit. See [observability](observability.md).

To reduce competing work, adjust scheduler cadence/scan limits, extraction thresholds
(including the sparse-profile threshold), compression cooldown, and available models.
Batching can reduce burst reply volume but adds wait. Raising queue/state caps can
hide overload and increase memory use; it does not increase processing capacity.

## Horizontal scaling is future work

Do not run multiple long-polling replicas with the same bot token as a scaling tactic.
Polling ownership, process-local timers/locks/queues, timestamp sequencing, caches,
and the shared metrics singleton all need coordinated designs first.

A future implementation would need:

1. An update-ownership/transport strategy, such as authenticated webhooks with routing
   or a single polling receiver feeding durable workers.
2. Shared queues and idempotency/delivery handling; clear ordering per conversation.
3. Cross-conversation serialization for participant profiles and atomic/versioned
   memory updates, plus distributed ownership for scheduled jobs.
4. Cache invalidation and shared rate-limit/gate state where correctness requires it.
5. Per-instance metrics with an external aggregation sink, replacing singleton overwrites.
6. Load-tested database query/index changes and capacity planning; shard keys must match
   each collection's actual access pattern.

No Redis state store, webhook server, distributed lock layer, persistent job queue,
or scale-out implementation is present. This is a design checklist, not a mechanical
migration promise or a claim that existing schemas/services need no changes.

[Documentation index](../README.md)
