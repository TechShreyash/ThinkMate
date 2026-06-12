from aiogram import Router, html
from aiogram.filters import Command
from aiogram.types import Message
from aiosqlite import Connection
from app.database import models
from app.services.memory_loader import build_memory_block

router = Router(name="commands")

@router.message(Command("start"))
async def cmd_start(message: Message, db: Connection):
    user_id = message.from_user.id
    username = message.from_user.username
    display_name = message.from_user.first_name
    
    # ensure_user uses the injected database connection
    await models.ensure_user(db, user_id, username, display_name)
    
    welcome_text = (
        f"Hi {html.bold(display_name)}! 👋\n\n"
        f"I am ThinkMate, an AI companion who remembers our past chats.\n"
        f"Use /profile to view what I remember, or /help to see all commands."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@router.message(Command("profile"))
async def cmd_profile(message: Message, db: Connection):
    user_id = message.from_user.id
    
    # Generate memory card using memory loader
    profile_data, _ = await build_memory_block(db, user_id)
    
    if not profile_data.strip():
        await message.answer("I don't have any saved memories for you yet. Let's chat more first!")
        return
        
    await message.answer(f"📋 {html.bold('My Memories of You:')}\n\n{html.code(profile_data)}", parse_mode="HTML")
