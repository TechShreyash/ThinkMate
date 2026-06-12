import json
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config

async def build_memory_block(db: AsyncIOMotorDatabase, user_id: int) -> tuple[str, bool]:
    """
    Loads all components of the user's memory from MongoDB and compiles them into a structured text block.
    Returns:
        (compiled_memory_text, needs_compression_flag)
    """
    # Fetch unified user profile
    doc = await db["user_profiles"].find_one({"_id": user_id})
    if not doc:
        doc = {}

    profile_summary = doc.get("profile_summary") or ""
    comm_style = doc.get("communication_style") or ""
    facts = doc.get("facts") or []
    beliefs = doc.get("beliefs") or []
    events = doc.get("events") or []
    emotional_state = doc.get("emotional_state") or None

    mood_str = ""
    if emotional_state:
        mood_str = f"Mood: {emotional_state.get('mood', 'calm')} (intensity: {emotional_state.get('intensity', 0.5)})"
        trigger = emotional_state.get('trigger')
        if trigger:
            mood_str += f", Triggered by: {trigger}"

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
            lines.append(f"- [{f.get('category')}] {f.get('content')}")
    else:
        lines.append("(No facts stored)")
    lines.append("")

    # Section 3: Subjective Beliefs
    lines.append("=== SUBJECTIVE BELIEFS ===")
    if beliefs:
        for b in beliefs:
            lines.append(f"- {b.get('content')}")
    else:
        lines.append("(No beliefs stored)")
    lines.append("")

    # Section 4: Life Events Timeline
    lines.append("=== LIFE EVENTS TIMELINE ===")
    if events:
        for ev in events:
            date_str = f" ({ev.get('event_date')})" if ev.get('event_date') else ""
            emotion_str = f" — felt {ev.get('emotional_context')}" if ev.get('emotional_context') else ""
            lines.append(f"- [{ev.get('significance', 'minor')}] {ev.get('description')}{date_str}{emotion_str}")
    else:
        lines.append("(No timeline events logged)")
    lines.append("")

    # Section 5: Current Mood
    lines.append("=== CURRENT MOOD ===")
    if mood_str:
        lines.append(mood_str)
    else:
        lines.append("Mood: calm")
    
    compiled_text = "\n".join(lines)
    
    # Check if compiled text length exceeds the user memory budget
    needs_compression = len(compiled_text) > config.USER_MEMORY_BUDGET_CHARS
    
    return compiled_text, needs_compression
