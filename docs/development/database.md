# Database reference

ThinkMate stores persistent state in MongoDB using Motor. Document helpers live in [models.py](../../app/database/models.py); [connection.py](../../app/database/connection.py) owns the client and index setup. See [memory engine](memory_engine.md) for the processing lifecycle and [configuration](configuration.md) for connection and retention settings.

## Connection lifecycle

`get_db_client()` lazily creates one `AsyncIOMotorClient` per process with a 10-second server-selection timeout. `get_db()` selects `MONGODB_DB` with timezone-aware UTC BSON decoding. `db_session()` yields that shared database; it does not start a MongoDB transaction or isolate operations.

Startup calls `ping_db()` and `init_db()` from the [entry point](../../main.py). Shutdown currently closes the Telegram session but has no explicit MongoDB-client close step. Changing connection settings requires a restart to recreate the client.

`init_db()` creates two indexes on `llm_audit_log`:

- `(user_id ascending, timestamp descending)` for per-ID diagnostic queries.
- `timestamp ascending`, named `audit_ttl`, with `expireAfterSeconds = AUDIT_LOG_RETENTION_DAYS * 86400` (default retention `30` days).

The compound-index failure propagates; TTL-index creation failure is logged and startup continues. Audit expiry depends on a working TTL index and MongoDB's background cleanup. Updating the configured retention can conflict with an existing index's options; check the actual index after changing it. There are no application-created secondary indexes or TTL policies for the other collections.

## Collections

| Collection | `_id` | Purpose |
| --- | --- | --- |
| `user_profiles` | Telegram user ID, or group chat ID | Personal memory/preferences or shared group memory. |
| `chat_buffers` | Telegram chat ID | Recent plain-text conversation entries with speaker attribution. |
| `llm_audit_log` | Generated ObjectId | LLM call summaries, status, and failure tracebacks. |
| `chat_members` | `"{chat_id}:{user_id}"` | Per-member group affinity and ambient-reply preference. |
| `group_settings` | Telegram group chat ID | Admin-controlled enabled flag and group-wide ambient mode. |
| `metrics_state` | `"metrics:singleton"` | Latest process-wide metrics snapshot. |

Direct-message chat IDs equal user IDs. Group buffers remain separate from personal profiles, but participant memory is stored in the same global personal profile used by DMs and other groups.

### `user_profiles`

`ensure_user` upserts Telegram identity and creates a memory skeleton only on insert. `refresh_identity_if_changed` avoids repeated identity writes on unchanged group messages. Memory writes can create a skeleton without identity when a profile is absent.

| Field | Stored shape / behavior |
| --- | --- |
| `username`, `display_name` | Telegram identity strings when captured. |
| `profile_summary`, `communication_style` | Strings; initialized empty. Summary is populated by compression/consolidation. |
| `gender` | `male`, `female`, `non-binary`, or null; inferred by the extraction model. |
| `facts` | `{category, content, confidence, created_at, updated_at}` entries. `confidence` is assigned `1.0` by code, not a calibrated certainty score. |
| `beliefs` | `{content, created_at, updated_at}` entries. |
| `events` | `{description, event_date, significance, emotional_context, created_at}` entries. |
| `insights` | `{content, created_at, updated_at}` model-inferred patterns; replaced by consolidation and capped at `MAX_INSIGHTS`. |
| `emotional_state` | Null or `{mood, intensity, trigger, detected_at}`. |
| `mood_history` | Recent extraction mood observations in the same shape; capped at `MAX_MOOD_HISTORY`. |
| `onboarded` | Boolean, initialized `false`. |
| `proactive_enabled`, `reactions_enabled` | Optional preferences. Missing flags default to enabled. |
| `last_interaction_at`, `last_proactive_at` | Optional UTC timestamps for DM engagement/check-in scheduling. |
| `proactive_unanswered` | Consecutive delivered check-ins without engagement; cleared on a DM or command. |
| `last_consolidated_at` | Timestamp advanced when a consolidation result is applied. |
| `created_at`, `updated_at` | UTC timestamps. Some preference/engagement helpers update only their own fields. |

Shared group profiles additionally carry `profile_type: "group"`, `group_chat_id`, `last_group_extraction_at`, and `last_group_extraction: {group_saved, participant_updates, skipped_updates}`. These marker fields are written on successful group extraction even when there is no new shared memory.

Application dates are BSON datetimes, decoded as UTC-aware Python datetimes. The event's `event_date` is a nullable string: the prompt requests `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`, but the schema does not validate calendar formatting.

### `chat_buffers`

An illustrative exported buffer is valid JSON; its timestamps represent BSON datetime values serialized to strings:

```json
{
  "_id": 12345678,
  "messages": [
    {
      "role": "user",
      "sender_id": 12345678,
      "sender_name": "Alice",
      "content": "Hello!",
      "created_at": "2026-10-11T08:00:00+00:00"
    },
    {
      "role": "assistant",
      "sender_id": 0,
      "sender_name": "ThinkMate",
      "content": "Hi Alice!",
      "created_at": "2026-10-11T08:00:05+00:00"
    }
  ],
  "updated_at": "2026-10-11T08:00:05+00:00"
}
```

`add_message_to_buffer` uses `find_one_and_update` with `$push`, `$slice`, and `ReturnDocument.AFTER`. It returns the updated messages array for history and length calculations. The `$slice` retains at most `CHAT_BUFFER_HARD_CAP` entries. This is a count limit, not a character, token, or complete-chat archive limit.

`get_chat_buffer` returns only `{role, content}`. Extraction reads the raw document directly for timestamps and group identity.

New messages carry a unique `message_id`. Extraction passes its original raw buffer snapshot to the trim helper, which removes only those processed IDs with `$pull`. Concurrent appends and hard-cap eviction cannot change the removal set. Legacy messages without IDs use array replacement conditioned on the entire array still matching the snapshot; concurrent changes defer that trim until a later extraction. Observation timestamps retain a process-local monotonic millisecond clock.

### `llm_audit_log`

Each logical LLM operation records `user_id`, `call_type`, `inputs`, `outputs`, `status`, `error`, and a UTC `timestamp`. On group calls the `user_id` field contains the group chat ID.

`inputs` contains string lengths of the supplied input values; `outputs` contains `raw_text` and `parsed_json` string lengths. These are diagnostic character counts, not provider token usage or billing information. The audit implementation does not routinely retain prompt/completion bodies, but `error` contains a failure traceback and can include provider response details. Logging is best effort and can be lost on process interruption. See [LLM integration](llm_integration.md#audit-and-metrics).

### Group and metrics state

`chat_members` stores `chat_id`, `user_id`, `affinity` (clamped to `0..1`), `mode` (`auto`, `quiet`, or `chatty`), and timestamps. Invalid written modes fall back to `auto`; [affinity.py](../../app/services/affinity.py) provides an in-memory read/write-through cache. A cold read can still reach MongoDB.

`group_settings` stores optional `enabled` and `group_mode` fields with timestamps. Missing values default to enabled and `auto`. The database accessors can raise on read errors; the [message handler](../../app/handlers/messages.py) applies fallback behavior. Admin commands update these settings. See [group chat](group_chat.md).

`metrics_state.state` holds the registry's counters, gauges, and timers. Loading and saving are best effort. Startup merges the saved snapshot, periodic flushes checkpoint it, and shutdown attempts a final flush. This singleton is process-wide state, not a safe aggregate of several simultaneously running bot instances. See [observability](observability.md).

## Memory update semantics

`save_extracted_memories` loads the current profile, builds updated arrays in Python, and applies one conditional `$set`. The write matches the memory fields of the read snapshot; if another conversation changed them, it retries against the latest profile up to five times. Repeated conflicts leave messages available for a later extraction instead of trimming after an unsuccessful save.

- Fact/belief removals and updates compare case-folded text with collapsed whitespace. This matches casing/spacing differences, not paraphrases.
- New and updated facts/beliefs/events are deduplicated by normalized content.
- Event updates keep the original date and significance when their replacement values are null, and preserve original emotional context and creation time when the old entry is found.
- Mood updates also append a bounded mood-history entry. Non-null style/gender updates do not clear earlier values when omitted.

`replace_user_memory` and `apply_consolidation` replace the fact/belief/event arrays with the model's returned lists. They rebuild item timestamps and clear event emotional context. Consolidation also replaces insights and advances its timestamp. Null summary/style fields preserve the corresponding previous values. Both helpers always preserve the recorded mood, its observation time, and mood history; maintenance output cannot establish a new mood. A valid result with empty lists empties the arrays; callers skip replacement only for `None`.

Compression and consolidation capture the profile before the LLM call and apply their results only if its memory fields still match. A concurrent memory change or reset skips replacement, preserving the newer data. The budget post-pass also conditions its write on the profile snapshot. Identity and engagement updates do not invalidate these memory writes. Group writes and history trim remain separate operations rather than a transaction. See [memory engine](memory_engine.md).

Each profile carries a UUID `memory_generation`, assigned at creation or conditionally initialized for legacy profiles. Extraction captures it before the LLM call and requires the same generation at save time. Deleted or recreated profiles reject stale output without recreating the old profile; group status markers also require their captured generation. Known owners whose profiles are initially absent receive identity-free empty skeletons before the call, including participants omitted from the memory context budget. Unresolved names never create profiles. The token is deleted with its profile and leaves no tombstone. Direct `save_extracted_memories` callers may omit the expected generation to retain their existing create-if-absent behavior.

## Reset and data retention

`export_user_data(db, user_id)` returns the full personal `user_profiles` document and the same-ID DM `chat_buffers` document, plus `user_id` and `exported_at`. It returns `None` when the profile is absent, even if a buffer exists. BSON/Python values require serialization such as `json.dumps(..., default=str)`.

The [reset handler](../../app/handlers/commands.py) requires `/reset confirm` (or the configured reset trigger). It stops prior owned DM work and holds new DM messages through snapshot export and deletion. If prior work does not stop within 10 seconds, deletion does not proceed. It attempts to send the snapshot as `backup_<user_id>.json` to `LOGS_CHANNEL_ID` before deletion. The backup operation is awaited but best effort: an unset channel, missing permissions, or a send failure does not prevent deletion. There is no built-in restore command; the success text mentions a backup only after its upload succeeds.

`reset_user` performs two separate deletions: the personal profile and the same-ID DM buffer. It does **not** delete:

- Shared group buffers/profiles, including text previously written by that person.
- `chat_members` affinity/mode records or group settings.
- Audit entries, metrics, local logs, or files/messages already sent to the logs channel.
- Data held by Telegram, the configured LLM provider, or external database backups.

Shared group work continues. Captured generation tokens reject obsolete extraction results, but later group extraction from retained messages or new DM activity can create a profile again. Reset is therefore a local profile/DM-buffer reset, not complete account-data erasure.

Profiles, active buffers, and other non-audit collections have no automatic age-based retention in this implementation. Mood and insight lists have item caps; memory compression is a content-reduction mechanism, not a deletion policy for all records. Restrict access to MongoDB and the logs channel because profiles and reset snapshots contain personal conversation data. A person's global profile is also included when replying to them in a group, so there is no enforced separation of DM memories from group reply context.

## Tests and maintenance

[test_database.py](../../tests/test_database.py) covers connection codecs, indexes, buffers, and basic saves. [test_hardening.py](../../tests/test_hardening.py), [test_group_models.py](../../tests/test_group_models.py), [test_consolidation_models.py](../../tests/test_consolidation_models.py), and [test_engagement_models.py](../../tests/test_engagement_models.py) cover the additional update paths. Most persistence tests use mongomock; they do not establish production TTL expiry, real transaction behavior, or cross-process consistency. See [testing guide](testing_guide.md) and [performance and scaling](performance_and_scaling.md).
