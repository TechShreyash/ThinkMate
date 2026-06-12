from aiogram import Router, F
from aiogram.types import Message
from aiosqlite import Connection
from app.config import config
from app.services.chat_manager import handle_message

router = Router(name="messages")

@router.message(F.text, flags={"long_operation": True})
async def handle_user_message(message: Message, db: Connection):
    user_id = message.from_user.id
    user_text = message.text

    # --- INPUT LENGTH GUARD ---
    if len(user_text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "that's a lot of text 😅 keep it short — i'm better at conversations than essays"
        )
        return  # Ignore completely, do not save to buffer or process with LLM

    # Process conversational message
    reply_text = await handle_message(db, user_id, user_text)
    await message.answer(reply_text)

