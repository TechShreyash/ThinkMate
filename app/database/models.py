"""MongoDB document accessors (CRUD) for ThinkMate.

Each function takes the active ``AsyncIOMotorDatabase`` as its first argument so sessions
can be injected by middleware and swapped for an in-memory mock under test. All state is
keyed on the Telegram ``user_id`` for strict per-user isolation.

Buffer trimming uses an atomic ``$pull`` on a ``created_at`` cutoff rather than a
read-slice-overwrite, so messages appended concurrently by the chat path are never
clobbered by a background extractor (see docs/development/hardening_plan.md, B1).
"""
import threading
from datetime import datetime, timezone, timedelta
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from app.config import config
from app.services.schemas import MemoryExtraction, MemoryCompression, MemoryConsolidation

_ts_lock = threading.Lock()
_last_ts: datetime | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic_utcnow() -> datetime:
    """Wall-clock UTC, strictly increasing within this process at millisecond resolution.

    Buffer messages are ordered and trimmed by ``created_at``. BSON stores dates with only
    millisecond precision, so the monotonic step must be a full millisecond — otherwise two
    writes in the same millisecond collide once persisted and the cutoff trim can't separate
    them. Real user messages are seconds apart, so this never drifts in practice.
    """
    global _last_ts
    with _ts_lock:
        now = datetime.now(timezone.utc)
        now = now.replace(microsecond=(now.microsecond // 1000) * 1000)  # align to BSON ms
        if _last_ts is not None and now <= _last_ts:
            now = _last_ts + timedelta(milliseconds=1)
        _last_ts = now
        return now


def _normalize(text: str | None) -> str:
    """Casefold + collapse whitespace for tolerant matching of free-text memory items."""
    return " ".join((text or "").split()).casefold()


async def ensure_user(db: AsyncIOMotorDatabase, user_id: int, username: str, display_name: str):
    """Upsert the user profile document in the user_profiles collection."""
    now = _utcnow()
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {
            "$set": {
                "username": username,
                "display_name": display_name,
                "updated_at": now,
            },
            "$setOnInsert": {
                "profile_summary": "",
                "communication_style": "",
                "gender": None,
                "emotional_state": None,
                "facts": [],
                "beliefs": [],
                "events": [],
                "insights": [],
                "mood_history": [],
                "onboarded": False,
                "created_at": now,
            },
        },
        upsert=True,
    )


async def refresh_identity_if_changed(
    db: AsyncIOMotorDatabase, user_id: int, username: str, display_name: str
) -> dict | None:
    """Read the stored profile and write Identity_Fields only when absent or changed.

    Returns a small change descriptor (e.g. {"created": bool, "username": (old, new),
    "display_name": (old, new)}) when a write happened, else None. Never sets or clears
    any Memory_Field. Safe to call on every group message.
    """
    profile = await db["user_profiles"].find_one(
        {"_id": user_id}, {"username": 1, "display_name": 1}
    )

    if profile is None:
        # No profile yet: create one carrying the incoming identity, with the SAME
        # $setOnInsert memory-field skeleton ensure_user uses (empty memory, not empty
        # identity). Use upsert so a concurrent create is idempotent.
        await ensure_user(db, user_id, username, display_name)
        return {"created": True, "username": (None, username),
                "display_name": (None, display_name)}

    set_fields: dict = {}
    if username and profile.get("username") != username:
        set_fields["username"] = username
    if display_name and profile.get("display_name") != display_name:
        set_fields["display_name"] = display_name

    if not set_fields:
        return None  # already current -> no write (Req 1.5)

    set_fields["updated_at"] = _utcnow()
    await db["user_profiles"].update_one({"_id": user_id}, {"$set": set_fields})
    return {"created": False, **{k: (profile.get(k), v)
                                 for k, v in set_fields.items() if k != "updated_at"}}


async def _ensure_memory_skeleton(db: AsyncIOMotorDatabase, user_id: int):
    """Create the memory-field skeleton if absent WITHOUT writing Identity_Fields.

    Uses the SAME ``$setOnInsert`` memory skeleton ``ensure_user`` seeds, but leaves
    ``username``/``display_name`` unset so an absent profile is never stamped with
    empty-string identity (Req 2.1). Safe as a fallback in the memory-write path.
    """
    now = _utcnow()
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {
            "$setOnInsert": {
                "profile_summary": "",
                "communication_style": "",
                "gender": None,
                "emotional_state": None,
                "facts": [],
                "beliefs": [],
                "events": [],
                "insights": [],
                "mood_history": [],
                "onboarded": False,
                "created_at": now,
            }
        },
        upsert=True,
    )


async def export_user_data(db: AsyncIOMotorDatabase, user_id: int) -> dict | None:
    """Return a JSON-serializable snapshot of everything stored for ``user_id``.

    Bundles the full ``user_profiles`` document and the ``chat_buffers`` document so the
    snapshot can be archived (e.g. forwarded to the Logs_Channel as a backup before a
    destructive ``/reset``) and, if ever needed, restored by an admin. Returns ``None``
    when no profile exists, so callers can skip backing up a user with nothing to lose.
    ``ObjectId``/``datetime`` values are left as-is; serialize with ``default=str``.
    """
    profile = await db["user_profiles"].find_one({"_id": user_id})
    if profile is None:
        return None
    chat_buffer = await db["chat_buffers"].find_one({"_id": user_id})
    return {
        "user_id": user_id,
        "exported_at": _utcnow(),
        "user_profiles": profile,
        "chat_buffers": chat_buffer,
    }


async def reset_user(db: AsyncIOMotorDatabase, user_id: int):
    """Hard-delete all stored state for a user (profile + chat buffer)."""
    await db["user_profiles"].delete_one({"_id": user_id})
    await db["chat_buffers"].delete_one({"_id": user_id})


async def add_message_to_buffer(
    db: AsyncIOMotorDatabase,
    chat_id: int,
    role: str,
    content: str,
    *,
    sender_id: int | None = None,
    sender_name: str = "",
) -> list[dict]:
    """Append a message to the ``chat_id``-keyed buffer and return the messages array.

    The buffer is keyed by ``chat_id``; in a DM ``chat_id == user_id`` so the on-disk
    document (``_id``) is unchanged from current behavior. Each pushed message now also
    carries ``sender_id``/``sender_name`` for multi-party group context. When
    ``sender_id`` is omitted it defaults to ``chat_id``, preserving DM semantics (a DM's
    only speaker is the user, whose id equals the chat id).

    Returning the post-update array lets the caller derive the char count and active
    history without extra round-trips. A ``$slice`` hard cap bounds the array so a
    stalled extractor can never let it grow without limit.
    """
    if sender_id is None:
        sender_id = chat_id
    now = _monotonic_utcnow()
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
                    "$slice": -config.CHAT_BUFFER_HARD_CAP,
                }
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc.get("messages", []) if doc else []


async def get_chat_buffer(db: AsyncIOMotorDatabase, user_id: int) -> list[dict]:
    """Return the active chat history as role/content dicts (LLM message format)."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        return [{"role": m["role"], "content": m["content"]} for m in doc["messages"]]
    return []


async def get_buffer_count(db: AsyncIOMotorDatabase, user_id: int) -> int:
    """Return the number of messages in the active chat buffer."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        return len(doc["messages"])
    return 0


async def get_buffer_char_count(db: AsyncIOMotorDatabase, user_id: int) -> int:
    """Return the summed character length of all messages in the chat buffer."""
    doc = await db["chat_buffers"].find_one({"_id": user_id})
    if doc and "messages" in doc:
        return sum(len(m["content"]) for m in doc["messages"])
    return 0


async def count_memory_items(db: AsyncIOMotorDatabase, user_id: int) -> int:
    """Return the total count of stored memory items (facts + beliefs + events).

    Used to decide whether a user is "new"/sparse so the extraction trigger can fire
    sooner for them. Reads only the three array fields via projection (cheap). Returns 0
    when the profile is absent.
    """
    doc = await db["user_profiles"].find_one(
        {"_id": user_id}, {"facts": 1, "beliefs": 1, "events": 1}
    )
    if not doc:
        return 0
    return (
        len(doc.get("facts") or [])
        + len(doc.get("beliefs") or [])
        + len(doc.get("events") or [])
    )


async def delete_oldest_buffer_messages(db: AsyncIOMotorDatabase, user_id: int, count: int):
    """Atomically trim the ``count`` oldest messages from the buffer.

    Removal is done with ``$pull`` on a ``created_at`` cutoff, so messages appended after
    this snapshot (e.g. by a concurrent chat batch) are preserved rather than clobbered.
    """
    if count <= 0:
        return
    doc = await db["chat_buffers"].find_one({"_id": user_id}, {"messages": 1})
    if not doc or "messages" not in doc:
        return
    messages = doc["messages"]
    if count >= len(messages):
        await db["chat_buffers"].update_one(
            {"_id": user_id}, {"$set": {"messages": [], "updated_at": _utcnow()}}
        )
        return
    cutoff = messages[count].get("created_at")
    if cutoff is None:
        # Legacy messages without created_at: fall back to a positional slice.
        await db["chat_buffers"].update_one(
            {"_id": user_id},
            {"$set": {"messages": messages[count:], "updated_at": _utcnow()}},
        )
        return
    await db["chat_buffers"].update_one(
        {"_id": user_id},
        {
            "$pull": {"messages": {"created_at": {"$lt": cutoff}}},
            "$set": {"updated_at": _utcnow()},
        },
    )


async def save_extracted_memories(
    db: AsyncIOMotorDatabase, user_id: int, extraction: MemoryExtraction
):
    """Apply extracted profile style, facts, beliefs, events, and mood to the user record.

    Free-text matches (removals/updates) are normalized (casefold + whitespace-collapse)
    so minor LLM phrasing drift still resolves to the stored item, and new items are
    de-duplicated against existing ones.
    """
    profile = await db["user_profiles"].find_one({"_id": user_id})
    if not profile:
        await _ensure_memory_skeleton(db, user_id)
        profile = await db["user_profiles"].find_one({"_id": user_id})

    facts = profile.get("facts", [])
    beliefs = profile.get("beliefs", [])
    events = profile.get("events", [])
    now = _utcnow()

    set_fields: dict = {}

    # 1. Profile style
    if extraction.profile_updates and extraction.profile_updates.communication_style:
        set_fields["communication_style"] = extraction.profile_updates.communication_style

    # 1b. Gender (only overwrite when the extractor is confident enough to emit a value).
    if extraction.profile_updates and extraction.profile_updates.gender:
        set_fields["gender"] = extraction.profile_updates.gender

    # 2. Direct emotional state update
    if extraction.emotional_state:
        mood_entry = {
            "mood": extraction.emotional_state.mood,
            "intensity": extraction.emotional_state.intensity,
            "trigger": extraction.emotional_state.trigger or "",
            "detected_at": now,
        }
        set_fields["emotional_state"] = mood_entry
        # Also append to the bounded mood_history (oldest dropped past MAX_MOOD_HISTORY).
        mood_history = list(profile.get("mood_history") or [])
        mood_history.append(mood_entry)
        set_fields["mood_history"] = mood_history[-config.MAX_MOOD_HISTORY:]

    # 3. Facts CRUD (hard deletes, normalized matching)
    exclude_facts = {_normalize(f.content) for f in extraction.removed_facts}
    exclude_facts |= {_normalize(f.old_content) for f in extraction.updated_facts}
    facts = [f for f in facts if _normalize(f["content"]) not in exclude_facts]

    seen_facts = {_normalize(f["content"]) for f in facts}
    for f in extraction.new_facts:
        key = _normalize(f.content)
        if key in seen_facts:
            continue
        seen_facts.add(key)
        facts.append({
            "category": f.category, "content": f.content,
            "confidence": 1.0, "created_at": now, "updated_at": now,
        })
    for f in extraction.updated_facts:
        key = _normalize(f.new_content)
        if key in seen_facts:
            continue
        seen_facts.add(key)
        facts.append({
            "category": f.category, "content": f.new_content,
            "confidence": 1.0, "created_at": now, "updated_at": now,
        })

    # 4. Beliefs CRUD
    exclude_beliefs = {_normalize(b.content) for b in extraction.removed_beliefs}
    exclude_beliefs |= {_normalize(b.old_content) for b in extraction.updated_beliefs}
    beliefs = [b for b in beliefs if _normalize(b["content"]) not in exclude_beliefs]

    seen_beliefs = {_normalize(b["content"]) for b in beliefs}
    for b in extraction.new_beliefs:
        key = _normalize(b.content)
        if key in seen_beliefs:
            continue
        seen_beliefs.add(key)
        beliefs.append({"content": b.content, "created_at": now, "updated_at": now})
    for b in extraction.updated_beliefs:
        key = _normalize(b.new_content)
        if key in seen_beliefs:
            continue
        seen_beliefs.add(key)
        beliefs.append({"content": b.new_content, "created_at": now, "updated_at": now})

    # 5. Events CRUD
    exclude_events = {_normalize(e.description) for e in extraction.removed_events}
    exclude_events |= {_normalize(e.old_description) for e in extraction.updated_events}
    original_events = list(profile.get("events", []))
    events = [e for e in events if _normalize(e["description"]) not in exclude_events]

    seen_events = {_normalize(e["description"]) for e in events}
    for e in extraction.new_events:
        key = _normalize(e.description)
        if key in seen_events:
            continue
        seen_events.add(key)
        events.append({
            "description": e.description, "event_date": e.date,
            "significance": e.significance, "emotional_context": e.emotion or "",
            "created_at": now,
        })
    for update in extraction.updated_events:
        old_ev = next(
            (e for e in original_events if _normalize(e["description"]) == _normalize(update.old_description)),
            None,
        )
        events.append({
            "description": update.new_description,
            "event_date": update.date if update.date is not None else (old_ev["event_date"] if old_ev else None),
            "significance": update.significance if update.significance is not None else (old_ev["significance"] if old_ev else "minor"),
            "emotional_context": old_ev["emotional_context"] if old_ev else "",
            "created_at": old_ev["created_at"] if old_ev else now,
        })

    set_fields["facts"] = facts
    set_fields["beliefs"] = beliefs
    set_fields["events"] = events
    set_fields["updated_at"] = now

    await db["user_profiles"].update_one({"_id": user_id}, {"$set": set_fields})


async def replace_user_memory(
    db: AsyncIOMotorDatabase, user_id: int, compression: MemoryCompression
):
    """Replace profile summary, style, facts, beliefs, and events with compressed layouts."""
    now = _utcnow()
    set_fields: dict = {}

    if compression.profile_summary is not None:
        set_fields["profile_summary"] = compression.profile_summary
    if compression.communication_style is not None:
        set_fields["communication_style"] = compression.communication_style
    if compression.emotional_state:
        set_fields["emotional_state"] = {
            "mood": compression.emotional_state.mood,
            "intensity": compression.emotional_state.intensity,
            "trigger": compression.emotional_state.trigger or "",
            "detected_at": now,
        }

    set_fields["facts"] = [
        {"category": fact.category, "content": fact.content,
         "confidence": 1.0, "created_at": now, "updated_at": now}
        for fact in compression.compressed_facts
    ]
    set_fields["beliefs"] = [
        {"content": belief.content, "created_at": now, "updated_at": now}
        for belief in compression.compressed_beliefs
    ]
    set_fields["events"] = [
        {"description": event.description, "event_date": event.date,
         "significance": event.significance, "emotional_context": "", "created_at": now}
        for event in compression.compressed_events
    ]
    set_fields["updated_at"] = now

    await db["user_profiles"].update_one({"_id": user_id}, {"$set": set_fields})


async def find_users_due_for_consolidation(
    db: AsyncIOMotorDatabase, *, interval_secs: float, min_items: int, limit: int
) -> list[int]:
    """Return up to ``limit`` user ids due for consolidation.

    Due = ``last_consolidated_at`` null/absent OR older than ``now - interval_secs``, AND
    ``len(facts)+len(beliefs)+len(events) >= min_items``. The time predicate runs in the
    query; the item-count threshold is applied in Python (array-length predicates aren't
    portable to mongomock). Collection stops once ``limit`` qualifying users are found, so
    the helper's own work is bounded (Req 1.9).
    """
    cutoff = _utcnow() - timedelta(seconds=interval_secs)
    query = {
        "$or": [
            {"last_consolidated_at": {"$exists": False}},
            {"last_consolidated_at": None},
            {"last_consolidated_at": {"$lt": cutoff}},
        ]
    }
    due: list[int] = []
    async for doc in db["user_profiles"].find(query, {"facts": 1, "beliefs": 1, "events": 1}):
        count = (
            len(doc.get("facts") or [])
            + len(doc.get("beliefs") or [])
            + len(doc.get("events") or [])
        )
        if count >= min_items:
            due.append(doc["_id"])
            if len(due) >= limit:
                break
    return due


async def apply_consolidation(
    db: AsyncIOMotorDatabase, user_id: int, consolidation: MemoryConsolidation
):
    """Single-``$set`` apply of a consolidation result + ``last_consolidated_at``.

    Mirrors ``replace_user_memory``'s single-write style: refreshes summary/style (only
    when present), replaces facts/beliefs/events with merged layouts, preserves the latest
    emotional state, writes ``insights`` truncated to ``config.MAX_INSIGHTS`` (Req 8.4),
    and advances ``last_consolidated_at`` / ``updated_at``.
    """
    now = _utcnow()
    set_fields: dict = {}

    if consolidation.profile_summary is not None:
        set_fields["profile_summary"] = consolidation.profile_summary
    if consolidation.communication_style is not None:
        set_fields["communication_style"] = consolidation.communication_style
    if consolidation.emotional_state:
        set_fields["emotional_state"] = {
            "mood": consolidation.emotional_state.mood,
            "intensity": consolidation.emotional_state.intensity,
            "trigger": consolidation.emotional_state.trigger or "",
            "detected_at": now,
        }

    set_fields["facts"] = [
        {"category": fact.category, "content": fact.content,
         "confidence": 1.0, "created_at": now, "updated_at": now}
        for fact in consolidation.consolidated_facts
    ]
    set_fields["beliefs"] = [
        {"content": belief.content, "created_at": now, "updated_at": now}
        for belief in consolidation.consolidated_beliefs
    ]
    set_fields["events"] = [
        {"description": event.description, "event_date": event.date,
         "significance": event.significance, "emotional_context": "", "created_at": now}
        for event in consolidation.consolidated_events
    ]
    set_fields["insights"] = [
        {"content": ins.content, "created_at": now, "updated_at": now}
        for ins in consolidation.insights[: config.MAX_INSIGHTS]
    ]
    set_fields["last_consolidated_at"] = now
    set_fields["updated_at"] = now

    await db["user_profiles"].update_one({"_id": user_id}, {"$set": set_fields})


async def get_active_facts(db: AsyncIOMotorDatabase, user_id: int) -> list[dict]:
    """Return all active facts in the user_profiles document (used by tests)."""
    doc = await db["user_profiles"].find_one({"_id": user_id})
    if doc and "facts" in doc:
        return [
            {"id": idx, "category": f["category"], "content": f["content"]}
            for idx, f in enumerate(doc["facts"])
        ]
    return []


# --- Engagement / proactive check-ins (Phase 12) ---


async def touch_and_get_last_interaction(
    db: AsyncIOMotorDatabase, user_id: int, *, now=None
) -> "datetime | None":
    """Record ``last_interaction_at = now`` and return the *previous* value, in one round-trip.

    A single ``find_one_and_update`` with ``ReturnDocument.BEFORE`` reads the prior
    timestamp (for the temporal "last talked" gap) and writes the new one atomically.
    Does **not** upsert (Req 2.3): a user without a profile is a no-op returning ``None``.

    Engaging in a DM also clears ``proactive_unanswered`` (the consecutive-unanswered
    check-in streak), so a user who replies becomes eligible for proactive check-ins
    again after they had been auto-paused for going silent.
    """
    now = now or _utcnow()
    doc = await db["user_profiles"].find_one_and_update(
        {"_id": user_id},
        {"$set": {"last_interaction_at": now, "proactive_unanswered": 0}},
        projection={"last_interaction_at": 1},
        return_document=ReturnDocument.BEFORE,
        upsert=False,
    )
    return (doc or {}).get("last_interaction_at")


async def set_proactive_enabled(db: AsyncIOMotorDatabase, user_id: int, enabled: bool):
    """Single-``$set`` toggle of the per-user proactive opt-out flag."""
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"proactive_enabled": enabled, "updated_at": _utcnow()}},
    )


async def get_proactive_enabled(db: AsyncIOMotorDatabase, user_id: int) -> bool:
    """Return whether proactive check-ins are enabled for ``user_id`` (default: enabled).

    Mirrors the due-user query's ``proactive_enabled != False`` rule: an absent profile or
    absent flag means the user never opted out, so check-ins are allowed.
    """
    doc = await db["user_profiles"].find_one(
        {"_id": user_id}, {"proactive_enabled": 1}
    )
    if not doc:
        return True
    return doc.get("proactive_enabled", True)


async def get_reactions_enabled(db: AsyncIOMotorDatabase, user_id: int) -> bool:
    """Return whether the bot may add emoji reactions to ``user_id``'s messages.

    Per-user opt-out (default: enabled). An absent profile or absent flag means the user
    never opted out, so reactions are allowed. The reply hot path does NOT call this — it
    reads the flag straight off the profile doc it already fetches for the memory block
    (see ``chat_manager.handle_message``) to avoid a second round-trip. Kept as a small,
    correct accessor for callers/tests that only need the flag.
    """
    doc = await db["user_profiles"].find_one(
        {"_id": user_id}, {"reactions_enabled": 1}
    )
    if not doc:
        return True
    return doc.get("reactions_enabled", True)


async def set_reactions_enabled(db: AsyncIOMotorDatabase, user_id: int, enabled: bool):
    """Single-``$set`` toggle of the per-user emoji-reaction opt-out flag."""
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"reactions_enabled": enabled, "updated_at": _utcnow()}},
    )


# Fixed _id for the single document holding the persisted observability metrics state.
_METRICS_STATE_ID = "metrics:singleton"


async def save_metrics_state(db: AsyncIOMotorDatabase, state: dict) -> None:
    """Upsert the process-wide metrics snapshot into the ``metrics_state`` collection.

    A single document keyed on :data:`_METRICS_STATE_ID` holds the latest
    ``metrics.snapshot()`` so counters/gauges/timers survive a restart. Best-effort:
    metrics are observability data, never worth raising into the caller, so failures are
    logged at debug and swallowed.
    """
    try:
        await db["metrics_state"].update_one(
            {"_id": _METRICS_STATE_ID},
            {"$set": {"state": state, "updated_at": _utcnow()}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.debug(f"save_metrics_state failed: {exc}")


async def load_metrics_state(db: AsyncIOMotorDatabase) -> dict:
    """Return the persisted metrics state dict, or ``{}`` when none/unreadable.

    Mirrors :func:`save_metrics_state`; never raises so a missing or malformed document
    can't block startup.
    """
    try:
        doc = await db["metrics_state"].find_one({"_id": _METRICS_STATE_ID})
        state = (doc or {}).get("state")
        return state if isinstance(state, dict) else {}
    except Exception as exc:  # noqa: BLE001 - load is best-effort
        logger.debug(f"load_metrics_state failed: {exc}")
        return {}


async def set_onboarded(db: AsyncIOMotorDatabase, user_id: int, value: bool = True):
    """Single-``$set`` write of the onboarding flag."""
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"onboarded": value, "updated_at": _utcnow()}},
    )


async def set_last_proactive(db: AsyncIOMotorDatabase, user_id: int, *, now=None):
    """Single-``$set`` write of ``last_proactive_at`` (holds the rate-limit window)."""
    now = now or _utcnow()
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"last_proactive_at": now, "updated_at": now}},
    )


async def increment_proactive_unanswered(db: AsyncIOMotorDatabase, user_id: int):
    """Bump the consecutive-unanswered proactive check-in counter by one.

    Called only when a check-in is actually delivered. Once the counter reaches
    ``config.PROACTIVE_MAX_UNANSWERED`` the user is skipped by the scan until they engage
    (a DM message or any command), which resets it to 0.
    """
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$inc": {"proactive_unanswered": 1}, "$set": {"updated_at": _utcnow()}},
    )


async def reset_proactive_unanswered(db: AsyncIOMotorDatabase, user_id: int):
    """Clear the consecutive-unanswered proactive streak (user engaged).

    Best-effort, no upsert: a user without a profile is a no-op. Used by the command
    path; the DM chat path clears it inline via ``touch_and_get_last_interaction``.
    """
    await db["user_profiles"].update_one(
        {"_id": user_id},
        {"$set": {"proactive_unanswered": 0, "updated_at": _utcnow()}},
    )


async def find_users_due_for_proactive(
    db: AsyncIOMotorDatabase,
    *,
    inactivity_secs: float,
    min_interval_secs: float,
    limit: int,
    max_unanswered: int = 0,
    now=None,
) -> list[int]:
    """Return up to ``limit`` user ids due for a proactive check-in.

    Due = ``last_interaction_at`` present AND older than ``now - inactivity_secs`` (the
    ``$lt`` cutoff naturally excludes profiles where it's absent — a user who never
    interacted is never due), AND (``last_proactive_at`` null/absent OR older than
    ``now - min_interval_secs``), AND ``proactive_enabled != False`` (absent/true is
    eligible). The grounding threshold (``>= config.PROACTIVE_MIN_ITEMS`` total
    facts+beliefs+events) is applied in Python so it stays mongomock-friendly. When
    ``max_unanswered > 0``, a user whose ``proactive_unanswered`` streak has reached that
    cap is skipped (auto-paused for going silent) until they engage and reset it.
    Collection stops once ``limit`` qualifying users are found, bounding the helper's own
    work.
    """
    now = now or _utcnow()
    inactive_cutoff = now - timedelta(seconds=inactivity_secs)
    nudge_cutoff = now - timedelta(seconds=min_interval_secs)
    query = {
        "last_interaction_at": {"$lt": inactive_cutoff},
        "proactive_enabled": {"$ne": False},
        "$or": [
            {"last_proactive_at": {"$exists": False}},
            {"last_proactive_at": None},
            {"last_proactive_at": {"$lt": nudge_cutoff}},
        ],
    }
    due: list[int] = []
    async for doc in db["user_profiles"].find(
        query, {"facts": 1, "beliefs": 1, "events": 1, "proactive_unanswered": 1}
    ):
        # Auto-pause: a user who hasn't engaged after `max_unanswered` delivered
        # check-ins is skipped until they reset the streak by talking to the bot.
        if max_unanswered > 0 and (doc.get("proactive_unanswered") or 0) >= max_unanswered:
            continue
        count = (
            len(doc.get("facts") or [])
            + len(doc.get("beliefs") or [])
            + len(doc.get("events") or [])
        )
        if count >= config.PROACTIVE_MIN_ITEMS:
            due.append(doc["_id"])
            if len(due) >= limit:
                break
    return due


# --- chat_members (per-(chat, user) affinity & mode) ---
_VALID_MODES = {"auto", "quiet", "chatty"}


def _chat_member_id(chat_id: int, user_id: int) -> str:
    """Composite key for a chat_members document: ``"{chat_id}:{user_id}"``."""
    return f"{chat_id}:{user_id}"


async def get_chat_member(
    db: AsyncIOMotorDatabase, chat_id: int, user_id: int
) -> dict | None:
    """Return the ``chat_members`` document for (chat_id, user_id), or None if absent."""
    return await db["chat_members"].find_one({"_id": _chat_member_id(chat_id, user_id)})


async def upsert_chat_member(
    db: AsyncIOMotorDatabase,
    chat_id: int,
    user_id: int,
    *,
    affinity: float | None = None,
    mode: str | None = None,
) -> dict:
    """Upsert a ``chat_members`` record keyed ``"{chat_id}:{user_id}"`` and return it.

    Affinity values are clamped to the inclusive range [0.0, 1.0] before writing. An
    invalid ``mode`` is coerced to ``"auto"`` (with a warning) rather than raising, so a
    bad signal can never block the hot path. Defaults (``AFFINITY_DEFAULT``, ``"auto"``,
    ``created_at``) are applied only on insert via ``$setOnInsert``, mirroring
    ``ensure_user``'s single read-modify-write style.
    """
    now = _utcnow()
    set_fields: dict = {
        "chat_id": chat_id,
        "user_id": user_id,
        "updated_at": now,
    }

    if affinity is not None:
        set_fields["affinity"] = max(0.0, min(1.0, affinity))

    if mode is not None:
        if mode not in _VALID_MODES:
            logger.warning(
                "upsert_chat_member: invalid mode {!r} for {}:{}, coercing to 'auto'",
                mode, chat_id, user_id,
            )
            mode = "auto"
        set_fields["mode"] = mode

    set_on_insert: dict = {"created_at": now}
    if "affinity" not in set_fields:
        set_on_insert["affinity"] = config.AFFINITY_DEFAULT
    if "mode" not in set_fields:
        set_on_insert["mode"] = "auto"

    return await db["chat_members"].find_one_and_update(
        {"_id": _chat_member_id(chat_id, user_id)},
        {"$set": set_fields, "$setOnInsert": set_on_insert},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


# --- group_settings (per-chat bot on/off kill switch) ---


async def is_group_enabled(db: AsyncIOMotorDatabase, chat_id: int) -> bool:
    """Return whether the bot is enabled in ``chat_id`` (default: enabled).

    A group is "on" unless an admin has explicitly turned it off via ``/groupoff``.
    An absent document means the group was never toggled, so the bot is active. This is
    read on every group message, so it must be cheap and never raise — the caller treats
    any failure as "enabled" so a transient DB hiccup can never silence the bot.
    """
    doc = await db["group_settings"].find_one({"_id": chat_id}, {"enabled": 1})
    if not doc:
        return True
    return doc.get("enabled", True)


async def set_group_enabled(db: AsyncIOMotorDatabase, chat_id: int, enabled: bool):
    """Upsert the per-chat enabled flag (the ``/groupon`` / ``/groupoff`` kill switch)."""
    now = _utcnow()
    await db["group_settings"].update_one(
        {"_id": chat_id},
        {"$set": {"enabled": enabled, "updated_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


# Valid group-wide ambient modes. "auto" = no group override (defer to each member's
# own /quiet|/chatty); "quiet"/"chatty" are admin-set overrides applied group-wide.
VALID_GROUP_MODES: tuple[str, ...] = ("auto", "quiet", "chatty")


async def get_group_mode(db: AsyncIOMotorDatabase, chat_id: int) -> str:
    """Return the group-wide ambient mode for ``chat_id`` (default: ``"auto"``).

    ``"auto"`` means there is no group-level override, so each member's own mode applies.
    ``"quiet"`` / ``"chatty"`` are admin-set overrides that take priority over any user's
    personal ``/quiet`` / ``/chatty`` preference in this group. Read on the ambient hot
    path, so it must be cheap and never raise — the caller treats any failure as
    ``"auto"`` (no override) so a transient DB hiccup never changes behavior. An unknown
    stored value also degrades to ``"auto"``.
    """
    doc = await db["group_settings"].find_one({"_id": chat_id}, {"group_mode": 1})
    if not doc:
        return "auto"
    mode = doc.get("group_mode", "auto")
    return mode if mode in VALID_GROUP_MODES else "auto"


async def set_group_mode(db: AsyncIOMotorDatabase, chat_id: int, mode: str):
    """Upsert the per-chat group-wide ambient mode (the ``/groupquiet`` etc. commands).

    An invalid ``mode`` is coerced to ``"auto"`` so the stored value is always valid.
    """
    if mode not in VALID_GROUP_MODES:
        mode = "auto"
    now = _utcnow()
    await db["group_settings"].update_one(
        {"_id": chat_id},
        {"$set": {"group_mode": mode, "updated_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
