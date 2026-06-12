# Database Architecture & Schema Design (MongoDB)

This guide documents the persistent storage layout of the ThinkMate system, MongoDB document structures, connection management using `motor`, and implementation details for asynchronous database interactions.

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

---

### 2. `chat_buffers` Collection
Manages the sliding window context of active conversation history per user. Kept separate from profiles to optimize high-frequency chat reads and writes.

* **Primary Key (`_id`)**: Telegram User ID (`int`)

#### Example Document Schema:
```json
{
  "_id": 12345678,
  "messages": [
    {
      "role": "user",
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
A centralized audit log collection to trace all inputs, prompts, API parameters, raw response text, parsed outputs, and latency/error information for LLM executions.

* **Primary Key (`_id`)**: Auto-generated `ObjectId`
* **Compound Index**: `("user_id", 1), ("timestamp", -1)` to optimize log inspection and chronological query lookups per user.

#### Example Document Schema:
```json
{
  "_id": ObjectId("6e3b2e..."),
  "user_id": 12345678,
  "call_type": "chat_reply",  // "chat_reply" | "memory_extraction" | "memory_compression"
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

## ⚙️ Connection Management (`connection.py`)

Database connections are managed asynchronously via `motor.motor_asyncio.AsyncIOMotorClient`. A single database client singleton is instantiated and reused.

```python
# app/database/connection.py
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from contextlib import asynccontextmanager
from loguru import logger
from app.config import config

_client: AsyncIOMotorClient | None = None

def get_db_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        logger.info("Initializing AsyncIOMotorClient...")
        _client = AsyncIOMotorClient(config.MONGODB_URI)
    return _client

def get_db() -> AsyncIOMotorDatabase:
    client = get_db_client()
    return client[config.MONGODB_DB]

@asynccontextmanager
async def db_session():
    """Context manager yielding the active MongoDB database instance."""
    db = get_db()
    yield db

async def init_db():
    """Initializes MongoDB indexes for optimized query performance."""
    db = get_db()
    logger.info("Initializing MongoDB indexes...")
    # Compound index for user LLM log auditing
    await db["llm_audit_log"].create_index([("user_id", 1), ("timestamp", -1)])
    logger.info("MongoDB indexes initialized successfully.")
```

---

## 🛠️ CRUD Models & Operations (`models.py`)

CRUD methods are designed to perform atomic document modifications, avoiding raw SQL statements or SQLite table constraints.

### 1. Active Chat History Operations
Chat history is appended to the message array via the `$push` operator. Slicing trims the oldest messages dynamically:

```python
async def add_message_to_buffer(db: AsyncIOMotorDatabase, user_id: int, role: str, content: str):
    """Appends a chat message to the messages array in the user's chat_buffers document."""
    now = datetime.utcnow()
    await db["chat_buffers"].update_one(
        {"_id": user_id},
        {
            "$push": {
                "messages": {
                    "role": role,
                    "content": content,
                    "created_at": now
                }
            },
            "$set": {"updated_at": now}
        },
        upsert=True
    )
```

---

### 2. Surgical Memory Updates (`save_extracted_memories`)
When the background Extractor processes the user's message history, updates are transactionally applied directly inside the user's profile document. We support full **hard deletion** of refuted facts, outdated beliefs, and old events.

```python
async def save_extracted_memories(db: AsyncIOMotorDatabase, user_id: int, extraction: MemoryExtraction):
    """Surgically applies extracted profile style, facts, beliefs, events, and emotional states to the user record."""
    profile = await db["user_profiles"].find_one({"_id": user_id})
    if not profile:
        await ensure_user(db, user_id, "", "")
        profile = await db["user_profiles"].find_one({"_id": user_id})
        
    facts = profile.get("facts", [])
    beliefs = profile.get("beliefs", [])
    events = profile.get("events", [])
    now = datetime.utcnow()
    
    set_fields = {}
    if extraction.profile_updates and extraction.profile_updates.communication_style:
        set_fields["communication_style"] = extraction.profile_updates.communication_style
        
    if extraction.emotional_state:
        set_fields["emotional_state"] = {
            "mood": extraction.emotional_state.mood,
            "intensity": extraction.emotional_state.intensity,
            "trigger": extraction.emotional_state.trigger or "",
            "detected_at": now
        }
        
    # Apply Facts modifications (Hard Deletes)
    removed_contents = {f.content for f in extraction.removed_facts}
    updated_old_contents = {f.old_content for f in extraction.updated_facts}
    exclude_facts = removed_contents.union(updated_old_contents)
    facts = [f for f in facts if f["content"] not in exclude_facts]
    
    for f in extraction.new_facts:
        facts.append({"category": f.category, "content": f.content, "confidence": 1.0, "created_at": now, "updated_at": now})
    for f in extraction.updated_facts:
        facts.append({"category": f.category, "content": f.new_content, "confidence": 1.0, "created_at": now, "updated_at": now})
        
    # Apply Beliefs modifications (Hard Deletes)
    removed_beliefs = {b.content for b in extraction.removed_beliefs}
    updated_old_beliefs = {b.old_content for b in extraction.updated_beliefs}
    exclude_beliefs = removed_beliefs.union(updated_old_beliefs)
    beliefs = [b for b in beliefs if b["content"] not in exclude_beliefs]
    
    for b in extraction.new_beliefs:
        beliefs.append({"content": b.content, "created_at": now, "updated_at": now})
    for b in extraction.updated_beliefs:
        beliefs.append({"content": b.new_content, "created_at": now, "updated_at": now})
        
    # Save back to MongoDB
    set_fields["facts"] = facts
    set_fields["beliefs"] = beliefs
    set_fields["events"] = events
    set_fields["updated_at"] = now
    
    await db["user_profiles"].update_one({"_id": user_id}, {"$set": set_fields})
```

---

## 🙋 Design Decisions & FAQ

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
