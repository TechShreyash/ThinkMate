# Database Architecture & Schema Design (SQLite + aiosqlite)

This guide documents the persistent storage layout of the ThinkMate system, detail-oriented SQLite schemas, and implementation details for asynchronous database interactions, aligning with our Pydantic validation structure.

---

## 📐 Schema Definitions

The schema is defined in [connection.py](file:///d:/ThinkMate/app/database/connection.py):

```sql
-- 1. Main User Profiles
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    profile_summary TEXT DEFAULT '',
    communication_style TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Core Facts (Atomic fragments of user preferences, details, and habits)
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    category TEXT NOT NULL,          -- 'personal', 'work', 'preference', 'health', 'hobby', 'relationship'
    content TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1,     -- Soft delete flag
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- 3. Life Events Timeline (Important chronological events)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    event_date TEXT,                 -- ISO date or string ("June 2026", "yesterday")
    significance TEXT DEFAULT 'minor', -- 'major', 'minor', 'routine'
    emotional_context TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- 4. Emotional Log (Logs user mood trends detected by LLM analysis)
CREATE TABLE IF NOT EXISTS emotional_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mood TEXT NOT NULL,
    intensity REAL DEFAULT 0.5,      -- Value from 0.0 (weak) to 1.0 (strong)
    trigger TEXT DEFAULT '',
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- 5. Chat Buffer (Temporary timeline of conversation histories for sliding windows)
CREATE TABLE IF NOT EXISTS chat_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,              -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);
```

---

## ⚙️ Connection Management (`connection.py`)

SQLite database files are kept inside the gitignored `data/` folder. Connection allocation runs on an async pool model using Write-Ahead Logging (WAL):

```python
# app/database/connection.py
import os
import aiosqlite
from app.config import config

DB_PATH = os.path.join("data", "database.sqlite")

async def get_db_connection() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    # Enable WAL mode to prevent locks during concurrent writes
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with await get_db_connection() as db:
        schema = """
        -- Schema SQL defined above...
        """
        await db.executescript(schema)
        await db.commit()
```

---

## 🛠️ Dependency-Injected CRUD Models (`models.py`)

In this architecture, database connections are managed by a middleware and injected directly as the first argument (`db: Connection`) of the CRUD methods.

### 1. User Management
Verify and update profile references:

```python
from aiosqlite import Connection

async def ensure_user(db: Connection, user_id: int, username: str, display_name: str):
    await db.execute(
        """
        INSERT INTO user_profiles (user_id, username, display_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            display_name = excluded.display_name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, username, display_name)
    )
    await db.commit()
```

### 2. Message History Buffer Operations
Maintain the message queue for sliding windows:

```python
async def add_message_to_buffer(db: Connection, user_id: int, role: str, content: str):
    await db.execute(
        "INSERT INTO chat_buffer (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    await db.commit()

async def get_chat_buffer(db: Connection, user_id: int) -> list[dict]:
    async with db.execute(
        "SELECT role, content FROM chat_buffer WHERE user_id = ? ORDER BY id ASC",
        (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

async def get_buffer_count(db: Connection, user_id: int) -> int:
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM chat_buffer WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return row["cnt"]

async def delete_oldest_buffer_messages(db: Connection, user_id: int, count: int):
    await db.execute(
        """
        DELETE FROM chat_buffer 
        WHERE id IN (
            SELECT id FROM chat_buffer 
            WHERE user_id = ? 
            ORDER BY id ASC 
            LIMIT ?
        )
        """,
        (user_id, count)
    )
    await db.commit()
```

### 3. Transactional Memory Insertion (Using Pydantic Inputs)
Here, the database loader parses attributes from the validated `MemoryExtraction` Pydantic model:

```python
from app.services.schemas import MemoryExtraction

async def save_extracted_memories(db: Connection, user_id: int, extraction: MemoryExtraction):
    """Transactionally commits profile, fact, event, and mood updates."""
    
    # Use SQLite transaction markers to ensure all operations succeed or fail together
    await db.execute("BEGIN TRANSACTION;")
    try:
        # 1. Apply Profile Updates
        if extraction.profile_updates and extraction.profile_updates.communication_style:
            await db.execute(
                """
                UPDATE user_profiles 
                SET communication_style = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (extraction.profile_updates.communication_style, user_id)
            )

        # 2. Insert New Facts
        for fact in extraction.new_facts:
            await db.execute(
                "INSERT INTO facts (user_id, category, content) VALUES (?, ?, ?)",
                (user_id, fact.category, fact.content)
            )

        # 3. Apply Fact Modifications (Soft-delete old, insert new)
        for fact in extraction.updated_facts:
            # Soft-delete the matching old fact
            await db.execute(
                "UPDATE facts SET is_active = 0 WHERE user_id = ? AND content = ?",
                (user_id, fact.old_content)
            )
            # Insert the replacement fact
            await db.execute(
                "INSERT INTO facts (user_id, category, content) VALUES (?, ?, ?)",
                (user_id, fact.category, fact.new_content)
            )

        # 4. Save Event Details
        for event in extraction.events:
            await db.execute(
                """
                INSERT INTO events (user_id, description, event_date, significance, emotional_context) 
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    event.description,
                    event.date,
                    event.significance,
                    event.emotion
                )
            )

        # 5. Log Mood State
        if extraction.emotional_state:
            await db.execute(
                "INSERT INTO emotional_log (user_id, mood, intensity, trigger) VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    extraction.emotional_state.mood,
                    extraction.emotional_state.intensity,
                    extraction.emotional_state.trigger
                )
            )

        await db.commit()
    except Exception as e:
        await db.execute("ROLLBACK;")
        raise e
```

### 4. Fetching memory blocks
```python
async def get_active_facts(db: Connection, user_id: int) -> list[dict]:
    async with db.execute(
        "SELECT id, category, content FROM facts WHERE user_id = ? AND is_active = 1",
        (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [{"id": r["id"], "category": r["category"], "content": r["content"]} for r in rows]

async def deactivate_facts_by_ids(db: Connection, ids: list[int]):
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE facts SET is_active = 0 WHERE id IN ({placeholders})",
        tuple(ids)
    )
    await db.commit()
```

### 5. Atomic Memory Replacement during Compression
For background memory compression, `replace_user_memory()` updates the user's profile metadata, marks old active facts as inactive, replaces events history, and records the current mood in a single SQLite transaction block:

```python
from app.services.schemas import MemoryCompression

async def replace_user_memory(db: Connection, user_id: int, compression: MemoryCompression):
    """Transactionally updates the profile and completely replaces active facts and events with the compressed ones."""
    await db.execute("BEGIN TRANSACTION;")
    try:
        # 1. Update Profile summary and communication style if provided
        if compression.profile_summary is not None or compression.communication_style is not None:
            if compression.profile_summary is not None and compression.communication_style is not None:
                await db.execute(
                    "UPDATE user_profiles SET profile_summary = ?, communication_style = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (compression.profile_summary, compression.communication_style, user_id)
                )
            elif compression.profile_summary is not None:
                await db.execute(
                    "UPDATE user_profiles SET profile_summary = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (compression.profile_summary, user_id)
                )
            elif compression.communication_style is not None:
                await db.execute(
                    "UPDATE user_profiles SET communication_style = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (compression.communication_style, user_id)
                )

        # 2. Soft-delete all old active facts for the user
        await db.execute(
            "UPDATE facts SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )

        # 3. Insert new compressed facts
        for fact in compression.compressed_facts:
            await db.execute(
                "INSERT INTO facts (user_id, category, content) VALUES (?, ?, ?)",
                (user_id, fact.category, fact.content)
            )

        # 4. Delete old events for the user
        await db.execute(
            "DELETE FROM events WHERE user_id = ?",
            (user_id,)
        )

        # 5. Insert compressed events
        for event in compression.compressed_events:
            await db.execute(
                "INSERT INTO events (user_id, description, event_date, significance) VALUES (?, ?, ?, ?)",
                (user_id, event.description, event.date, event.significance)
            )

        # 6. Log Mood State if provided
        if compression.emotional_state:
            await db.execute(
                "INSERT INTO emotional_log (user_id, mood, intensity, trigger) VALUES (?, ?, ?, ?)",
                (user_id, compression.emotional_state.mood, compression.emotional_state.intensity, compression.emotional_state.trigger)
            )

        await db.commit()
    except Exception as e:
        await db.execute("ROLLBACK;")
        raise e
```
