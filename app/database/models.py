from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.services.schemas import MemoryExtraction, MemoryCompression

async def ensure_user(db: AsyncIOMotorDatabase, user_id: int, username: str, display_name: str):
    """Upserts the user profile document in the user_profiles collection."""
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {
            "$set": {
                "username": username,
                "display_name": display_name,
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {
                "profile_summary": "",
                "communication_style": "",
                "emotional_state": None,
                "facts": [],
                "beliefs": [],
                "events": [],
                "created_at": datetime.utcnow()
            }
        },
        upsert=True
    )

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

async def get_chat_buffer(db: AsyncIOMotorDatabase, user_id: int) -> list[dict]:
    """Retrieves the array of chat messages in active history for the user."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        return [{"role": m["role"], "content": m["content"]} for m in doc["messages"]]
    return []

async def get_buffer_count(db: AsyncIOMotorDatabase, user_id: int) -> int:
    """Returns the total number of messages in the active chat buffer."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        return len(doc["messages"])
    return 0

async def get_buffer_char_count(db: AsyncIOMotorDatabase, user_id: int) -> int:
    """Returns the sum of character lengths of all messages in the chat buffer."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        return sum(len(m["content"]) for m in doc["messages"])
    return 0

async def delete_oldest_buffer_messages(db: AsyncIOMotorDatabase, user_id: int, count: int):
    """Trims the chat buffer by slicing away the oldest messages from the array."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        remaining = doc["messages"][count:]
        await db["chat_buffers"].update_one(
            {"_id": user_id},
            {"$set": {"messages": remaining, "updated_at": datetime.utcnow()}}
        )

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
    
    # 1. Profile Style
    set_fields = {}
    if extraction.profile_updates and extraction.profile_updates.communication_style:
        set_fields["communication_style"] = extraction.profile_updates.communication_style
        
    # 2. Direct Emotional State Update
    if extraction.emotional_state:
        set_fields["emotional_state"] = {
            "mood": extraction.emotional_state.mood,
            "intensity": extraction.emotional_state.intensity,
            "trigger": extraction.emotional_state.trigger or "",
            "detected_at": now
        }
        
    # 3. Facts CRUD (Hard Deletes)
    removed_contents = {f.content for f in extraction.removed_facts}
    updated_old_contents = {f.old_content for f in extraction.updated_facts}
    exclude_facts = removed_contents.union(updated_old_contents)
    
    # Filter out removed and updated facts
    facts = [f for f in facts if f["content"] not in exclude_facts]
    
    # Append new facts
    for f in extraction.new_facts:
        facts.append({
            "category": f.category,
            "content": f.content,
            "confidence": 1.0,
            "created_at": now,
            "updated_at": now
        })
        
    # Append updated facts (as replacement content)
    for f in extraction.updated_facts:
        facts.append({
            "category": f.category,
            "content": f.new_content,
            "confidence": 1.0,
            "created_at": now,
            "updated_at": now
        })
        
    # 4. Beliefs CRUD (Hard Deletes)
    removed_beliefs = {b.content for b in extraction.removed_beliefs}
    updated_old_beliefs = {b.old_content for b in extraction.updated_beliefs}
    exclude_beliefs = removed_beliefs.union(updated_old_beliefs)
    
    # Filter out removed and updated beliefs
    beliefs = [b for b in beliefs if b["content"] not in exclude_beliefs]
    
    # Append new beliefs
    for b in extraction.new_beliefs:
        beliefs.append({
            "content": b.content,
            "created_at": now,
            "updated_at": now
        })
        
    # Append updated beliefs
    for b in extraction.updated_beliefs:
        beliefs.append({
            "content": b.new_content,
            "created_at": now,
            "updated_at": now
        })
        
    # 5. Events CRUD (Hard Deletes)
    removed_events = {e.description for e in extraction.removed_events}
    updated_old_events = {e.old_description for e in extraction.updated_events}
    exclude_events = removed_events.union(updated_old_events)
    
    # Filter out removed and updated events
    events = [e for e in events if e["description"] not in exclude_events]
    
    # Append new events
    for e in extraction.new_events:
        events.append({
            "description": e.description,
            "event_date": e.date,
            "significance": e.significance,
            "emotional_context": e.emotion or "",
            "created_at": now
        })
        
    # Append updated events (preserving created_at and emotional_context if event existed)
    for update in extraction.updated_events:
        old_ev = next((e for e in profile.get("events", []) if e["description"] == update.old_description), None)
        description = update.new_description
        date = update.date if update.date is not None else (old_ev["event_date"] if old_ev else None)
        significance = update.significance if update.significance is not None else (old_ev["significance"] if old_ev else "minor")
        emotion = old_ev["emotional_context"] if old_ev else ""
        events.append({
            "description": description,
            "event_date": date,
            "significance": significance,
            "emotional_context": emotion,
            "created_at": old_ev["created_at"] if old_ev else now
        })

    # Save consolidated state back to user_profiles
    set_fields["facts"] = facts
    set_fields["beliefs"] = beliefs
    set_fields["events"] = events
    set_fields["updated_at"] = now
    
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": set_fields}
    )

async def replace_user_memory(db: AsyncIOMotorDatabase, user_id: int, compression: MemoryCompression):
    """Replaces profile summary, style preference, facts, beliefs, and events arrays with compressed layouts."""
    now = datetime.utcnow()
    set_fields = {}
    
    if compression.profile_summary is not None:
        set_fields["profile_summary"] = compression.profile_summary
    if compression.communication_style is not None:
        set_fields["communication_style"] = compression.communication_style
    if compression.emotional_state:
        set_fields["emotional_state"] = {
            "mood": compression.emotional_state.mood,
            "intensity": compression.emotional_state.intensity,
            "trigger": compression.emotional_state.trigger or "",
            "detected_at": now
        }
        
    # Compressed Facts
    facts = []
    for fact in compression.compressed_facts:
        facts.append({
            "category": fact.category,
            "content": fact.content,
            "confidence": 1.0,
            "created_at": now,
            "updated_at": now
        })
    set_fields["facts"] = facts
    
    # Compressed Beliefs
    beliefs = []
    for belief in compression.compressed_beliefs:
        beliefs.append({
            "content": belief.content,
            "created_at": now,
            "updated_at": now
        })
    set_fields["beliefs"] = beliefs
    
    # Compressed Events
    events = []
    for event in compression.compressed_events:
        events.append({
            "description": event.description,
            "event_date": event.date,
            "significance": event.significance,
            "emotional_context": "",
            "created_at": now
        })
    set_fields["events"] = events
    set_fields["updated_at"] = now
    
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": set_fields}
    )

async def get_active_facts(db: AsyncIOMotorDatabase, user_id: int) -> list[dict]:
    """Retrieves all active facts inside the user_profiles document (for test compatibility)."""
    doc = await db["user_profiles"].find_one({"_id": user_id})
    if doc and "facts" in doc:
        return [
            {"id": idx, "category": f["category"], "content": f["content"]}
            for idx, f in enumerate(doc["facts"])
        ]
    return []
