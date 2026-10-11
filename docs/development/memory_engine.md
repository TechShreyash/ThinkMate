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
2. Select everything except the latest retained entries and serialize transcript entries as JSON data with role, sender attribution, timestamps, and text.
3. Ask the LLM for structured updates. DM extraction includes the current compiled profile; group extraction includes separately labeled group and uniquely attributable participant profiles. Memory content is serialized as JSON data so embedded headings remain part of the supplied text.
4. On a non-`None` result, save updates and trim the oldest selected count. A valid empty result still counts as success and the selected history is trimmed.
5. On `None`, reread the buffer for the next attempt while retaining the original owner lifecycle tokens. After three unsuccessful attempts, trim only the selected entries from the final attempted snapshot. This intentionally loses unextracted history during sustained LLM failure, while preserving messages that arrived during that last call.

Transport retries inside each logical attempt are separate; see [LLM integration](llm_integration.md). Database or other pipeline exceptions are logged and end the run, rather than entering the `None` retry path.

The extraction prompt asks for English memory text, preservation of proper nouns, and absolute event dates resolved against each message's UTC date. These are model instructions. Event dates are nullable strings, without a calendar-format validator.

### Group attribution

A group attempt uses one `GroupMemoryExtraction` result containing optional shared `group_extraction` and participant `updates`:

- Shared updates are saved under the group `chat_id`.
- Participant names are matched to sender IDs from the extracted segment using case folding and collapsed whitespace. Unknown names are skipped.
- Names shared by different sender IDs are ambiguous and skipped, preventing updates from being assigned to the first matching person.
- A group profile marker records whether shared memory was saved and how many participant updates were saved or skipped, including on an otherwise empty successful result.
- Participant writes, the group marker, and buffer trim are separate operations. A failure can leave some updates saved while history remains available for re-extraction.

Group extraction limits the combined supplied memory text to `USER_MEMORY_BUDGET_CHARS`; transcript, metadata, instructions, and schema add further request content. An owner's memory is omitted in full when it does not fit, retaining the participant identity and an explicit availability flag. New supported information can still be saved, but updates/removals for an owner whose matching context was omitted are discarded. This keeps large groups from multiplying the full personal-profile budget by the number of participants.

## Compiling memory

`compile_memory_text` is a pure function that renders summary, inferred gender, communication style, facts, beliefs, behavioral insights, events, the latest recorded mood, and a recent mood trend. Mood context includes UTC observation timestamps (or `unknown` for legacy entries) and explicitly marks the latest mood as a past observation. Missing fields produce empty/default sections. `compile_memory_block` pairs that text with `len(text) > USER_MEMORY_BUDGET_CHARS`; `build_memory_block` first fetches the profile.

DM replies record `last_interaction_at` and reset the unanswered-check-in streak with a combined read/update, returning the previous interaction timestamp for a coarse gap. Group replies include current time context without updating the personal interaction timestamp. Time context uses named reference cities with date-aware daylight-saving offsets; unavailable timezone data causes a reference to be omitted while UTC remains available. Reference cities are not a per-user timezone setting.

Gender is inferred by the extraction model into a dedicated field, with instructions to avoid guessing from a name. It is not verified identity. Compression and consolidation do not modify this field.

Extraction mood updates also append to `mood_history`, retaining the latest `MAX_MOOD_HISTORY` entries (default `10`). Compression and consolidation preserve the mood, its original `detected_at`, and history even if the model emits a mood: reorganizing stored memory provides no new mood evidence. The displayed trend represents extraction-time observations, not every conversational mood change.

## Compression and budget enforcement

Compression sends the compiled profile to the memory model with a target of 80% of `USER_MEMORY_BUDGET_CHARS`. A `None` result skips replacement. A validated result replaces facts, beliefs, and events, including when those returned lists are empty. It updates summary/style only when non-null; identity, gender, preferences, insights, recorded mood, and mood history remain in the document.

The deterministic post-pass reads the profile once and removes items from the front of the arrays in this order: events, then beliefs, then facts. It first checks that the profile still belongs to the maintenance call's original lifecycle, then recomputes rendered length locally and writes changed arrays once, provided the memory snapshot still matches. A reset or concurrent change skips this write so new memory survives. This is array order, not a sort by event date or significance, so a major event can be removed.

The post-pass stops when the text fits or all three arrays are empty. Summary, style, insights, mood, and section headers are not shortened, so fitting the budget is not guaranteed. Compression also rebuilds item timestamps and clears event emotional context; the original item metadata is not an archive.

## Periodic consolidation

The scheduler checks profiles after each `CONSOLIDATION_SCAN_INTERVAL_SECS` (default `3600`). A profile is due when `last_consolidated_at` is absent/null or older than `CONSOLIDATION_INTERVAL_SECS` (default `86400`), and facts + beliefs + events total at least `CONSOLIDATION_MIN_ITEMS` (default `8`). The query does not filter out shared group profiles.

Each scan selects up to `CONSOLIDATION_MAX_USERS_PER_SCAN` qualifying profiles (default `50`) and processes them sequentially. Item-count filtering occurs in Python, so finding those profiles can read more than 50 documents. Set `CONSOLIDATION_INTERVAL_SECS <= 0` to disable the scheduler.

Consolidation sends the compiled current profile, not the full conversation history. On `None`, it leaves memory and the consolidation timestamp unchanged, allowing a later scan to retry. On a validated result it replaces facts/beliefs/events and insights, updates optional summary/style, advances `last_consolidated_at`, and runs the same budget post-pass. Insights are capped at `MAX_INSIGHTS` (default `5`) and are model inferences rendered separately from user-stated beliefs.

## Concurrency and retention limits

The task manager's `memory_lock` coordinates extraction, compression, and consolidation for the same conversation in one process. Busy requests are skipped, not queued. Chat generation has a separate lock, so normal appends can continue during memory work. Compression cooldown state is in memory and is lost when the state is evicted or the process restarts.

This lock is not a distributed lock. Group extraction holds the group conversation lock while writing participant profiles; it does not acquire each participant's DM lock. Memory writes use snapshot comparisons to protect participant profiles across DMs, groups, and processes: extraction retries on conflicts, while compression, consolidation, and budget enforcement skip outdated results. Identity and engagement updates can proceed independently. The save/trim pipeline and multi-participant updates remain separate operations rather than transactions.

New buffer messages have unique IDs. Extraction removes IDs from its original processed snapshot, preserving unprocessed messages even when the hard cap rotated the buffer during an LLM call. Legacy entries without IDs use replacement only when the entire buffer still matches the snapshot; otherwise trimming is deferred and retained messages may be extracted again. The timestamp clock remains monotonic only within one process, but trimming does not depend on timestamp order.

Extraction captures an owner's UUID `memory_generation` before asking the LLM and retains it across that run's retries. Output is rejected if that profile is deleted or recreated while the run proceeds. DM capture precedes the transcript snapshot. Group owners are resolved from the extracted segment and get lifecycle tokens even when their memory context is omitted; initially absent owners receive identity-free empty skeletons. A rejected owner produces no saved-memory event. Generation-conditioned group markers cannot recreate a reset shared profile. Tokens are removed with their profiles rather than retained as reset tombstones. This prevents stale in-flight results from undoing a reset; later group extraction can still learn from retained shared history, which personal reset does not delete.

Profiles can contain personal facts, health details, inferred gender, moods, and behavioral insights. They are sent to the configured LLM, and a person's global profile can inform group replies. For reset scope and external backups, see [database: reset and data retention](database.md#reset-and-data-retention).

## Relevant tests

[test_memory_concurrency.py](../../tests/test_memory_concurrency.py) covers overlapping writes, stale maintenance results, snapshot trimming under hard-cap eviction, and legacy append races.

[test_hardening.py](../../tests/test_hardening.py) covers normal concurrent-appends trimming, hard caps, normalized fact deduplication, retry rereads, and failure trimming. [test_group_extraction.py](../../tests/test_group_extraction.py) covers shared/participant saves and unresolved names. [Extraction context tests](../../tests/test_extraction_context.py) verify JSON attribution, exact-key corrections, and bounded group memory requests through the real SDK with mocked HTTP. [Mood preservation tests](../../tests/test_memory_mood_preservation.py) cover maintenance preserving observations and timestamps. [test_compression_safety.py](../../tests/test_compression_safety.py) covers `None` preservation and array shedding. The consolidation [model](../../tests/test_consolidation_models.py), [run](../../tests/test_consolidation_run.py), and [scheduler](../../tests/test_consolidation_scheduler.py) tests cover application and cadence. These mocked tests do not prove model accuracy or cross-process concurrency safety; use the [testing guide](testing_guide.md) for running them.
