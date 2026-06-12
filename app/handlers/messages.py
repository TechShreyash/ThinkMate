from aiogram import Router, F
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.services.user_task_manager import user_task_manager

router = Router(name="messages")

@router.message(F.text)
async def handle_user_message(message: Message, db: AsyncIOMotorDatabase):
    user_id = message.from_user.id
    user_text = message.text

    # --- INPUT LENGTH GUARD ---
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
        )
        return  # Ignore completely, do not save to buffer or process with LLM

    # Enqueue conversational message for batching/processing
    await user_task_manager.enqueue_message(message.bot, user_id, user_text, message)

