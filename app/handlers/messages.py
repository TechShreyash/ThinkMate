"""Default text-message router: input guard, then enqueue for batched processing.

Typing indicators are driven by ``UserTaskManager`` (which spans the batching delay and
generation), so no aiogram typing middleware is involved here.
"""
from aiogram import Router, F
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.services.user_task_manager import user_task_manager

router = Router(name="messages")


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

    # Input length guard: ignore essays/code dumps entirely (not saved, not sent to LLM).
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
        )
        return

    await user_task_manager.enqueue_message(message.bot, message.from_user.id, user_text, message)
