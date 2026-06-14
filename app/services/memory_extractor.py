"""Background memory extraction.

Reads the oldest buffer messages (everything except the latest ``CHAT_BUFFER_TRIM``),
asks the extraction model for structured updates, applies them to the user profile, and
atomically trims the processed messages from the buffer.

The extraction call is retried up to ``MAX_EXTRACTION_ATTEMPTS`` times. Each attempt
re-reads the buffer, so any messages that arrive *while* a slow call is in flight are
folded into the next attempt instead of being missed. If every attempt fails (e.g. an LLM
outage), the processed segment is trimmed anyway so the buffer can't grow without bound —
a deliberate trade of a bounded amount of un-extracted memory for a healthy buffer.
"""
from loguru import logger
from app.config import config
from app.database.connection import db_session
from app.database import models
from app.services.llm_service import llm_service
from app.services.memory_loader import build_memory_block
from app.services.metrics import metrics
from app.prompts.extraction_prompt import SYSTEM_EXTRACTION_PROMPT

# Maximum number of extraction LLM calls per run; each call re-snapshots the latest buffer.
MAX_EXTRACTION_ATTEMPTS = 3


def _format_segment(segment: list[dict]) -> str:
    """Render a buffer slice as a readable ``User:``/``Assistant:`` transcript."""
    return "".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}\n"
        for m in segment
    )


# --- Multi-party (group) extraction helpers ---------------------------------- #

# Name used for the bot's own (assistant) turns; never a memory participant.
_BOT_NAME = "ThinkMate"


def _normalize_name(name: str | None) -> str:
    """Casefold + whitespace-collapse a sender name for tolerant name->id matching."""
    return " ".join((name or "").split()).casefold()


async def _read_raw_buffer(db, chat_id: int) -> list[dict]:
    """Return the raw buffer messages (with ``sender_id``/``sender_name``) for ``chat_id``.

    ``models.get_chat_buffer`` intentionally projects to ``{role, content}`` for the LLM
    reply path, which strips the sender attribution the group extractor needs. We read the
    document directly here so the multi-party name->id map and the DM-vs-group dispatch can
    see who actually spoke. Returns ``[]`` when the buffer is absent.
    """
    doc = await db["chat_buffers"].find_one({"_id": chat_id})
    if doc and "messages" in doc:
        return doc["messages"]
    return []


def _is_group_buffer(messages: list[dict]) -> bool:
    """Heuristic dispatch: is this buffer a multi-party group or a single-party DM?

    We count the distinct *human* senders among ``role == "user"`` messages, excluding the
    bot's own turns (``sender_id == 0`` / name "ThinkMate"). A DM has exactly one human
    speaker (whose ``sender_id == chat_id``), so it yields a single distinct id; a group
    has two or more. More than one distinct human sender => group path. This lets the
    single ``extract_and_trim(chat_id)`` entry point route without any caller change.
    """
    senders: set[int] = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        sid = m.get("sender_id")
        if sid in (None, 0) or _normalize_name(m.get("sender_name")) == _BOT_NAME.casefold():
            continue
        senders.add(sid)
    return len(senders) > 1


def _format_group_segment(segment: list[dict]) -> str:
    """Render a multi-party slice as ``"SenderName: content"`` lines for the LLM.

    Assistant turns are attributed to "ThinkMate"; user turns use their ``sender_name``
    (falling back to "Unknown" when absent) so the model can tell speakers apart.
    """
    lines = []
    for m in segment:
        if m.get("role") == "assistant":
            name = m.get("sender_name") or _BOT_NAME
        else:
            name = m.get("sender_name") or "Unknown"
        lines.append(f"{name}: {m['content']}\n")
    return "".join(lines)


def _build_name_id_map(segment: list[dict]) -> dict[str, int]:
    """Build a normalized ``name -> sender_id`` map from a group segment's human messages.

    Skips the assistant (``sender_id == 0`` / "ThinkMate") and empty names. Names are
    normalized (casefold + whitespace-collapse) for tolerant resolution. When two distinct
    ids share the same name, the FIRST occurrence wins (documented tie-break) — we can't
    disambiguate identical display names, so we attribute deterministically to the earliest
    speaker rather than guess.
    """
    name_to_id: dict[str, int] = {}
    for m in segment:
        if m.get("role") != "user":
            continue
        sid = m.get("sender_id")
        key = _normalize_name(m.get("sender_name"))
        if sid in (None, 0) or not key or key == _BOT_NAME.casefold():
            continue
        if key not in name_to_id:  # prefer the first id seen for a given name
            name_to_id[key] = sid
    return name_to_id


# Extra instruction appended to the extraction prompt for the multi-party path so the
# model tags each update with the speaker name it sees in the rendered segment.
_GROUP_EXTRACTION_NOTE = (
    "=== GROUP CONVERSATION (MULTI-PARTY) ===\n"
    "This is a multi-party group conversation. Each line is prefixed with the speaker's "
    "name as 'Name: message'. Produce a list of per-participant updates: for each person "
    "you have something worth remembering about, output one entry tagging that participant "
    "by the EXACT name shown, paired with their own memory extraction. Attribute every "
    "fact, belief, and event to the correct speaker — never mix people, and ignore "
    "ThinkMate's own turns.\n"
    "Store each participant's memory in ENGLISH (translate non-English content to natural "
    "English, never transliterate), while keeping every person's name and other proper nouns "
    "in their original form.\n"
)


async def extract_and_trim(chat_id: int, *, is_group: bool | None = None):
    """Entry point for background memory extraction; dispatches DM vs group.

    The caller (``chat_manager`` via ``run_extractor``) now passes an explicit ``is_group``
    derived from the real Telegram ``chat_type``, which is authoritative: when given it
    overrides the heuristic entirely. ``is_group is True`` routes to
    :func:`extract_and_trim_group`; ``is_group is False`` routes to
    :func:`_extract_and_trim_single`.

    When no hint is provided (``is_group is None`` — e.g. existing test callers that invoke
    ``extract_and_trim(chat_id)``), we fall back to the CURRENT behavior: peek at the raw
    buffer once and decide via :func:`_is_group_buffer` (more than one distinct human
    sender => group). The sender-count heuristic is only this fallback; it can misclassify
    a group whose extractable segment happens to contain a single active speaker, which is
    exactly why an explicit hint is preferred when the caller knows the real chat type.
    """
    if is_group is True:
        await extract_and_trim_group(chat_id)
        return
    if is_group is False:
        await _extract_and_trim_single(chat_id)
        return

    try:
        async with db_session() as db:
            raw_messages = await _read_raw_buffer(db, chat_id)
    except Exception as e:  # noqa: BLE001 - never let dispatch peeking crash the task
        logger.error(f"Extraction dispatch failed to read buffer for chat {chat_id}: {e}")
        return

    if _is_group_buffer(raw_messages):
        await extract_and_trim_group(chat_id)
    else:
        await _extract_and_trim_single(chat_id)


async def _extract_and_trim_single(user_id: int):
    """Extract lasting memories from older buffer messages, then trim them.

    Retries the extraction call up to ``MAX_EXTRACTION_ATTEMPTS`` times, re-reading the
    buffer on each attempt so messages that arrived mid-call are included. On success the
    processed segment is saved and atomically trimmed. If all attempts fail, the oldest
    messages are trimmed anyway to keep the buffer bounded during an outage.

    This is the original single-party (DM) flow, unchanged; only the dispatching entry
    point (:func:`extract_and_trim`) was added around it.
    """
    logger.info(f"Memory extraction started for user {user_id}.")
    metrics.incr("extraction.runs")
    keep_count = config.CHAT_BUFFER_TRIM
    try:
        for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
            async with db_session() as db:
                buffer_messages = await models.get_chat_buffer(db, user_id)
                if len(buffer_messages) <= keep_count:
                    # Nothing left to process (e.g. a concurrent run already trimmed it).
                    return

                trim_size = len(buffer_messages) - keep_count
                segment = buffer_messages[:trim_size]

                # Give the model current memories so it can de-dupe / merge / correct.
                current_memory_text, _ = await build_memory_block(db, user_id)
                instruction_prompt = (
                    f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                    f"=== CURRENT MEMORIES ===\n{current_memory_text}\n"
                )

                extraction = await llm_service.extract_memory(
                    user_id=user_id,
                    system_prompt=instruction_prompt,
                    user_history_text=_format_segment(segment),
                )

                if extraction is not None:
                    await models.save_extracted_memories(db, user_id, extraction)
                    # Atomic trim of the processed segment (concurrent appends preserved):
                    # the segment we processed is the oldest `trim_size` messages, so a
                    # fresh-read oldest-N trim removes exactly those, keeping newer arrivals.
                    await models.delete_oldest_buffer_messages(db, user_id, trim_size)
                    logger.info(
                        f"Memory extraction done for user {user_id}; trimmed {trim_size} messages."
                    )
                    return

            logger.warning(
                f"Extraction attempt {attempt}/{MAX_EXTRACTION_ATTEMPTS} failed for user {user_id}."
            )

        # Every attempt failed. Trim the oldest messages anyway so a sustained outage can't
        # let the buffer grow unbounded. This drops un-extracted memory by design.
        async with db_session() as db:
            buffer_messages = await models.get_chat_buffer(db, user_id)
            if len(buffer_messages) > keep_count:
                trim_size = len(buffer_messages) - keep_count
                await models.delete_oldest_buffer_messages(db, user_id, trim_size)
                logger.error(
                    f"All {MAX_EXTRACTION_ATTEMPTS} extraction attempts failed for user {user_id}; "
                    f"trimmed {trim_size} un-extracted messages to keep the buffer bounded."
                )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Extraction pipeline failed for user {user_id}: {e}")


async def extract_and_trim_group(chat_id: int):
    """Multi-party group extraction: one LLM call, attribute updates per participant, trim.

    Mirrors the single-party :func:`_extract_and_trim_single` contract:
    - The segment to extract is everything except the most recent ``CHAT_BUFFER_TRIM``
      messages, re-read on each of ``MAX_EXTRACTION_ATTEMPTS`` attempts so messages that
      arrive mid-call are folded into the next attempt.
    - A single :meth:`llm_service.extract_group_memory` call processes the whole segment
      (Requirement 5.1), not one call per participant.
    - On success, each returned :class:`GroupMemoryUpdate` is mapped from its tagged
      participant name back to a ``sender_id`` via the segment's own name->id map
      (Requirements 5.2, 5.3). Updates whose name cannot be resolved are skipped rather
      than misattributed (Requirement 5.4).
    - The processed segment is trimmed with the existing atomic ``$pull``-on-cutoff trim
      (Requirement 5.6).
    - If every attempt fails, the oldest messages are trimmed anyway to keep the buffer
      bounded during an outage — the same all-fail-still-trim contract as the DM path.
    """
    logger.info(f"Group memory extraction started for chat {chat_id}.")
    metrics.incr("extraction.runs")
    keep_count = config.CHAT_BUFFER_TRIM
    try:
        for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
            async with db_session() as db:
                buffer_messages = await _read_raw_buffer(db, chat_id)
                if len(buffer_messages) <= keep_count:
                    # Nothing left to process (e.g. a concurrent run already trimmed it).
                    return

                trim_size = len(buffer_messages) - keep_count
                segment = buffer_messages[:trim_size]

                # Local name->id map built from the segment's own sender attribution; this
                # is the only source of truth for attribution (no cross-segment guessing).
                name_to_id = _build_name_id_map(segment)

                instruction_prompt = (
                    f"{SYSTEM_EXTRACTION_PROMPT}\n\n{_GROUP_EXTRACTION_NOTE}"
                )

                result = await llm_service.extract_group_memory(
                    chat_id,
                    instruction_prompt,
                    _format_group_segment(segment),
                )

                if result is not None:
                    saved = skipped = 0
                    for update in result.updates:
                        resolved_id = name_to_id.get(_normalize_name(update.participant))
                        if resolved_id is None:
                            skipped += 1
                            logger.warning(
                                f"Group extraction: participant {update.participant!r} could not be "
                                f"resolved to a sender in chat {chat_id}; skipping (no misattribution)."
                            )
                            continue
                        await models.save_extracted_memories(db, resolved_id, update.extraction)
                        saved += 1

                    # Atomic trim of the processed segment (concurrent appends preserved).
                    await models.delete_oldest_buffer_messages(db, chat_id, trim_size)
                    logger.info(
                        f"Group memory extraction done for chat {chat_id}; saved {saved} "
                        f"participant update(s), skipped {skipped}, trimmed {trim_size} messages."
                    )
                    return

            logger.warning(
                f"Group extraction attempt {attempt}/{MAX_EXTRACTION_ATTEMPTS} failed for chat {chat_id}."
            )

        # Every attempt failed. Trim the oldest messages anyway so a sustained outage can't
        # let the buffer grow unbounded. This drops un-extracted memory by design.
        async with db_session() as db:
            buffer_messages = await _read_raw_buffer(db, chat_id)
            if len(buffer_messages) > keep_count:
                trim_size = len(buffer_messages) - keep_count
                await models.delete_oldest_buffer_messages(db, chat_id, trim_size)
                logger.error(
                    f"All {MAX_EXTRACTION_ATTEMPTS} group extraction attempts failed for chat {chat_id}; "
                    f"trimmed {trim_size} un-extracted messages to keep the buffer bounded."
                )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Group extraction pipeline failed for chat {chat_id}: {e}")
