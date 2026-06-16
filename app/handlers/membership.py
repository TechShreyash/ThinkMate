"""Membership handler: greet a group with a self-introduction when the bot joins.

Listens for ``my_chat_member`` updates (which always concern the bot itself and are
delivered regardless of group privacy mode) and, on a join transition into a group or
supergroup, posts a short introduction explaining who the bot is and how to talk to it.

Why ``my_chat_member`` rather than the ``new_chat_members`` service message: the
``my_chat_member`` update fires reliably the moment the bot's membership changes — even
when Telegram group privacy is ON (which suppresses the service message from reaching the
bot) — so the intro is sent every time the bot is actually added.
"""
from aiogram import Router, html
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import ChatMemberUpdated
from loguru import logger

from app.config import config
from app.services import log_forwarder

router = Router(name="membership")

# Chat types that should receive the group introduction (DMs/channels are excluded).
_GROUP_CHAT_TYPES = ("group", "supergroup")


async def _intro_text(bot) -> str:
    """Build the group self-introduction, resolving the bot's name/@username live.

    Prefers the configured ``BOT_NAME``; falls back to the Telegram first name. The
    ``@username`` is included as the explicit way to address the bot. Any lookup failure
    degrades gracefully to a generic name so the intro can always be sent.
    """
    name = config.BOT_NAME.strip()
    username = ""
    try:
        me = await bot.get_me()
        username = me.username or ""
        if not name:
            name = me.first_name or "ThinkMate"
    except Exception as e:  # noqa: BLE001 - identity lookup is cosmetic
        logger.debug(f"get_me() failed while building group intro: {e}")
        if not name:
            name = "ThinkMate"

    handle = f"@{username}" if username else name
    # Resolved start-command trigger (e.g. "start" or "chatbot") for the DM guide pointer.
    start_trigger = config.COMMANDS.get("start", ("start", True))[0]

    return (
        f"👋 Hey everyone, I'm {html.bold(name)} — your group's AI companion.\n\n"
        "I chat naturally, pick up on context, and remember what matters so "
        "conversations actually go somewhere.\n\n"
        f"💬 {html.bold('To talk to me directly')}, mention me ({html.quote(handle)}) "
        "or reply to one of my messages — I'll always answer. Otherwise I mostly stay "
        "out of the way and only chime in now and then.\n\n"
        f"📖 DM me /{html.quote(start_trigger)} for the full rundown of what I can do "
        "and how your data is handled."
    )


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_added_to_group(event: ChatMemberUpdated) -> None:
    """Send a one-time self-introduction when the bot is added to a group."""
    if event.chat.type not in _GROUP_CHAT_TYPES:
        return

    # Opt-out switch: when GROUP_INTRO_ON_JOIN is False, skip the intro entirely (no
    # message, no diagnostic). Useful when another bot sharing this account already posts
    # a join message, so the group only sees one.
    if not config.GROUP_INTRO_ON_JOIN:
        logger.info(
            f"Group intro on join disabled by config; skipping for chat {event.chat.id}"
        )
        return

    try:
        text = await _intro_text(event.bot)
        await event.bot.send_message(event.chat.id, text, parse_mode="HTML")
        logger.info(f"Sent group intro on join to chat {event.chat.id}")
    except Exception as e:  # noqa: BLE001 - a failed intro must never crash the update
        is_perm = any(
            p in str(e).lower()
            for p in ["forbidden", "permission", "write access", "not enough rights", "restricted", "kicked", "blocked"]
        )
        if is_perm:
            logger.info(f"Could not send group intro for chat {event.chat.id} (restricted/forbidden): {e}")
        else:
            logger.warning(f"Failed to send group intro for chat {event.chat.id}: {e}")

    try:
        title = getattr(event.chat, "title", None) or ""
        await log_forwarder.diagnostic(
            event.bot,
            event.chat.id,
            f"➕ added to group chat={event.chat.id} ({title!r}) — intro sent",
        )
    except Exception:  # noqa: BLE001
        pass
