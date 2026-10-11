# Current safeguards and known limitations

This page replaces the historical production-hardening checklist with a description
of the current implementation. The filename is retained for existing links. Checked
items in old plans, deployment anecdotes, and provider timing samples are not evidence
of production readiness or load capacity. Use [testing](testing_guide.md) to validate a
change and [observability](observability.md) to inspect a running deployment.

## Implemented safeguards

| Area | Current behavior | Source |
| --- | --- | --- |
| Startup | Numeric bounds/relationships validated; Mongo ping and audit indexes initialized; missing persona warning/fallback; explicit webhook/backlog removal | [`config.py`](../../app/config.py), [`main.py`](../../main.py), [`connection.py`](../../app/database/connection.py), [`chat_manager.py`](../../app/services/chat_manager.py) |
| LLM retries | Shared client, SDK retries disabled; bounded exponential retries for transient errors | [`llm_service.py`](../../app/services/llm_service.py) |
| Structured output | Reply/reaction bundle; memory Pydantic validation; configurable JSON-object/native parsing with fallback | [`llm_service.py`](../../app/services/llm_service.py), [`schemas.py`](../../app/services/schemas.py) |
| Input routing | Sender/non-conversational guards, startup-stale cutoff, per-user rate limit, text-length limit, slash-command skip | [`middlewares.py`](../../app/handlers/middlewares.py), [`messages.py`](../../app/handlers/messages.py) |
| Reply delivery | Character cap, Telegram-safe chunks, group reply fallback, normalized reactions, blocked/restricted handling | [`telegram_text.py`](../../app/services/telegram_text.py), [`user_task_manager.py`](../../app/services/user_task_manager.py) |
| Batching/state | Per-chat pending cap, maximum-age coalescing timer, consecutive sender ordering, chat/memory locks, idle state/cache pruning | [`user_task_manager.py`](../../app/services/user_task_manager.py) |
| Buffers | Mongo `$push`/`$slice` message cap; extraction-snapshot message-ID removal; conditional legacy-array replacement | [`models.py`](../../app/database/models.py) |
| Memory updates | Normalized dedup/matching; snapshot-conditional profile writes; bounded extraction-save conflict retries; stale/failing maintenance replacements skipped | [`models.py`](../../app/database/models.py), [`memory_extractor.py`](../../app/services/memory_extractor.py), [`memory_compressor.py`](../../app/services/memory_compressor.py), [`memory_consolidator.py`](../../app/services/memory_consolidator.py) |
| Memory maintenance | Compression cooldown, single-read/at-most-one-write budget shedding, capped mood/insight lists | [`memory_compressor.py`](../../app/services/memory_compressor.py), [`models.py`](../../app/database/models.py) |
| Operations | Best-effort audit writes, attempted TTL retention, metrics checkpointing, gated reports, loop-protected forwarding | [Observability runbook](observability.md) |
| Group behavior | Explicit/implicit/ambient gates, mass-tag/burst checks, per-member and group mode controls | [Group chat guide](group_chat.md) |
| Proactive behavior | Inactivity/attempt-gap/item-count checks, opt-out, UTC quiet hours, unanswered-streak pause; permanent delivery failures disable outreach, transient failures preserve opt-in | [`health.py`](../../app/services/health.py), [`models.py`](../../app/database/models.py) |
| Shutdown | Owned conversation/audit/error tasks drain within bounded windows, final metrics/log flushes, client closure, and startup-failure cleanup | [`main.py`](../../main.py), [`user_task_manager.py`](../../app/services/user_task_manager.py), [`llm_service.py`](../../app/services/llm_service.py) |

These safeguards describe code paths and their scope. They do not guarantee that the
LLM returns accurate memories, that network operations succeed, or that storage stays
within a fixed global limit.

## Failure policies and data loss

**Extraction exhaustion trades memory capture for bounded buffers.** The pipeline
tries up to three times, rereading the buffer, then trims older unextracted messages
if all attempts fail. The hard cap can also discard older messages before extraction
succeeds. Those turns will not become durable memories. A valid empty extraction is a
successful call and may trim without adding memories. See [memory engine](memory_engine.md).

**Budget shedding is lossy.** Compression and consolidation keep existing memory on
LLM failure; successful results can replace details. The deterministic budget pass
then removes entries from the front of the event, belief, and fact arrays in that order; it does not sort them by event date. It preserves other
metadata, so a very small budget may remain unsatisfied. A memory-character budget is
not a complete prompt-token or Mongo document-size cap.

**Atomic trim has a defined scope.** New messages carry unique IDs. Extraction removes
only messages in its observed snapshot, preserving concurrent appends and avoiding
over-trim after hard-cap rotation. Legacy arrays without IDs use compare-and-set
replacement; concurrent changes skip trimming so those messages can be reconsidered.

**Background work is best-effort.** Pending updates/jobs are not durably queued for
replay. Shutdown drains active update handlers for up to 10 seconds, owned reply/memory
tasks for another 10 seconds, audits for up to 5 seconds, and error forwarding for up to
5 seconds before cancelling outstanding
tasks. Separate asynchronous cleanup steps have a 15-second outer timeout. Crashes,
forced termination, and drain timeouts can lose in-flight work and metrics since the
last successful checkpoint.

## Privacy and operator access

Normal LLM audits retain lengths rather than full prompts/completions, but errors
store tracebacks that can contain provider/validation details. Local logs include
routing identifiers/names and message previews. A configured Telegram logs destination
receives actual extracted memory summaries and best-effort reset backups containing
profile/buffer data. Audit TTL does not erase those copies.

An empty `ADMIN_USER_IDS` permits ops reports to **every DM user**. A non-empty list
restricts sender IDs but permits their reports in groups. `/profile` and confirmed
`/reset` operate on the invoking user's ID; group transcripts and shared/member
records are separate. Reset does not erase audits, all group references, local logs,
or Telegram copies. Reset success text mentions recovery only after a backup upload
succeeds. There is no built-in restore command.

Review the [observability runbook](observability.md) and [database guide](database.md)
when changing logging or deletion behavior.

## Concurrency and scaling limits

The supported topology is one long-polling process. Locks/queues/caches, gate windows,
and compression cooldowns are process-local. Group extraction writes participant
profiles under a group lock, so it is not serialized with the participant's DM or
another group's work. Profile snapshot checks prevent stale memory overwrites;
extraction retries conflicts and maintenance skips stale replacements. Generation tokens
also reject results when the captured profile was deleted or recreated. DM extraction
captures the token before its transcript snapshot; group tokens are captured after
reading the segment. Confirmed reset stops prior owned DM work and holds new DM messages
through backup/deletion. New activity or extraction from retained shared group history
can still create profiles again. There are no persistent
deletion tombstones or complete-erasure guarantees.

Queue caps are per chat; active states, LLM requests, due-user scans, and log-forwarding
buffers lack a global hard cap. The batching maximum bounds the coalescing timer,
not end-to-end response time. Readiness checks Mongo connectivity only;
a green report says nothing about LLM/Telegram delivery. No repository benchmark
substantiates a 50,000-user capacity claim. See
[performance and scaling](performance_and_scaling.md).

## Practical validation before a runtime change

1. Read the relevant subsystem and its source before changing retry, trim, reset, or
   profile replacement semantics.
2. Run focused regression tests with the `dev` extra and development configuration;
   keep LLM/Telegram/background calls mocked. See [testing](testing_guide.md).
3. Validate live endpoint/model capabilities separately using an isolated database.
   Provider compatibility and historic latency samples are not transferable guarantees.
4. Observe startup warnings, actual TTL indexes, report authorization, drop counts,
   and slow-path behavior in a development deployment.
5. When changing concurrency, test simultaneous conversation/member/profile operations
   and failure during writes/delivery, not just a single sequential happy path.

[Project roadmap](../roadmap.md) records remaining work; this page records
current safeguards and limitations. [Documentation index](../README.md).
