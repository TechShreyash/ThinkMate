"""Response-flow orchestrator.

For each user batch: append to the buffer, trigger background extraction on overflow,
assemble the system prompt (cached persona + compiled memory), generate the reply and an
optional reaction in a single LLM call, persist the reply, and trigger background
compression (rate-limited) when the memory profile outgrows its budget.
"""
import os
import asyncio
from datetime import datetime, timedelta, timezone
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.affinity import affinity_cache
from app.services.group_gate import scan_negative_signal
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block, load_profile_doc, compile_memory_block
from app.prompts.system_prompt import build_system_prompt

# Representative UTC offsets so the model never needs to do its own timezone math.
# Covers: South Asia, US East/West, UK, Central Europe, Gulf, East Asia.
# Standard offsets are close enough — the model can handle minor DST shifts.
_REFERENCE_ZONES: list[tuple[str, float]] = [
    ("India", 5.5),
    ("US-East", -5),
    ("US-West", -8),
    ("UK", 0),
    ("Europe-Central", 1),
    ("Dubai", 4),
    ("Japan", 9),
]


def build_time_context(now: datetime, last_interaction_at) -> str:
    """A short time-context string for the system prompt: current UTC time, reference
    local times for major zones, and a coarse 'last talked' gap when a previous
    interaction exists (never raw seconds, never a gap on first contact)."""
    lines = [f"Current time (UTC): {now.strftime('%Y-%m-%d %H:%M')}"]
    # Compact local-time references so the model can answer date/time questions
    # from users worldwide without doing timezone arithmetic.
    refs = []
    for label, offset_hours in _REFERENCE_ZONES:
        local = now + timedelta(hours=offset_hours)
        refs.append(f"{label} {local.strftime('%H:%M %b %d')}")
    lines.append("Local times: " + " · ".join(refs))
    lines.append("For any other region, derive the local time from UTC.")
    if last_interaction_at is not None:
        try:
            delta = now - last_interaction_at
            secs = max(0, int(delta.total_seconds()))
            if secs < 3600:
                ago = f"{secs // 60} minute(s) ago"
            elif secs < 86400:
                ago = f"{secs // 3600} hour(s) ago"
            else:
                ago = f"{secs // 86400} day(s) ago"
            lines.append(f"Last talked with this user: {ago}")
        except Exception:
            pass
    return "\n".join(lines)


_DEFAULT_PERSONA = f"You are {config.bot_display_name}, a warm, witty AI companion."
_persona_cache: dict = {"path": None, "mtime": None, "content": _DEFAULT_PERSONA}

# Affinity-down step applied to the speaker when a cheap "back off" keyword
# (stop / quiet / spam / annoying / shut up) is detected in their message
# (Requirement 4.5). AffinityCache.bump clamps the result to [0, 1].
_NEGATIVE_AFFINITY_STEP = -0.1


def _load_persona() -> str:
    """Return the persona text, re-reading the file only when it changes on disk."""
    path = config.PERSONA_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _DEFAULT_PERSONA
    if _persona_cache["path"] != path or _persona_cache["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as f:
                _persona_cache.update(path=path, mtime=mtime, content=f.read())
        except OSError as e:
            logger.warning(f"Could not read persona file {path}: {e}")
            return _persona_cache["content"] or _DEFAULT_PERSONA
    return _persona_cache["content"]


async def handle_message(
    db: AsyncIOMotorDatabase,
    chat_id: int,
    user_text: str,
    *,
    chat_type: str = "private",
    sender_id: int | None = None,
    sender_name: str = "",
    reason: str = "reply",
    participants: dict[int, str] | None = None,
) -> tuple[str, str | None]:
    """Process one combined message; return ``(reply_text, reaction_emoji_or_None)``.

    Buffers are keyed by ``chat_id`` (in a DM, ``chat_id == user_id``, so the on-disk
    document is unchanged). New parameters are keyword-only with DM-safe defaults, so the
    existing call ``handle_message(db, user_id, text)`` is unchanged in meaning
    (``chat_type="private"``, ``sender_id == chat_id``).

    DM path (``chat_type == "private"``): byte-for-byte identical to the original — a
    single-party history, the same reply call (2-tuple ``(reply, reaction)``), and the
    same memory pipeline. The ambient gate, addressed-detection, and affinity logic never
    run here (Requirement 1.5).

    Group path (``group``/``supergroup``): renders a multi-party history attributed by
    ``sender_name`` (Requirement 2.7) and obtains an optional ``affinity_delta`` from the
    reply call (Requirement 4.6) without changing the return contract.
    """
    # In a DM the only speaker is the user, whose id equals the chat id.
    if sender_id is None:
        sender_id = chat_id
    is_group = chat_type in ("group", "supergroup")

    # 1. Append user message with sender attribution; the returned array gives us char
    #    count + active history in a single round-trip.
    messages = await models.add_message_to_buffer(
        db, chat_id, "user", user_text, sender_id=sender_id, sender_name=sender_name
    )
    buffer_chars = sum(len(m["content"]) for m in messages)

    # History rendering: DMs stay single-party (exact current behavior); groups are
    # rendered multi-party so the model can distinguish speakers. Every turn — including
    # the bot's own past replies — is prefixed with the speaker's stored name, so the
    # transcript reads as a consistent named multi-party log ("Alice: …", "Bob: …",
    # "<bot>: …"). The model is told (in the system prompt's GROUP CHAT section) that the
    # name before the first colon is attribution, not message text, and that it must not
    # prefix its own reply with a name.
    if is_group:
        active_history = []
        for m in messages:
            name = m.get("sender_name") or ""
            content = f"{name}: {m['content']}" if name else m["content"]
            active_history.append({"role": m["role"], "content": content})
    else:
        active_history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # 2. Buffer overflow -> non-blocking background extraction. Keyed by chat_id; the
    #    known group flag is passed through to the extractor so extraction dispatch is
    #    authoritative rather than relying on a sender-count heuristic.
    #    New/sparse users extract sooner (NEW_USER_EXTRACTION_CHARS) so their profile
    #    builds quickly; established users use the normal CHAT_BUFFER_MAX_CHARS threshold.
    extraction_threshold = config.CHAT_BUFFER_MAX_CHARS
    try:
        if await models.count_memory_items(db, chat_id) < config.NEW_USER_MEMORY_THRESHOLD:
            extraction_threshold = min(
                config.NEW_USER_EXTRACTION_CHARS, config.CHAT_BUFFER_MAX_CHARS
            )
    except Exception as e:  # noqa: BLE001 - never let the threshold check break the hot path
        logger.debug(f"new-user extraction threshold check failed for chat {chat_id}: {e}")

    if buffer_chars >= extraction_threshold:
        from app.services.user_task_manager import user_task_manager
        logger.info(
            f"Buffer overflow for chat {chat_id} ({buffer_chars} chars >= "
            f"{extraction_threshold}); launching extraction."
        )
        asyncio.create_task(user_task_manager.run_extractor(chat_id, is_group=is_group))

    # 3. Assemble system prompt (cached persona + compiled memory).
    persona = _load_persona()
    now = datetime.now(timezone.utc)

    # Whether the bot may add an emoji reaction to THIS sender's message (/reactions
    # opt-out, default enabled). Read for free from the sender's profile doc that the
    # memory-block load below already fetches — no extra round-trip. Degrades to enabled
    # if that doc is unavailable, so a read failure never silently suppresses reactions.
    sender_reactions_enabled = True

    if is_group:
        # Group block keyed by chat_id (existing behavior); needs_compression continues to
        # track the GROUP block (Req 3.2).
        group_block, needs_compression = await build_memory_block(db, chat_id)
        # Per-user block for the TRIGGERING sender only (Req 3.1, 3.4). Degrade to
        # group-only on failure without raising (Req 3.7). Reuse the sender's profile doc
        # to read the per-user reaction preference in the same read.
        user_block = ""
        try:
            sender_doc = await load_profile_doc(db, sender_id)
            user_block, _ = compile_memory_block(sender_doc)
            sender_reactions_enabled = sender_doc.get("reactions_enabled", True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"per-user memory load failed for sender {sender_id}: {e}")
            user_block = ""
        # Both blocks are included; the per-user block is added, never replaces the
        # group block (Req 3.3, 3.5).
        time_context = build_time_context(now, None)
        system_prompt = build_system_prompt(
            persona, group_block, time_context=time_context, user_memory_text=user_block,
            speaker_name=sender_name, is_group=True, bot_name=config.bot_display_name,
        )
    else:
        # DM path: byte-for-byte unchanged (Req 3.6, 5.2). The single profile read here
        # also carries the sender's reaction preference (chat_id == sender_id in a DM).
        dm_doc = await load_profile_doc(db, chat_id)
        memory_block, needs_compression = compile_memory_block(dm_doc)
        sender_reactions_enabled = dm_doc.get("reactions_enabled", True)
        # Temporal context: record the user's last-interaction time and compute a
        # concise "now + last talked" string in a single combined round-trip
        # (no extra LLM call, no upsert).
        prev = await models.touch_and_get_last_interaction(db, chat_id, now=now)
        time_context = build_time_context(now, prev)
        system_prompt = build_system_prompt(persona, memory_block, time_context=time_context)

    # 4. Single LLM call -> reply + optional reaction (+ optional affinity_delta for groups).
    if is_group:
        reply_text, reaction, affinity_delta = await llm_service.generate_reply_bundle(
            chat_id, system_prompt, active_history, with_affinity=True
        )
        # Affinity signals (no extra LLM call; all clamping lives in AffinityCache.bump).
        #
        # Note: the mention/reply-to-bot affinity-up signal and the engagement-after-chime
        # signal are routing-level and applied in task 3.2 (handlers/messages.py); here we
        # handle only the two signals that are naturally available within handle_message:
        # the reply bundle's ``affinity_delta`` fold and the negative-keyword down-bump.

        # (a) Fold the reply bundle's optional ``affinity_delta`` into the speaker's
        #     affinity (Requirement 4.6). Skip no-op deltas (None / 0).
        if affinity_delta is not None and affinity_delta != 0:
            new_affinity = await affinity_cache.bump(db, chat_id, sender_id, affinity_delta)
            logger.debug(
                f"affinity signal=reply_delta chat={chat_id} sender={sender_id} "
                f"delta={affinity_delta:+.3f} -> {new_affinity:.3f} (reason={reason})"
            )

        # (b) Negative "back off" keyword -> small affinity-down for the speaker
        #     (Requirement 4.5). This branch only runs when a reply is produced
        #     (addressed/ambient), which is acceptable for now per task 5.2.
        if scan_negative_signal(user_text):
            new_affinity = await affinity_cache.bump(
                db, chat_id, sender_id, _NEGATIVE_AFFINITY_STEP
            )
            logger.debug(
                f"affinity signal=negative_keyword chat={chat_id} sender={sender_id} "
                f"delta={_NEGATIVE_AFFINITY_STEP:+.3f} -> {new_affinity:.3f}"
            )
    else:
        reply_text, reaction = await llm_service.generate_reply_bundle(chat_id, system_prompt, active_history)

    # 5. Persist the assistant reply with sender attribution (DM: just adds two fields).
    await models.add_message_to_buffer(
        db, chat_id, "assistant", reply_text, sender_id=0, sender_name=config.bot_display_name
    )

    # 6. Memory over budget -> rate-limited background compression.
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_compressor(chat_id))

    # Per-user reaction opt-out (/reactions off): drop the reaction here so the sender
    # delivery path never needs a second profile read. The flag was folded into the
    # memory-block load above.
    if reaction and not sender_reactions_enabled:
        reaction = None

    return reply_text, reaction
