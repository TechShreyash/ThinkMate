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
from aiogram import Router, F
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from loguru import logger
from app.config import config
from app.database import models
from app.services.user_task_manager import user_task_manager
from app.services.group_gate import is_addressed
from app.services.affinity import affinity_cache

router = Router(name="messages")

# Telegram chat.type values that route to the multi-party group path.
_GROUP_CHAT_TYPES = ("group", "supergroup")

# Cached identity from a single ``bot.get_me()`` call. Resolving the bot's username and
# name is needed for addressed-detection on every group message, but ``get_me`` is a
# network round-trip — so we cache the result process-wide after the first successful
# call to avoid an API hit per message. Shape: {"id": int, "username": str, "name": str}.
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
            "name": me.first_name or "",
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


async def _maybe_ambient_chime(
    message: Message,
    db: AsyncIOMotorDatabase,
    user_text: str,
    sender_name: str,
) -> None:
    """Hand-off point for non-addressed group messages → the ambient gate.

    The message has already been recorded to the buffer by the caller. The ambient-gate
    wiring (cooldown → cheap trigger scan → affinity-weighted dice roll → at most one
    chime-in enqueue) is completed in task 4.2; for now this is a thin stub that returns
    without any LLM call so non-addressed messages are simply observed.
    """
    # ambient gate wiring completed in task 4.2
    return


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
    to the buffer (Requirement 2.1), but we must avoid writing the *user* message twice.
    ADDRESSED messages are enqueued and the normal ``enqueue_message`` → ``handle_message``
    path appends the user message itself — exactly like DMs — so we do NOT write here for
    them (a single write). NON-ADDRESSED messages are never enqueued and never reach
    ``handle_message``, so this handler is the only place that can record them; we write
    them here before handing off to the ambient gate. Net result: each group message is
    buffered exactly once, addressed by the enqueue path and non-addressed by this handler.
    """
    # Length guard in groups: still don't process essays, but stay silent (no DM-style
    # deflection) to avoid group spam — just drop the over-long message quietly.
    if len(user_text) > config.MAX_INPUT_CHARS:
        return

    sender_name = _display_name(message)

    # Resolve (cached) bot identity for addressed-detection.
    identity = await _get_bot_identity(message)

    # reply_to_bot: the message replies to one of the bot's own messages.
    reply_to_bot = False
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to is not None and getattr(reply_to, "from_user", None) is not None:
        reply_to_bot = (reply_to.from_user.id == identity["id"]) and identity["id"] != 0

    addressed = is_addressed(
        text=user_text,
        entities=message.entities,
        reply_to_bot=reply_to_bot,
        bot_username=identity["username"],
        bot_name=identity["name"],
    )

    if addressed:
        # Affinity-up: a mention / reply-to-bot is a routing-level engagement signal
        # (Requirement 4.4). The bump is small and clamped to [0, 1] by the cache.
        try:
            await affinity_cache.bump(db, message.chat.id, message.from_user.id, 0.05)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"affinity bump on addressed message failed: {e}")

        # Single-write invariant: do NOT buffer here — the enqueue → handle_message path
        # appends the user message, mirroring DM behavior.
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

    # Not addressed: this handler is the only writer for non-addressed messages, so record
    # the message to the chat buffer with sender attribution before handing off to the gate.
    await models.add_message_to_buffer(
        db,
        message.chat.id,
        "user",
        user_text,
        sender_id=message.from_user.id,
        sender_name=sender_name,
    )
    await _maybe_ambient_chime(message, db, user_text, sender_name)
