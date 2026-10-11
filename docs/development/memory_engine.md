# Memory engine

ThinkMate keeps recent conversation text in a chat buffer and extracts longer-lived context into MongoDB profiles. Replies use a compiled profile plus the current buffer. This is a lossy memory system: it summarizes, merges, and drops information rather than preserving a complete conversation archive.

Start with [architecture](../architecture.md) for the request flow. See [database](database.md) for stored fields, [LLM integration](llm_integration.md) for output contracts, and [configuration](configuration.md) for environment settings.

## Components

| Module | Responsibility |
| --- | --- |
| [chat_manager.py](../../app/services/chat_manager.py) | Append a responding batch, assemble context, generate and store a reply, schedule memory work. |
| [memory_loader.py](../../app/services/memory_loader.py) | Compile a profile into prompt text and report whether it exceeds the memory budget. |
| [memory_extractor.py](../../app/services/memory_extractor.py) | Extract updates from older buffer messages, then trim the buffer. |
| [memory_compressor.py](../../app/services/memory_compressor.py) | Replace an oversized profile with a condensed result and shed items if necessary. |
| [memory_consolidator.py](../../app/services/memory_consolidator.py) | Review an existing profile, merge items, and synthesize behavioral insights. |
| [user_task_manager.py](../../app/services/user_task_manager.py) | Coordinate background tasks with an in-process lock per conversation and a compression cooldown. |
| [health.py](../../app/services/health.py) | Schedule periodic consolidation and proactive check-ins. |
| [models.py](../../app/database/models.py) | Apply updates and maintain buffers in MongoDB. |

## Reply context and triggers

For a responding batch, `handle_message` appends the combined user text and builds history from the returned buffer. It schedules extraction when the sum of buffered content lengths reaches the applicable threshold. It then loads memory, assembles the persona and system prompt, generates the reply bundle, appends the assistant reply, and schedules compression if the loaded memory was oversized.

Background extraction is not awaited before generation. A reply can therefore use the profile as it existed before that extraction completed. Compression is scheduled after reply generation succeeds; it does not truncate the context of the current request.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CHAT_BUFFER_MAX_CHARS` | `4000` | Extraction trigger for established profiles; not a strict buffer character cap. |
| `NEW_USER_EXTRACTION_CHARS` | `1000` | Earlier trigger when the stored item count is below `NEW_USER_MEMORY_THRESHOLD`. Capped by `CHAT_BUFFER_MAX_CHARS`. |
| `NEW_USER_MEMORY_THRESHOLD` | `10` | Count of facts + beliefs + events used to select the earlier trigger. |
| `CHAT_BUFFER_TRIM` | `8` | Number of recent buffer entries excluded from extraction. |
| `CHAT_BUFFER_HARD_CAP` | `200` | Maximum retained entries after an append; oldest entries fall out even without extraction. |
| `USER_MEMORY_BUDGET_CHARS` | `10000` | Compression trigger based on the compiled profile text. |
| `COMPRESSION_COOLDOWN_SECS` | `300` | Minimum gap between compressor runs for an active conversation state. |

These counts apply to stored entries, which can be combined batches or assistant turns, not necessarily individual Telegram messages. The memory budget excludes the persona, system instructions, active history, and any additional group participant block. It is not a model token or total context limit.

In groups, [messages.py](../../app/handlers/messages.py) also buffers accepted text when the bot does not reply. The shared buffer and group profile use `chat_id`; each person's profile uses `user_id`. The reply prompt includes the group profile and the triggering sender's personal profile. Only the group block's size triggers compression in this reply path. See [group chat](group_chat.md) for routing and buffering details.

## Extraction

`extract_and_trim(chat_id, is_group=...)` uses the explicit Telegram chat type passed through the task manager. Direct callers that omit the hint use a fallback heuristic: more than one distinct human sender selects group extraction. That heuristic can misclassify a group with one active speaker.

Each extraction run makes up to three logical extraction attempts:

1. Read the raw buffer, including sender attribution and timestamps. Return immediately if there are no entries beyond `CHAT_BUFFER_TRIM`.
2. Select everything except the latest retained entries and render a date-stamped transcript.
3. Ask the LLM for structured updates. DM extraction also includes the current compiled profile; group extraction currently does not include existing group or participant profiles.
4. On a non-`None` result, save updates and trim the oldest selected count. A valid empty result still counts as success and the selected history is trimmed.
5. On `None`, reread the buffer for the next attempt. After three unsuccessful attempts, trim the oldest entries anyway. This intentionally loses unextracted history during sustained LLM failure.

Transport retries inside each logical attempt are separate; see [LLM integration](llm_integration.md). Database or other pipeline exceptions are logged and end the run, rather than entering the `None` retry path.

The extraction prompt asks for English memory text, preservation of proper nouns, and absolute event dates resolved against each message's UTC date. These are model instructions. Event dates are nullable strings, without a calendar-format validator.

### Group attribution

A group attempt uses one `GroupMemoryExtraction` result containing optional shared `group_extraction` and participant `updates`:

- Shared updates are saved under the group `chat_id`.
- Participant names are matched to sender IDs from the extracted segment using case folding and collapsed whitespace. Unknown names are skipped.
- If two people share a display name, the first ID seen wins. This deterministic tie-break does not establish correct identity; attribution remains ambiguous.
- A group profile marker records whether shared memory was saved and how many participant updates were saved or skipped, including on an otherwise empty successful result.
- Participant writes, the group marker, and buffer trim are separate operations. A failure can leave some updates saved while history remains available for re-extraction.

## Compiling memory

`compile_memory_text` is a pure function that renders summary, inferred gender, communication style, facts, beliefs, behavioral insights, events, current mood, and a recent mood trend. Missing fields produce empty/default sections. `compile_memory_block` pairs that text with `len(text) > USER_MEMORY_BUDGET_CHARS`; `build_memory_block` first fetches the profile.

DM replies record `last_interaction_at` and reset the unanswered-check-in streak with a combined read/update, returning the previous interaction timestamp for a coarse gap. Group replies include current time context without updating the personal interaction timestamp. Time context includes fixed reference UTC offsets, so it is approximate for regions with daylight saving time; it is not a per-user timezone setting.

Gender is inferred by the extraction model into a dedicated field, with instructions to avoid guessing from a name. It is not verified identity. Compression and consolidation do not modify this field.

Extraction mood updates also append to `mood_history`, retaining the latest `MAX_MOOD_HISTORY` entries (default `10`). Compression and consolidation can replace the current mood but do not append to this history. The displayed trend therefore represents extraction-time observations, not every conversational mood change.

## Compression and budget enforcement

Compression sends the compiled profile to the memory model with a target of 80% of `USER_MEMORY_BUDGET_CHARS`. A `None` result skips replacement. A validated result replaces facts, beliefs, and events, including when those returned lists are empty. It updates summary/style only when non-null and mood only when provided; identity, gender, preferences, insights, and mood history remain in the document.

The deterministic post-pass reads the profile once and removes items from the front of the arrays in this order: events, then beliefs, then facts. It recomputes rendered length locally and writes changed arrays once. This is array order, not a sort by event date or significance, so a major event can be removed.

The post-pass stops when the text fits or all three arrays are empty. Summary, style, insights, mood, and section headers are not shortened, so fitting the budget is not guaranteed. Compression also rebuilds item timestamps and clears event emotional context; the original item metadata is not an archive.

## Periodic consolidation

The scheduler checks profiles after each `CONSOLIDATION_SCAN_INTERVAL_SECS` (default `3600`). A profile is due when `last_consolidated_at` is absent/null or older than `CONSOLIDATION_INTERVAL_SECS` (default `86400`), and facts + beliefs + events total at least `CONSOLIDATION_MIN_ITEMS` (default `8`). The query does not filter out shared group profiles.

Each scan selects up to `CONSOLIDATION_MAX_USERS_PER_SCAN` qualifying profiles (default `50`) and processes them sequentially. Item-count filtering occurs in Python, so finding those profiles can read more than 50 documents. Set `CONSOLIDATION_INTERVAL_SECS <= 0` to disable the scheduler.

Consolidation sends the compiled current profile, not the full conversation history. On `None`, it leaves memory and the consolidation timestamp unchanged, allowing a later scan to retry. On a validated result it replaces facts/beliefs/events and insights, updates optional summary/style/mood, advances `last_consolidated_at`, and runs the same budget post-pass. Insights are capped at `MAX_INSIGHTS` (default `5`) and are model inferences rendered separately from user-stated beliefs.

## Concurrency and retention limits

The task manager's `memory_lock` coordinates extraction, compression, and consolidation for the same conversation in one process. Busy requests are skipped, not queued. Chat generation has a separate lock, so normal appends can continue during memory work. Compression cooldown state is in memory and is lost when the state is evicted or the process restarts.

This lock is not a distributed lock. Group extraction holds the group conversation lock while writing participant profiles; it does not acquire each participant's DM lock. Concurrent work from a DM or another group can overwrite read/modify/write updates to the same person. MongoDB makes each write atomic, but the entire read/modify/write sequence and save/trim pipeline are not transactions.

Timestamp-cutoff trimming preserves later appends on the normal path. The timestamp clock is monotonic only within one process. Legacy entries without a cutoff timestamp and whole-buffer deletion use array replacement; hard-cap eviction during a slow extraction can also change the oldest entries counted at trim time. These paths do not provide exact snapshot retention guarantees.

Profiles can contain personal facts, health details, inferred gender, moods, and behavioral insights. They are sent to the configured LLM, and a person's global profile can inform group replies. For reset scope and external backups, see [database: reset and data retention](database.md#reset-and-data-retention).

## Relevant tests

[test_hardening.py](../../tests/test_hardening.py) covers normal concurrent-appends trimming, hard caps, normalized fact deduplication, retry rereads, and failure trimming. [test_group_extraction.py](../../tests/test_group_extraction.py) covers shared/participant saves and unresolved names. [test_compression_safety.py](../../tests/test_compression_safety.py) covers `None` preservation and array shedding. The consolidation [model](../../tests/test_consolidation_models.py), [run](../../tests/test_consolidation_run.py), and [scheduler](../../tests/test_consolidation_scheduler.py) tests cover application and cadence. These mocked tests do not prove model accuracy or cross-process concurrency safety; use the [testing guide](testing_guide.md) for running them.
