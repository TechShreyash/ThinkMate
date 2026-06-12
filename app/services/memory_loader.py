import json
from aiosqlite import Connection
from app.config import config

async def build_memory_block(db: Connection, user_id: int) -> tuple[str, bool]:
    """
    Loads all 4 components of the user's memory from SQLite and compiles them into a structured text block.
    Returns:
        (compiled_memory_text, needs_compression_flag)
    """
    # 1. Fetch profile summary and communication style
    async with db.execute(
        "SELECT profile_summary, communication_style FROM user_profiles WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        profile = await cursor.fetchone()
        profile_summary = profile["profile_summary"] if profile and profile["profile_summary"] else ""
        comm_style = profile["communication_style"] if profile and profile["communication_style"] else ""

    # 2. Fetch active facts
    async with db.execute(
        "SELECT category, content FROM facts WHERE user_id = ? AND is_active = 1",
        (user_id,)
    ) as cursor:
        facts = await cursor.fetchall()

    # 3. Fetch events
    async with db.execute(
        "SELECT description, event_date, significance FROM events WHERE user_id = ? ORDER BY id ASC",
        (user_id,)
    ) as cursor:
        events = await cursor.fetchall()

    # 4. Fetch latest emotional state
    async with db.execute(
        "SELECT mood, intensity, trigger FROM emotional_log WHERE user_id = ? ORDER BY detected_at DESC LIMIT 1",
        (user_id,)
    ) as cursor:
        mood_row = await cursor.fetchone()
        mood_str = ""
        if mood_row:
            mood_str = f"Mood: {mood_row['mood']} (intensity: {mood_row['intensity']})"
            if mood_row['trigger']:
                mood_str += f", Triggered by: {mood_row['trigger']}"

    # Format the sections
    lines = []
    
    # Section 1: User Profile
    lines.append("=== USER PROFILE ===")
    lines.append(f"Summary: {profile_summary}")
    lines.append(f"Communication Style Preference: {comm_style}")
    lines.append("")

    # Section 2: Core Facts
    lines.append("=== CORE FACTS ===")
    if facts:
        for f in facts:
            lines.append(f"- [{f['category']}] {f['content']}")
    else:
        lines.append("(No facts stored)")
    lines.append("")

    # Section 3: Life Events Timeline
    lines.append("=== LIFE EVENTS TIMELINE ===")
    if events:
        for ev in events:
            date_str = f" ({ev['event_date']})" if ev['event_date'] else ""
            lines.append(f"- [{ev['significance']}] {ev['description']}{date_str}")
    else:
        lines.append("(No timeline events logged)")
    lines.append("")

    # Section 4: Current Mood
    lines.append("=== CURRENT MOOD ===")
    if mood_str:
        lines.append(mood_str)
    else:
        lines.append("Mood: calm")
    
    compiled_text = "\n".join(lines)
    
    # Check if compiled text length exceeds the user memory budget
    needs_compression = len(compiled_text) > config.USER_MEMORY_BUDGET_CHARS
    
    return compiled_text, needs_compression
