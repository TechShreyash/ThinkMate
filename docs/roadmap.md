# Capabilities and roadmap

This page separates what exists in the repository from possible improvements. It replaces the old phase-by-phase build checklist, which mixed implemented behavior with acceptance targets that had not been demonstrated. Proposed work below has no committed schedule.

## Implemented capabilities

| Area | Current implementation | Reference |
| --- | --- | --- |
| Telegram | Long polling, text replies, batching, command menus, membership greetings, and reactions. | [Telegram](development/telegram_bot.md) |
| Groups | Explicit/implicit addressing, ambient gates, spam heuristics, member affinity, and group controls. | [Groups](development/group_chat.md) |
| Persistence | MongoDB profiles, bounded chat buffers, group-member settings, audit entries, and persisted metrics. | [Database](development/database.md) |
| Memory | Structured extraction, mood history, compression, and periodic consolidation with insights. | [Memory](development/memory_engine.md) |
| Engagement | Onboarding, temporal context, and optional proactive check-ins with opt-out and quiet hours. | [Telegram](development/telegram_bot.md) and [memory](development/memory_engine.md) |
| Operations | Health/metrics commands, rotating logs, optional forwarding, and Docker/Compose configuration with runtime dependencies installed from `uv.lock`. | [Operations](operations.md) and [observability](development/observability.md) |
| Verification | Unit, regression, and property tests with database mocks; a separate manual live LLM script. | [Testing](development/testing_guide.md) |

These capabilities do not imply that every historical acceptance target was met. The repository does not provide a production load benchmark, a formal data-isolation guarantee, or universal LLM endpoint compatibility.

## Proposed priorities

These are recommendations based on the present implementation, not features already shipped.

| Priority | Improvement | Why it matters |
| --- | --- | --- |
| 1 | Define personal/group memory-sharing rules and add a private scope for profile inspection. | The group prompt currently includes the triggering sender's global profile, and `/profile` can publish it to the group. |
| 1 | Define complete deletion/export semantics and coordinate resets with background work. | Current reset leaves group records, audit/log data, optional Telegram backups, and work in flight. |
| 1 | Coordinate profile writes across DM and group conversations. | Per-chat memory locks do not serialize all writers to a participant's global profile. |
| 1 | Reject destructive empty memory replacements and strengthen schema/content checks. | A syntactically valid empty result can clear arrays; parse success alone does not prove a useful extraction or compression. |
| 2 | Validate configuration ranges, modes, and relationships at startup. | Current type parsing does not reject every unusable or unsafe combination. |
| 2 | Add bounded global LLM concurrency and explicit task draining at shutdown. | Per-conversation guards do not impose a process-wide request ceiling or ensure all detached work completes. |
| 2 | Add realistic MongoDB/provider integration checks and measured load tests. | In-memory fixtures cannot validate all database, transport, or deployment behavior. |
| 3 | Rank memories by relevance and recency. | The current reply prompt compiles stored memory without semantic selection. |
| 3 | Evaluate retrieval over trimmed conversation history. | Details not extracted into memory may be lost once old buffer entries are removed. |
| 3 | Design distributed state and update delivery before supporting replicas. | Polling, queues, locks, caches, and cooldowns currently assume one process. |

The last two memory-quality proposals were already deferred in the previous plan. There is no embedding store, relevance ranker, webhook deployment, or external metrics server in the current implementation.

Before implementing an item, describe its expected behavior, inspect the relevant [subsystem guide](README.md), add meaningful regression coverage, and update the documentation with the resulting behavior. See [Contributing](../CONTRIBUTING.md).
