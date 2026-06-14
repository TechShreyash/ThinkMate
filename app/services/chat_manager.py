"""Response-flow orchestrator.

For each user batch: append to the buffer, trigger background extraction on overflow,
assemble the system prompt (cached persona + compiled memory), generate the reply and an
optional reaction in a single LLM call, persist the reply, and trigger background
compression (rate-limited) when the memory profile outgrows its budget.
"""
import os
import asyncio
from datetime import datetime, timezone
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.affinity import affinity_cache
from app.services.group_gate import scan_negative_signal
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.prompts.system_prompt import build_system_prompt


def build_time_context(now: datetime, last_interaction_at) -> str:
    """A short time-context string for the system prompt: current UTC time, and a coarse
    'last talked' gap when a previous interaction exists (never raw seconds, never a gap on
    first contact)."""
    lines = [f"Current time (UTC): {now.strftime('%Y-%m-%d %H:%M')}"]
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


_DEFAULT_PERSONA = "You are ThinkMate, a warm, witty AI companion."
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
    # rendered multi-party so the model can distinguish speakers.
    if is_group:
        active_history = []
        for m in messages:
            if m["role"] == "user":
                name = m.get("sender_name") or ""
                content = f"{name}: {m['content']}" if name else m["content"]
                active_history.append({"role": "user", "content": content})
            else:
                active_history.append({"role": m["role"], "content": m["content"]})
    else:
        active_history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # 2. Buffer overflow -> non-blocking background extraction. Keyed by chat_id; the
    #    known group flag is passed through to the extractor so extraction dispatch is
    #    authoritative rather than relying on a sender-count heuristic.
    if buffer_chars >= config.CHAT_BUFFER_MAX_CHARS:
        from app.services.user_task_manager import user_task_manager
        logger.info(f"Buffer overflow for chat {chat_id} ({buffer_chars} chars); launching extraction.")
        asyncio.create_task(user_task_manager.run_extractor(chat_id, is_group=is_group))

    # 3. Assemble system prompt (cached persona + compiled memory).
    persona = _load_persona()
    memory_block, needs_compression = await build_memory_block(db, chat_id)

    # Temporal context: DM path only. Record the user's last-interaction time and compute a
    # concise "now + last talked" string in a single combined round-trip (no extra LLM call,
    # no upsert). Groups keep an empty time_context so multi-party behavior is unchanged.
    if not is_group:
        now = datetime.now(timezone.utc)
        prev = await models.touch_and_get_last_interaction(db, chat_id, now=now)
        time_context = build_time_context(now, prev)
    else:
        time_context = ""
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
        db, chat_id, "assistant", reply_text, sender_id=0, sender_name="ThinkMate"
    )

    # 6. Memory over budget -> rate-limited background compression.
    if needs_compression:
        from app.services.user_task_manager import user_task_manager
        asyncio.create_task(user_task_manager.run_compressor(chat_id))

    return reply_text, reaction
