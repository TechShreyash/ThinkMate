"""Slash-command handlers: /start, /help, /profile, /reset."""
from aiogram import Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services.affinity import affinity_cache
from app.services.health import liveness, readiness
from app.services.memory_loader import build_memory_block
from app.services.metrics import metrics

router = Router(name="commands")


def _admin_allowed(message: Message) -> bool:
    """Authorization gate for the ops commands (Req 4.3, 4.4).

    When ``ADMIN_USER_IDS`` is non-empty, only those user ids are honored.
    When it is empty, fall back to the safe DM-only default so a status report
    is never broadcast into a group.
    """
    if not message.from_user:
        return False
    if config.ADMIN_USER_IDS:
        return message.from_user.id in config.ADMIN_USER_IDS
    return message.chat.type == "private"


def _render_health(live: dict, ready: dict) -> str:
    """Render a readable plain-text /health report (no parse_mode needed)."""
    summary = live.get("summary", {}) or {}
    if ready.get("ready"):
        mongo_line = f"mongo: ok ({ready.get('mongo', 'ok')})"
    else:
        mongo_line = f"mongo: degraded ({ready.get('reason', ready.get('mongo', 'error'))})"
    lines = [
        "🩺 ThinkMate health",
        f"status: {live.get('status', 'unknown')}",
        f"uptime_secs: {live.get('uptime_secs', 0)}",
        f"readiness: {mongo_line}",
        "",
        "metrics summary:",
        f"  llm_calls_total: {summary.get('llm_calls_total', 0)}",
        f"  reply_latency_avg: {summary.get('reply_latency_avg', 0)}",
        f"  reply_latency_max: {summary.get('reply_latency_max', 0)}",
        f"  throttle_drops: {summary.get('throttle_drops', 0)}",
        f"  queue_drops: {summary.get('queue_drops', 0)}",
        f"  conversations_active: {summary.get('conversations_active', 0)}",
        f"  extraction_runs: {summary.get('extraction_runs', 0)}",
        f"  compression_runs: {summary.get('compression_runs', 0)}",
    ]
    return "\n".join(lines)


def _render_metrics(snap: dict) -> str:
    """Render a compact plain-text dump of ``metrics.snapshot()``."""
    counters = snap.get("counters", {}) or {}
    gauges = snap.get("gauges", {}) or {}
    timers = snap.get("timers", {}) or {}
    lines = ["📊 ThinkMate metrics", "", "counters:"]
    if counters:
        lines += [f"  {name}: {value}" for name, value in sorted(counters.items())]
    else:
        lines.append("  (none)")
    lines.append("gauges:")
    if gauges:
        lines += [f"  {name}: {value}" for name, value in sorted(gauges.items())]
    else:
        lines.append("  (none)")
    lines.append("timers:")
    if timers:
        for name, agg in sorted(timers.items()):
            lines.append(
                f"  {name}: count={agg.get('count', 0)} "
                f"avg={agg.get('avg', 0)} max={agg.get('max', 0)}"
            )
    else:
        lines.append("  (none)")
    return "\n".join(lines)


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


@router.message(Command("quiet"))
async def cmd_quiet(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    if message.chat.type == "private":
        await message.answer(
            "/quiet and /chatty control how much I chime in within a group. "
            "In our DM I always reply to you, so there's nothing to quiet here. 🙂"
        )
        return
    await affinity_cache.set_mode(db, message.chat.id, message.from_user.id, "quiet")
    await message.answer("Okay, I'll stay quiet around you here. Mention me if you need me. 🤫")


@router.message(Command("chatty"))
async def cmd_chatty(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    if message.chat.type == "private":
        await message.answer(
            "/quiet and /chatty control how much I chime in within a group. "
            "In our DM I always reply to you, so there's nothing to boost here. 🙂"
        )
        return
    await affinity_cache.set_mode(db, message.chat.id, message.from_user.id, "chatty")
    await message.answer("You got it — I'll chime in more with you here! 😄")


@router.message(Command("health"))
async def cmd_health(message: Message, db: AsyncIOMotorDatabase):
    if not _admin_allowed(message):
        return  # fail closed; never leak a report (Req 4.3, 4.4)
    live = liveness()
    ready = await readiness(db)
    await message.answer(_render_health(live, ready))


@router.message(Command("metrics"))
async def cmd_metrics(message: Message, db: AsyncIOMotorDatabase):
    if not _admin_allowed(message):
        return  # same authorization rule as /health (Req 4.5)
    await message.answer(_render_metrics(metrics.snapshot()))
