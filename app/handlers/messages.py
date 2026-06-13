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

    # Input length guard: ignore essays/code dumps entirely (not saved, not sent to LLM).
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
        )
        return

    await user_task_manager.enqueue_message(message.bot, message.from_user.id, user_text, message)
