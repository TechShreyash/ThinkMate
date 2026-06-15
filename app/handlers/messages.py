"""Default text-message router: input guard, chat-type routing, then enqueue.

Routing overview (Phase 9, group chat):

- ``private``  → the exact DM path that existed before group support: length-guard,
  then a positional ``enqueue_message`` so DM behavior and DM tests are byte-for-byte
  unchanged.
- ``channel``  → ignored entirely (no buffer write, no reply). Channel posts usually
  carry no ``from_user`` anyway, but we branch explicitly.
- ``group`` / ``supergroup`` → the group path: record multi-party context, decide
  whether the message *addresses* the bot, and either enqueue a reply or hand the
  message to the ambient gate.

Typing indicators are driven by ``UserTaskManager`` (which spans the batching delay and
generation), so no aiogram typing middleware is involved here.
"""
import time

from aiogram import Router, F
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from loguru import logger
from app.config import config
from app.database import models
from app.services.user_task_manager import user_task_manager
from app.services.group_gate import (
    is_addressed,
    is_mass_tag_spam,
    is_directed_at_other,
    scan_cheap_triggers,
    ambient_gate,
    implicit_gate,
    spam_burst_detector,
)
from app.services.affinity import affinity_cache
from app.services import log_forwarder

router = Router(name="messages")

# Telegram chat.type values that route to the multi-party group path.
_GROUP_CHAT_TYPES = ("group", "supergroup")

# Cached identity from a single ``bot.get_me()`` call. Resolving the bot's username and
# name is needed for addressed-detection on every group message, but ``get_me`` is a
# network round-trip — so we cache the result process-wide after the first successful
# call to avoid an API hit per message. The ``name`` honors ``config.BOT_NAME`` when set,
# otherwise falls back to the Telegram first name. Shape: {"id": int, "username": str, "name": str}.
_bot_identity: dict | None = None


async def _get_bot_identity(message: Message) -> dict:
    """Return the cached bot identity, resolving it once via ``bot.get_me()``.

    The result is cached at module level after the first successful lookup. Any failure
    degrades to an empty identity (``id=0``, empty username/name) so addressed-detection
    simply treats the message as "not addressed" rather than raising on the hot path.
    """
    global _bot_identity
    if _bot_identity is not None:
        return _bot_identity
    try:
        me = await message.bot.get_me()
        _bot_identity = {
            "id": me.id,
            "username": me.username or "",
            # Prefer the configured BOT_NAME; fall back to the Telegram first name.
            "name": config.BOT_NAME.strip() or (me.first_name or ""),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"get_me() failed; treating messages as not-addressed: {e}")
        # Do NOT cache the failure permanently — return a transient empty identity so a
        # later message can retry the lookup.
        return {"id": 0, "username": "", "name": ""}
    return _bot_identity


def _display_name(message: Message) -> str:
    """Best-effort human-readable sender name for buffer attribution."""
    user = message.from_user
    return (
        getattr(user, "full_name", None)
        or getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or ""
    )


def _truncate(text: str, limit: int = 80) -> str:
    """Single-line, length-bounded preview of a message for trace logs."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def _trace_routing(message: Message, decision: str, detail: str = "") -> None:
    """Emit a group routing-decision trace to the console (DEBUG) and, when
    ``FORWARD_DIAGNOSTICS`` is on, to the Logs_Channel.

    Gives live visibility into WHY the bot did or did not respond to each group
    message — the core early-phase debugging signal. Best-effort: never raises.
    """
    sender = _display_name(message) or "?"
    preview = _truncate(getattr(message, "text", "") or "")
    line = (
        f"🧭 route={decision} chat={getattr(getattr(message, 'chat', None), 'id', '?')} "
        f"sender={getattr(message.from_user, 'id', '?')}({sender})"
        f"{(' ' + detail) if detail else ''} | “{preview}”"
    )
    logger.debug(line)
    try:
        await log_forwarder.diagnostic(message.bot, getattr(getattr(message, "chat", None), "id", None), line)
    except Exception:  # noqa: BLE001 - tracing must never affect routing
        pass


async def _maybe_ambient_chime(
    message: Message,
    db: AsyncIOMotorDatabase,
    user_text: str,
    sender_name: str,
    *,
    is_spam: bool = False,
) -> None:
    """Run the ambient gate on a non-addressed group message → maybe chime in.

    This function owns the buffer write for the *drop* path (single-write invariant). The
    funnel is a no-LLM sequence — fetch the speaker's affinity/mode, run the cheap trigger
    scan, then let :class:`AmbientGate` apply the cooldown → trigger/scan-tick →
    affinity-weighted dice roll. Only a candidate that survives all three steps reaches the
    LLM via an ``enqueue_message(reason="ambient")`` chime-in.

    Buffer-write strategy: when the gate does NOT pass (any drop stage, or a defensive
    gate-evaluation failure), this is the ONLY place the non-addressed message can be
    recorded, so we write it to the buffer here. When the gate DOES pass, the message is
    enqueued and the ``enqueue_message`` → ``handle_message`` path appends it instead, so
    we deliberately do NOT write it here. Net result: exactly one write per message.

    Defensiveness: the affinity read and the gate decision are wrapped so any failure on
    this hot path degrades to "no chime" (Requirement 7.4 / error-handling contract)
    rather than raising — and that failure is treated as a drop, so the message is still
    recorded once. When we do decide to chime in, ``mark_chimed`` is called *before*
    enqueueing so the per-chat cooldown holds for the full window even if the eventual
    model reply is empty or fails (Requirement 3.7).
    """
    try:
        # Speaker affinity/mode (group, so consulting chat_members is correct — Req 4.8
        # only forbids it in DMs). The cache serves warm members from memory.
        member = await affinity_cache.get(db, message.chat.id, message.from_user.id)

        # Group-wide admin override takes PRIORITY over the member's personal mode: when an
        # admin has set the group to "quiet" or "chatty" (/groupquiet|/groupchatty), that
        # wins for everyone here; "auto" (the default, /groupnormal) defers to each user's
        # own /quiet|/chatty. Read defensively so a DB hiccup degrades to "auto" (no
        # override) rather than dropping the message off the ambient path.
        try:
            group_mode = await models.get_group_mode(db, message.chat.id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"group-mode read failed; treating as auto: {e}")
            group_mode = "auto"
        effective_mode = group_mode if group_mode in ("quiet", "chatty") else member["mode"]

        # Cheap, no-LLM trigger scan (birthdays, congrats, questions, greetings, ...).
        # Spam-aware: when the message is Mass_Tag_Spam or Greeting_Burst_Spam, force the
        # trigger off so greeting/laughter/etc. keywords can never fire the ambient gate
        # for a spam message (Req 9.3, 10.5). The message still flows through the gate so
        # the single-write invariant holds — it simply drops at the "no_trigger" stage.
        triggered = scan_cheap_triggers(user_text) and not is_spam

        now = time.time()
        should, stage = ambient_gate.decide(
            message.chat.id,
            affinity=member["affinity"],
            mode=effective_mode,
            triggered=triggered,
            now=now,
        )
    except Exception as e:  # noqa: BLE001
        # Any failure in the affinity read or gate decision degrades to "no chime".
        # Treat it as a drop: record the message once (the gate is the sole writer on the
        # non-chime path) so a gate failure still preserves context and never chimes.
        logger.debug(f"ambient gate evaluation failed; dropping (no chime): {e}")
        await models.add_message_to_buffer(
            db,
            message.chat.id,
            "user",
            user_text,
            sender_id=message.from_user.id,
            sender_name=sender_name,
        )
        return

    if not should:
        # Per-stage drop logging (Req 7.2): surface WHICH funnel stage dropped the
        # candidate (cooldown / no_trigger / dice) so the funnel is observable.
        # Drop path: this is the only writer for a non-chiming message, so record it once.
        await models.add_message_to_buffer(
            db,
            message.chat.id,
            "user",
            user_text,
            sender_id=message.from_user.id,
            sender_name=sender_name,
        )
        logger.debug(f"ambient drop stage={stage} chat={message.chat.id}")
        await _trace_routing(message, "ambient→drop", f"stage={stage}")
        return

    # Passed the funnel — log the chime decision before dispatching (Req 7.2).
    logger.debug(f"ambient chime stage={stage} chat={message.chat.id}")
    await _trace_routing(message, "ambient→chime", f"stage={stage} affinity={member['affinity']:.2f}")

    # Reset the cooldown NOW so a failed/empty reply still holds the window (Req 3.7).
    # Do NOT write the buffer here: this message will be enqueued and the
    # enqueue_message → handle_message path appends it (single-write invariant).
    ambient_gate.mark_chimed(message.chat.id, now)
    await user_task_manager.enqueue_message(
        message.bot,
        message.chat.id,
        user_text,
        message,
        user_id=message.from_user.id,
        chat_type=message.chat.type,
        sender_name=sender_name,
        reason="ambient",
    )


@router.message(F.text)
async def handle_user_message(message: Message, db: AsyncIOMotorDatabase):
    # Ignore service/channel posts with no real sender.
    if not message.from_user:
        return

    user_text = message.text or ""

    # Skip bot commands: registered commands are handled by commands.py, and any
    # unregistered slash command falls through to this catch-all. Commands are not
    # conversation, so ignore them entirely (no reply, no enqueue). Treat the message
    # as a command when EITHER its text starts with "/" OR Telegram reports a
    # bot_command entity at offset 0 (covering "/foo" and "/foo@BotName"). The leading
    # "/" check alone is sufficient and reliable; the entity check is an extra safety
    # net. This does not misclassify text like "2/3" since it does not start with "/".
    # Use getattr-safe access so a missing/non-iterable entities value cannot raise.
    entities = message.entities or []
    is_command = user_text.startswith("/") or any(
        getattr(e, "type", None) == "bot_command" and getattr(e, "offset", None) == 0
        for e in entities
    )
    if is_command:
        return

    chat_type = getattr(getattr(message, "chat", None), "type", None)

    # --- Channel: ignore entirely (no buffer write, no reply). ---
    if chat_type == "channel":
        return

    # --- Group / supergroup: the multi-party group path. ---
    if chat_type in _GROUP_CHAT_TYPES:
        await _handle_group_message(message, db, user_text)
        return

    # --- Private (default): EXACT current DM behavior. ---
    # Input length guard: ignore essays/code dumps entirely (not saved, not sent to LLM).
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
        )
        return

    await user_task_manager.enqueue_message(message.bot, message.from_user.id, user_text, message)


async def _handle_group_message(
    message: Message,
    db: AsyncIOMotorDatabase,
    user_text: str,
) -> None:
    """Group/supergroup routing: addressed → reply; otherwise → ambient gate.

    Buffer-write strategy (single-write invariant): every group message must be recorded
    to the buffer (Requirement 2.1), but the *user* message must never be written twice.
    The writer depends on the path:

    - ADDRESSED → the message is enqueued and the normal ``enqueue_message`` →
      ``handle_message`` path appends the user message itself (exactly like DMs), so we do
      NOT write here.
    - NON-ADDRESSED + gate DROPS → the message is never enqueued, so ``_maybe_ambient_chime``
      records it on the drop path (the gate is the sole writer there).
    - NON-ADDRESSED + gate PASSES → the message IS enqueued, so the ``enqueue_message`` →
      ``handle_message`` path appends it; the gate deliberately does not write.

    Net result: each group message is buffered exactly once.
    """
    # Length guard in groups: still don't process essays, but stay silent (no DM-style
    # deflection) to avoid group spam — just drop the over-long message quietly.
    if len(user_text) > config.MAX_INPUT_CHARS:
        return

    # Group kill switch: when an admin has turned the bot off in this chat (/groupoff),
    # ignore the message COMPLETELY — no reply, no ambient chime, no memory, no buffer
    # write. Slash commands never reach here (they're handled by commands.py), so
    # /groupon can always re-enable the bot. Read defensively: any failure degrades to
    # "enabled" so a transient DB hiccup can never silently mute the bot.
    try:
        if not await models.is_group_enabled(db, message.chat.id):
            await _trace_routing(message, "group-disabled", "kill switch on (/groupoff)")
            return
    except Exception as e:  # noqa: BLE001
        logger.debug(f"group-enabled check failed; treating as enabled: {e}")

    sender_name = _display_name(message)

    # Best-effort identity capture/refresh for EVERY group sender (Req 1.*). This runs only
    # on the group path (the DM branch never reaches here — Req 5.3) and never writes the
    # chat buffer, so the Single_Write_Invariant is unaffected (Req 5.1). Any failure is
    # logged at debug and swallowed so it can never raise on the hot path (Req 1.7, 5.4).
    try:
        change = await models.refresh_identity_if_changed(
            db,
            message.from_user.id,
            message.from_user.username or "",
            sender_name,
        )
        if change is not None:
            await log_forwarder.send(
                message.bot,
                message.chat.id,
                f"👤 identity {'created' if change['created'] else 'refreshed'} "
                f"for {message.from_user.id} in chat {message.chat.id}",
            )
    except Exception as e:  # noqa: BLE001 - degrade, never raise on the hot path (Req 1.7)
        logger.debug(f"identity refresh failed for {message.from_user.id}: {e}")

    # Resolve (cached) bot identity for addressed-detection.
    identity = await _get_bot_identity(message)

    # Single clock read shared by every spam/recency decision below.
    now = time.time()

    # Classify both spam shapes up front, each defended independently so a classification
    # bug can never suppress a legitimate reply.
    #
    # Greeting_Burst_Spam is stateful: ``observe`` must run on EVERY group message so the
    # time-windowed history is complete regardless of which path the message takes
    # (Req 10.14 — error degrades to "not burst").
    try:
        is_burst = spam_burst_detector.observe(
            message.chat.id, user_text, message.entities, now, user_id=message.from_user.id
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"spam burst detector failed; treating as not-burst: {e}")
        is_burst = False

    # Mass_Tag_Spam is a pure per-message scan (Req 9.6 — error degrades to "not spam").
    try:
        is_mass = is_mass_tag_spam(
            user_text,
            message.entities,
            threshold=config.GROUP_MASS_TAG_SPAM_THRESHOLD,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mass-tag spam scan failed; treating as not-spam: {e}")
        is_mass = False

    # Single combined flag used by every downstream decision.
    spam = is_mass or is_burst

    # reply_to_bot: the message replies to one of the bot's own messages.
    reply_to_bot = False
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to is not None and getattr(reply_to, "from_user", None) is not None:
        reply_to_bot = (reply_to.from_user.id == identity["id"]) and identity["id"] != 0

    # Spam-aware explicit-address decision (replaces the bare is_addressed call):
    #   - reply_to_bot → explicit: a deliberate reply-to-bot survives spam (Req 9.5, 10.7).
    #   - elif spam → NOT explicit: a bot @mention buried in a bulk/burst message does not
    #     count as an explicit address (Req 9.4, 10.6).
    #   - else → existing is_addressed scan (Req 10.9).
    if reply_to_bot:
        addressed = True
    elif spam:
        addressed = False
        await _trace_routing(
            message,
            "spam-detected",
            f"mass_tag={is_mass} burst={is_burst} (mention ignored)",
        )
    else:
        addressed = is_addressed(
            text=user_text,
            entities=message.entities,
            reply_to_bot=False,
            bot_username=identity["username"],
            bot_name=identity["name"],
        )

    if addressed:
        # Affinity-up: a mention / reply-to-bot is a routing-level engagement signal
        # (Requirement 4.4). The bump is small and clamped to [0, 1] by the cache.
        try:
            new_affinity = await affinity_cache.bump(
                db, message.chat.id, message.from_user.id, 0.05
            )
            # Signal-type-tagged debug log for traceability (Requirement 7.5).
            logger.debug(
                f"affinity signal=mention_up chat={message.chat.id} "
                f"sender={message.from_user.id} delta=+0.050 -> {new_affinity:.3f}"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"affinity bump on addressed message failed: {e}")

        # Single-write invariant: do NOT buffer here — the enqueue → handle_message path
        # appends the user message, mirroring DM behavior.
        await _trace_routing(
            message,
            "addressed→reply",
            f"via={'reply_to_bot' if reply_to_bot else 'mention/name'}",
        )
        await user_task_manager.enqueue_message(
            message.bot,
            message.chat.id,
            user_text,
            message,
            user_id=message.from_user.id,
            chat_type=message.chat.type,
            sender_name=sender_name,
            reason="reply",
        )
        # Count this human message AFTER the decision so it is never treated as its own
        # intervening predecessor.
        implicit_gate.note_human_message(message.chat.id, now)
        return

    # --- Not explicitly addressed: implicit-address detection, then ambient gate. ---

    # Is the message a reply to a *non-bot* participant? Used by is_directed_at_other.
    reply_to_other = False
    if reply_to is not None and getattr(reply_to, "from_user", None) is not None:
        other_id = reply_to.from_user.id
        reply_to_other = other_id is not None and not (
            other_id == identity["id"] and identity["id"] != 0
        )

    directed_at_other = is_directed_at_other(
        entities=message.entities,
        reply_to_other=reply_to_other,
    )

    # Implicit-address decision (no-LLM). Defensive: any failure degrades to "not
    # implicit" so the message simply falls through to the ambient gate (Req 1.6).
    try:
        is_implicit, reason = implicit_gate.decide(
            message.chat.id,
            directed_at_other=directed_at_other,
            is_spam=spam,
            now=now,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"implicit gate decision failed; degrading to ambient: {e}")
        is_implicit = False
        reason = "error"

    if is_implicit and implicit_gate.cooldown_elapsed(message.chat.id, now):
        # Commit the cooldown BEFORE enqueueing so it holds even if the eventual reply is
        # empty or fails (Req 3.3), mirroring the ambient mark_chimed contract.
        implicit_gate.mark_implicit_reply(message.chat.id, now)
        logger.debug(f"implicit reply decision chat={message.chat.id}")
        await _trace_routing(message, "implicit→reply", "within recency window")
        # Count this human message AFTER the decision (see counter-ordering note below).
        implicit_gate.note_human_message(message.chat.id, now)
        # Single-write invariant: the implicit-reply path is identical to the addressed
        # path for buffer purposes — enqueue with reason="reply" and do NOT write here;
        # the enqueue → handle_message path appends the user message (Req 3.4).
        await user_task_manager.enqueue_message(
            message.bot,
            message.chat.id,
            user_text,
            message,
            user_id=message.from_user.id,
            chat_type=message.chat.type,
            sender_name=sender_name,
            reason="reply",
        )
        return

    # Diagnostic: surface WHY no direct (implicit) reply happened, so silent-bot
    # reports are debuggable. "out_of_window" = recency window closed (too old, or
    # more than GROUP_IMPLICIT_RECENCY_MAX_MSGS human messages since the bot last
    # spoke); "no_bot_activity" = the bot has not spoken in this chat yet;
    # "cooldown" here means implicit fired but GROUP_IMPLICIT_COOLDOWN_SECS holds.
    if is_implicit:
        logger.debug(f"implicit reply suppressed by cooldown chat={message.chat.id}")
        await _trace_routing(message, "implicit→cooldown", "→ ambient gate")
    else:
        logger.debug(f"implicit drop reason={reason} chat={message.chat.id}")
        await _trace_routing(message, "not-implicit", f"reason={reason} → ambient gate")

    # Neither explicit nor implicit (or the implicit cooldown has not elapsed — Req 4.1):
    # hand off to the ambient gate, which both decides whether to chime in and (on the
    # drop path only) records the message to the buffer — preserving the single-write
    # invariant. Counter ordering: note_human_message runs AFTER decide on every path so
    # the current message is never counted as one of its own intervening predecessors.
    implicit_gate.note_human_message(message.chat.id, now)
    await _maybe_ambient_chime(message, db, user_text, sender_name, is_spam=spam)
