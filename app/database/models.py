from aiosqlite import Connection
from app.services.schemas import MemoryExtraction, MemoryCompression

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
        return row["cnt"] if row else 0

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
                "UPDATE facts SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND content = ? AND is_active = 1",
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
        f"UPDATE facts SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
        tuple(ids)
    )
    await db.commit()

async def replace_user_memory(db: Connection, user_id: int, compression: MemoryCompression):
    """Transactionally updates the profile and completely replaces active facts and events with the compressed ones."""
    await db.execute("BEGIN TRANSACTION;")
    try:
        # 1. Update Profile summary and communication style if provided
        if compression.profile_summary is not None or compression.communication_style is not None:
            if compression.profile_summary is not None and compression.communication_style is not None:
                await db.execute(
                    """
                    UPDATE user_profiles 
                    SET profile_summary = ?, communication_style = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (compression.profile_summary, compression.communication_style, user_id)
                )
            elif compression.profile_summary is not None:
                await db.execute(
                    """
                    UPDATE user_profiles 
                    SET profile_summary = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (compression.profile_summary, user_id)
                )
            elif compression.communication_style is not None:
                await db.execute(
                    """
                    UPDATE user_profiles 
                    SET communication_style = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
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
                """
                INSERT INTO events (user_id, description, event_date, significance) 
                VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    event.description,
                    event.date,
                    event.significance
                )
            )

        # 6. Log Mood State if provided
        if compression.emotional_state:
            await db.execute(
                "INSERT INTO emotional_log (user_id, mood, intensity, trigger) VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    compression.emotional_state.mood,
                    compression.emotional_state.intensity,
                    compression.emotional_state.trigger
                )
            )

        await db.commit()
    except Exception as e:
        await db.execute("ROLLBACK;")
        raise e

