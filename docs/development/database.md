# Database Architecture & Schema Design (MongoDB)

This guide documents the persistent storage layout of the ThinkMate system, MongoDB document structures, connection management using `motor`, and implementation details for asynchronous database interactions. In short, it is the reference for *where* the bot keeps its state and *how* that state is read and written safely under concurrent background tasks.

ThinkMate persists everything in MongoDB, a document database that stores records as nested JSON-like documents rather than rows across normalized tables. If you are new to the project, this guide is the place to learn the shape of each stored document, the indexes that keep lookups fast, and the patterns used to update memory without losing data.

**What this guide covers:**

- **Collection & document schema design** — the three primary collections (`user_profiles`, `chat_buffers`, `llm_audit_log`) plus the group-chat `chat_members` collection, each with an example document.
- **CRUD models & operations** — the atomic, concurrency-safe read/write helpers in `models.py` for chat history and memory updates. (CRUD is the standard create/read/update/delete set of database operations.)
- **Design decisions & FAQ** — the reasoning behind the major storage choices, including the migration from SQLite to MongoDB.

---

## 📐 Collection & Document Schema Design

ThinkMate stores all state inside three MongoDB collections. Rather than splitting data across normalized relational tables, we consolidate each user's standing context into a single user profile document. This enables single-query lookups, atomic document updates via MongoDB operations, and prevents the overhead of multi-table joins.

### 1. `user_profiles` Collection
Tracks biographical profiles, communication preferences, direct emotional states, and consolidated memory arrays (`facts`, `beliefs`, `events`) in a single self-contained document per user.

* **Primary Key (`_id`)**: Telegram User ID (`int`)
* **Index**: Since the document matches on `_id`, no secondary indices are required for lookup.

#### Example Document Schema:
```json
{
  "_id": 12345678,  // Telegram User ID (int)
  "username": "shreyash",
  "display_name": "Shreyash",
  "profile_summary": "Software developer from Seattle.",
  "communication_style": "Friendly and direct.",
  "gender": "male",  // "male" | "female" | "non-binary" | null — AI-inferred, used as reply context
  "emotional_state": {
    "mood": "happy",
    "intensity": 0.9,
    "trigger": "good weather",
    "detected_at": "2026-06-12T10:44:00Z"
  },
  "facts": [
    {
      "category": "preference",
      "content": "Enjoys green tea",
      "confidence": 1.0,
      "created_at": "2026-06-12T10:44:00Z",
      "updated_at": "2026-06-12T10:44:00Z"
    }
  ],
  "beliefs": [
    {
      "content": "Believes remote work increases productivity",
      "created_at": "2026-06-12T10:44:00Z",
      "updated_at": "2026-06-12T10:44:00Z"
    }
  ],
  "events": [
    {
      "description": "Graduated college",
      "event_date": "2026-05",
      "significance": "major",
      "emotional_context": "pride",
      "created_at": "2026-06-12T10:44:00Z"
    }
  ],
  "created_at": "2026-06-12T10:44:00Z",
  "updated_at": "2026-06-12T10:44:00Z"
}
```

> **Additive preference flags.** A few optional fields toggle per-user behavior and are read
> defensively (absent → default), so no migration is needed: `onboarded` (bool), `proactive_enabled`
> (bool — set by `/pause`/`/resume`), and `reactions_enabled` (bool — set by `/reactions`; absent or
> `true` means the bot may add emoji reactions to that user's messages). See
> [telegram_bot.md](telegram_bot.md#reactions--per-user-emoji-reaction-opt-out).

---

### 2. `chat_buffers` Collection
Manages the sliding window of active conversation history — that is, only the most recent stretch of messages is retained, and older ones fall out of the window as new ones arrive. It is kept separate from profiles to
optimize high-frequency chat reads and writes.

* **Primary Key (`_id`)**: Telegram **chat ID** (`int`). In a DM, `chat_id == user_id`, so DMs
  are unchanged; in groups, the buffer is shared by the whole conversation.
* Each message carries `sender_id` and `sender_name` so group history is multi-party
  ("Alice: …", "Bob: …") and extracted memory can be attributed to the right person. In DMs
  these simply equal the single user.

#### Example Document Schema:
```json
{
  "_id": 12345678,
  "messages": [
    {
      "role": "user",
      "sender_id": 12345678,
      "sender_name": "Alice",
      "content": "Hello bot!",
      "created_at": "2026-06-12T10:44:00Z"
    },
    {
      "role": "assistant",
      "content": "Hi there! How can I help?",
      "created_at": "2026-06-12T10:44:05Z"
    }
  ],
  "updated_at": "2026-06-12T10:44:05Z"
}
```

---

### 3. `llm_audit_log` Collection
A centralized audit log collection to trace all inputs, prompts, API parameters, raw response text, parsed outputs, and latency/error information for LLM executions. The audit log is an append-only diagnostic trail: it records what was sent to and received from the language model so failures can be traced after the fact.

* **Primary Key (`_id`)**: Auto-generated `ObjectId`
* **Compound Index**: `("user_id", 1), ("timestamp", -1)` to optimize log inspection and chronological query lookups per user.

#### Example Document Schema:
```json
{
  "_id": ObjectId("6e3b2e..."),
  "user_id": 12345678,
  "call_type": "chat_reply",  // "chat_reply" | "memory_extraction" | "group_memory_extraction" | "memory_compression"
  "inputs": {
    "system_prompt": "...",
    "messages": [...]
  },
  "outputs": {
    "raw_text": "...",
    "parsed_json": {...}  // structured JSON dictionary, or null
  },
  "status": "success",  // "success" | "failed"
  "error": null,        // Traceback error string if status is "failed"
  "timestamp": "2026-06-12T10:44:00Z"
}
```

---

### 4. `chat_members` Collection *(group chat — Phase 9, implemented)*
Stores per-(chat, user) affinity and ambient-reply mode so the bot can tune how readily it
engages each person in a group. Here *affinity* is a 0..1 score of how warmly the bot leans toward replying to a given person, and *mode* is that person's reply preference. Cached in memory and written through on change, so it adds no
hot-path read. Has no effect in DMs.

* **Primary Key (`_id`)**: composite string `"{chat_id}:{user_id}"`.

#### Example Document Schema:
```json
{
  "_id": "-1001234567890:12345678",
  "chat_id": -1001234567890,
  "user_id": 12345678,
  "affinity": 0.62,                  // 0..1, default AFFINITY_DEFAULT
  "mode": "auto",                    // "auto" | "quiet" | "chatty"
  "updated_at": "2026-06-12T10:44:00Z"
}
```

> See [group_chat.md](group_chat.md) for how affinity is updated (mentions/engagement, "stop/
> quiet" keywords, and the `affinity_delta` that piggybacks on the reply call) and how `mode` is
> set via `/quiet` and `/chatty`.

The collection is read and written through `models.get_chat_member(db, chat_id, user_id)` and
`models.upsert_chat_member(db, chat_id, user_id, *, affinity=None, mode=None)`. `upsert_chat_member`
clamps `affinity` to `[0.0, 1.0]`, coerces an invalid `mode` to `"auto"` (rather than raising), and
applies defaults (`AFFINITY_DEFAULT`, `"auto"`, `created_at`) only on insert via `$setOnInsert`.
The in-memory read-through / write-through `AffinityCache`
([`affinity.py`](../../app/services/affinity.py)) sits in front of these so warm members never hit
the DB on the hot path.

---

### 5. `group_settings` Collection *(group on/off kill switch + group-wide chattiness)*

Stores per-group settings controlled by group admins:

1. **On/off kill switch** (`enabled`) — `/groupoff` / `/groupon`. When a group is disabled,
   `_handle_group_message` returns at the top — no reply, no ambient/implicit reply, no identity
   capture, no memory extraction, and no buffer write.
2. **Group-wide ambient mode** (`group_mode`) — `/groupmode quiet|chatty|normal`. This
   sets how chatty the bot is for **everyone** in the group and takes **priority over each member's
   personal `/quiet` / `/chatty`** preference. Values: `auto` (default — no override, defer to the
   per-user mode), `quiet`, `chatty`.

* **Primary Key (`_id`)**: the Telegram `chat_id` (integer).

#### Example Document Schema:

```json
{
  "_id": -1001234567890,
  "enabled": false,
  "group_mode": "quiet",
  "created_at": "2026-06-14T12:00:00Z",
  "updated_at": "2026-06-14T12:34:00Z"
}
```

> **Defaults are enabled + auto.** A group with no document is active with no chattiness override.
> `models.is_group_enabled` degrades to `True` and `models.get_group_mode` degrades to `"auto"` on
> any read error, so a transient DB hiccup can never silently mute the bot or change its chattiness.

The `enabled` flag is read on every group message via `models.is_group_enabled(db, chat_id)` and
written by `models.set_group_enabled(db, chat_id, enabled)`. The `group_mode` is read on the ambient
path via `models.get_group_mode(db, chat_id)` and written by `models.set_group_mode(db, chat_id,
mode)` (a single upsert; invalid modes are coerced to `auto`). Authorization for all of these
commands is enforced in [`commands.py`](../../app/handlers/commands.py) via `bot.get_chat_member`
(administrator/creator) or a configured `ADMIN_USER_IDS` operator.

---

### 6. `metrics_state` Collection *(observability checkpoint)*

Persists the process-wide observability metrics so the `/health` and `/metrics` reports survive a
restart or crash instead of resetting to zero. The in-memory
[`MetricsRegistry`](../../app/services/metrics.py) is checkpointed into a **single** document here.
See [observability.md](observability.md#metrics-persistence-surviving-restarts) for the full
load/flush lifecycle.

* **Primary Key (`_id`)**: the fixed string `"metrics:singleton"` (one document for the whole process).

#### Example Document Schema:

```json
{
  "_id": "metrics:singleton",
  "state": {
    "counters": { "llm.reply.calls": 42, "throttle.drops": 0 },
    "gauges":   { "conversations.active": 2 },
    "timers":   { "llm.reply.latency": { "count": 42, "sum": 64.2, "max": 3.1, "avg": 1.53 } }
  },
  "updated_at": "2026-06-14T12:34:00Z"
}
```

> **`state` mirrors `metrics.snapshot()`.** On startup `models.load_metrics_state(db)` returns this
> dict and `metrics.load_state(...)` merges it back in (counters/timer totals are *added*, gauges
> replaced, timer `max` kept as the larger). `models.save_metrics_state(db, snapshot)` is a single
> upsert. Both helpers are best-effort and never raise, so a missing or malformed document can't
> block startup or break a flush.

---

Database connections are managed asynchronously via `motor.motor_asyncio.AsyncIOMotorClient`. A single database client singleton is instantiated and reused — a *singleton* here means one shared client object is created on first use and handed out on every subsequent call, so the bot never opens redundant connection pools.

```python
# app/database/connection.py
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from contextlib import asynccontextmanager
from loguru import logger
from app.config import config

_client: AsyncIOMotorClient | None = None

def get_db_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        logger.info("Initializing AsyncIOMotorClient...")
        _client = AsyncIOMotorClient(config.MONGODB_URI, serverSelectionTimeoutMS=10000)
    return _client

def get_db() -> AsyncIOMotorDatabase:
    return get_db_client()[config.MONGODB_DB]

@asynccontextmanager
async def db_session():
    """Context manager yielding the active MongoDB database instance."""
    yield get_db()

async def ping_db():
    """Verify connectivity at startup (fail fast if Mongo is unreachable)."""
    await get_db_client().admin.command("ping")

async def init_db():
    """Create indexes for query performance and audit-log retention."""
    db = get_db()
    # Compound index: audit queries filtered by user_id, sorted by timestamp.
    await db["llm_audit_log"].create_index([("user_id", 1), ("timestamp", -1)])
    # TTL index: auto-expire audit entries after the retention window. Wrapped defensively
    # so an unsupported backend (e.g. mongomock) can't block startup.
    try:
        await db["llm_audit_log"].create_index(
            [("timestamp", 1)],
            expireAfterSeconds=config.AUDIT_LOG_RETENTION_DAYS * 86400,
            name="audit_ttl",
        )
    except Exception as e:  # noqa: BLE001 - retention is best-effort, never fatal
        logger.warning(f"Could not create audit-log TTL index: {e}")
```

A TTL (time-to-live) index is a MongoDB index that automatically deletes documents once they exceed a configured age, which is how old audit entries expire without a manual cleanup job.

> **Index summary.** `user_profiles` and `chat_buffers` are matched by `_id` (no secondary
> indexes needed). `chat_members` is matched by its composite `_id` string. `llm_audit_log`
> carries a compound `(user_id, 1),(timestamp, -1)` index plus a `(timestamp, 1)` TTL index.
> See [performance_and_scaling.md](performance_and_scaling.md#database-access-patterns--indexes).

---

## 🛠️ CRUD Models & Operations (`models.py`)

CRUD methods are designed to perform atomic document modifications, avoiding raw SQL statements or SQLite table constraints. *Atomic* here means each update lands as a single indivisible MongoDB operation, so a concurrent task never observes a half-applied write.

### 1. Active Chat History Operations
Chat history is appended via `find_one_and_update` so the post-update array is returned in a
single round-trip (the caller derives both the char count and the active history from it). A
`$slice` hard cap bounds the array, and timestamps use a strictly-monotonic millisecond clock
so the atomic trim below is exact:

```python
async def add_message_to_buffer(
    db: AsyncIOMotorDatabase,
    chat_id: int,
    role: str,
    content: str,
    *,
    sender_id: int | None = None,
    sender_name: str = "",
) -> list[dict]:
    """Append a message and return the resulting messages array (char count + history in one RT).

    Keyed by chat_id; each pushed message also carries sender_id/sender_name for multi-party
    group context. sender_id defaults to chat_id when omitted, preserving DM semantics.
    """
    if sender_id is None:
        sender_id = chat_id            # DM: the lone speaker's id equals the chat id
    now = _monotonic_utcnow()  # strictly increasing at ms resolution within the process
    doc = await db["chat_buffers"].find_one_and_update(
        {"_id": chat_id},
        {
            "$push": {
                "messages": {
                    "$each": [{
                        "role": role,
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "content": content,
                        "created_at": now,
                    }],
                    "$slice": -config.CHAT_BUFFER_HARD_CAP,   # safety net against unbounded growth
                }
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc.get("messages", []) if doc else []
```

Trimming the processed segment is **atomic** — a `$pull` on a `created_at` cutoff, never a
read-slice-overwrite — so messages appended by a concurrent chat batch while a (slow) extractor
runs are never clobbered:

```python
async def delete_oldest_buffer_messages(db, chat_id: int, count: int):
    """Atomically remove the `count` oldest messages via $pull on a created_at cutoff."""
    # read once, compute the cutoff = messages[count].created_at, then $pull < cutoff
    ...
```

---

### 2. Surgical Memory Updates (`save_extracted_memories`)
When the background Extractor processes message history, updates are applied inside the user's
profile document in a **single read-modify-write** (load the arrays once, mutate in memory, then
one `$set`) — a read-modify-write reads the current arrays, changes them in process memory, and writes the whole result back in one operation. This is the efficient pattern — never a query per fact/belief/event. It supports
full **hard deletion** of refuted facts, outdated beliefs, and old events, applies the same CRUD
to all three arrays, and is robust to LLM phrasing drift:

- **Normalized matching**: removals/updates match stored items by a normalized key
  (`casefold` + whitespace-collapse), so "Lives in Seattle" and "lives in  seattle" resolve to
  the same item.
- **Dedup on write**: new/updated items are skipped if their normalized key already exists, so
  re-extraction can't create duplicates.
- **Events carry metadata**: updates preserve the original `event_date`/`significance`/
  `emotional_context`/`created_at` unless explicitly changed.

```python
async def save_extracted_memories(db, user_id: int, extraction: MemoryExtraction):
    profile = await db["user_profiles"].find_one({"_id": user_id}) or await _ensure(db, user_id)
    facts, beliefs, events = profile.get("facts", []), profile.get("beliefs", []), profile.get("events", [])
    now = _utcnow()

    def norm(s): return " ".join((s or "").split()).casefold()

    # FACTS — drop removed/updated-old (normalized), then append new/updated, deduped.
    exclude = {norm(f.content) for f in extraction.removed_facts} | {norm(f.old_content) for f in extraction.updated_facts}
    facts = [f for f in facts if norm(f["content"]) not in exclude]
    seen = {norm(f["content"]) for f in facts}
    for f in [*extraction.new_facts, *( _as_new(u) for u in extraction.updated_facts)]:
        if norm(f.content) in seen: continue
        seen.add(norm(f.content)); facts.append({"category": f.category, "content": f.content,
                                                  "confidence": 1.0, "created_at": now, "updated_at": now})
    # BELIEFS and EVENTS follow the same remove -> dedup-append pattern (events keep prior metadata).
    ...
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"facts": facts, "beliefs": beliefs, "events": events,
                  "communication_style": ..., "emotional_state": ..., "updated_at": now}},
    )
```

> `replace_user_memory` (used by the compressor) follows the same single-write shape, replacing
> the arrays with the compressed layout. **It is skipped when compression fails** (the LLM
> returns `None`), so a failed compression never wipes a user's memory.

### Export & reset (backup-before-delete)

`/reset` is irreversible for the user, so two helpers cooperate to make it recoverable for an
admin:

- `export_user_data(db, user_id)` reads the **whole** `user_profiles` document plus the
  `chat_buffers` document and returns a single JSON-serializable snapshot
  (`{user_id, exported_at, user_profiles, chat_buffers}`), or `None` when the user has no
  profile. `datetime`/`ObjectId` values are left intact — serialize with `json.dumps(..., default=str)`.
- `reset_user(db, user_id)` hard-deletes both documents.

The `/reset confirm` handler calls `export_user_data` **first** and uploads the snapshot to the
Telegram Logs_Channel as a `backup_<user_id>.json` file (via
[`log_forwarder.send_document`](../../app/services/log_forwarder.py)) before calling
`reset_user`. The backup is best-effort and never blocks the delete the user requested.

---

## 🙋 Design Decisions & FAQ

This section captures the "why" behind the storage model. Each entry records a decision the project made and the reasoning that led to it, so future contributors understand the trade-offs before changing them.

### Q1: Why did we migrate from SQLite to MongoDB?
SQLite requires a local database file, which restricts multi-instance bot scaling, horizontal container deployments (like Docker/Kubernetes on cloud platforms), and creates file locking risks when concurrent background tasks write to disk. MongoDB enables cloud-native bot scaling, offers document nesting that maps naturally to Python's Pydantic schemas, and supports high-concurrency database drivers (`motor`).

### Q2: Why are we using a single document-per-user model for memory arrays?
Instead of normalized relational tables where each fact or event is a row, nesting arrays directly inside `user_profiles` allows atomic updates. Loading user context for system prompts is achieved in a single fast collection lookup query, bypassing index lookups on multiple foreign-key tables. Because our memory compression triggers at a 4,000-character budget, a user profile document's typical size is <20KB, which is tiny compared to MongoDB's 16MB document limit.

### Q3: Why did we remove soft deletions (is_active tombstones)?
Initially, soft-deleted facts were kept to prevent the extraction LLM from re-extracting the same old details. However, soft tombstones clutter the database over time. If a user refutes an old fact and mentions it again later, it is parsed by the LLM as a new detail and stored fresh. This allowed transitioning to hard deletions for facts, beliefs, and events, eliminating database arrays clutter.

### Q4: Why are facts separated from subjective beliefs?
Objective facts represent verifiable, concrete information (e.g. "Lives in Seattle," "Has a Golden Retriever Bruno"). In contrast, beliefs are opinions, values, and subjective views (e.g. "Believes remote work is productive," "Valuables family time above all else"). Separating them allows the compiler to structure the LLM memory blocks cleanly under separate headings (`=== CORE FACTS ===` vs `=== SUBJECTIVE BELIEFS ===`), preventing objective-subjective pollution in chatbot reasoning.

### Q5: Why is emotional state updated directly instead of maintaining a log?
A chronological emotional log collection (`emotional_log`) was redundant. The chatbot prompt compiler only ever injects the active (latest) emotional state. By saving mood, intensity, and triggers directly to the `emotional_state` field in `user_profiles`, we keep writes simple and fast, avoiding collection scans.

### Q6: Why did we lower the memory threshold to 4,000 characters?
With a 10,000-character threshold, a user would have to chat for a long time before compression ever ran. This meant that high-level profile synthesis (like writing `profile_summary` or analyzing `communication_style`) was postponed indefinitely. Lowering the default budget to `4,000` characters triggers memory compression timely, populating biographical descriptions and preference synthesis early.

### Q7: Why is a shared `memory_lock` used in `UserTaskManager`?
The extractor and compressor run as asynchronous background tasks. If they execute concurrently for the same user, they could read overlapping states, perform calculations, and write back, corrupting or overwriting each other's updates. A shared `memory_lock` guarantees they serialize, executing sequentially for any single user.
