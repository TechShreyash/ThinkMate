import os
import aiosqlite
from contextlib import asynccontextmanager
from loguru import logger

DB_PATH = os.path.join("data", "database.sqlite")

async def get_db_connection() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    # Enable WAL mode to prevent locks during concurrent writes
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn

@asynccontextmanager
async def db_session():
    db = await get_db_connection()
    try:
        yield db
    finally:
        await db.close()

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with db_session() as db:
        schema = """
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
        """
        await db.executescript(schema)
        await db.commit()
    logger.info("SQLite database tables initialized successfully.")
