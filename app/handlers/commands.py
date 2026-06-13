"""Slash-command handlers: /start, /help, /profile, /reset."""
from aiogram import Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import models
from app.services.memory_loader import build_memory_block

router = Router(name="commands")


@router.message(Command("start"))
async def cmd_start(message: Message, db: AsyncIOMotorDatabase):
    user = message.from_user
    if not user:
        return
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
    await message.answer(
        f"Hi {html.bold(user.first_name or 'there')}! 👋\n\n"
        "I'm ThinkMate, an AI companion who remembers our past chats.\n"
        "Use /profile to see what I remember, or /help for everything I can do.",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        f"{html.bold('Here is what I can do:')}\n\n"
        "/start — say hi and set up your profile\n"
        "/profile — see what I remember about you\n"
        "/reset — make me forget everything (with confirmation)\n"
        "/help — show this message\n\n"
        "Mostly though, just talk to me. 🙂",
        parse_mode="HTML",
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    user_id = message.from_user.id

    doc = await db["user_profiles"].find_one({"_id": user_id})
    has_memories = bool(
        doc and (
            (doc.get("profile_summary") or "").strip()
            or doc.get("facts") or doc.get("beliefs") or doc.get("events")
        )
    )
    if not has_memories:
        await message.answer("I don't have any saved memories for you yet. Let's chat more first!")
        return

    profile_data, _ = await build_memory_block(db, user_id)
    await message.answer(
        f"📋 {html.bold('My Memories of You:')}\n\n{html.code(profile_data)}",
        parse_mode="HTML",
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    if (command.args or "").strip().lower() != "confirm":
        await message.answer(
            "⚠️ This will erase everything I remember about you and our chats.\n"
            "If you're sure, send: /reset confirm"
        )
        return
    await models.reset_user(db, message.from_user.id)
    await message.answer("Done — I've cleared everything. We're starting fresh. 🌱")
