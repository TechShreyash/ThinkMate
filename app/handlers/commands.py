"""Slash-command handlers: /start, /help, /onboard, /profile, /reset, etc.

Beyond the plain slash commands, this module also powers an interactive, button-driven
**guide**. The guide is a small set of screens — memory, privacy, groups, check-ins, and
the full command list — that users page through with Telegram inline buttons
(``InlineKeyboardMarkup``). ``/start`` is the warm welcome and guide entry point, while
``/help`` jumps straight to the command list for users who expect a familiar help command.
A single ``callback_query`` handler
(:func:`on_guide_nav`) edits the message in place as the user taps between screens, so
newcomers can learn what the bot does without leaving the chat. See
``docs/development/telegram_bot.md`` for the design overview.
"""
import json
from datetime import datetime, timezone

from aiogram import F, Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import config
from app.database import models
from app.services import log_forwarder
from app.services.affinity import affinity_cache
from app.services.health import liveness, readiness
from app.services.memory_loader import build_memory_block
from app.services.metrics import metrics, LLM_TASK_TYPES
from app.services.telegram_text import send_message_text

router = Router(name="commands")


def _trigger(key: str) -> str:
    """Return the configured trigger name for a built-in command key.

    Commands can be renamed via ``CMD_<KEY>_NAME`` (see ``config.resolve_command_config``),
    so user-facing copy must reference the *resolved* trigger rather than hard-coding the
    default. Falls back to the key itself when unknown.
    """
    name, _enabled = config.COMMANDS.get(key, (key, True))
    return name


async def _reply(message: Message, text: str, **kwargs):
    """Send a command response as a Telegram *reply* in groups, plain answer in DMs.

    Mirrors the conversational reply behavior: in a group/supergroup the response threads
    under the user's command so it's clear who it's for; in a DM a plain answer is used
    (threading is needless there). Falls back to a plain answer if the original message
    can no longer be replied to (e.g. it was deleted).
    """
    return await send_message_text(
        message,
        text,
        prefer_reply=getattr(getattr(message, "chat", None), "type", None)
        in ("group", "supergroup"),
        **kwargs,
    )


def _parse_toggle(arg: str | None) -> bool | None:
    """Map an ``on``/``off``-style argument to a bool; ``None`` if absent/unrecognized.

    Shared by the toggle commands (``/reactions``, ``/checkins``, ``/groupbot``) so that a
    bare command (no/unknown arg) reports the current state while ``on``/``off`` set it.
    """
    a = (arg or "").strip().lower()
    if a in ("on", "enable", "enabled", "yes"):
        return True
    if a in ("off", "disable", "disabled", "no"):
        return False
    return None


def _is_admin(user_id: int | None, chat_type: str | None) -> bool:
    """Core authorization rule shared by messages and callback queries.

    When ``ADMIN_USER_IDS`` is non-empty, only those user ids are honored. When it is
    empty, fall back to the safe DM-only default so a status report is never broadcast
    into a group.
    """
    if user_id is None:
        return False
    if config.ADMIN_USER_IDS:
        return user_id in config.ADMIN_USER_IDS
    return chat_type == "private"


def _admin_allowed(message: Message) -> bool:
    """Authorization gate for the ops commands (Req 4.3, 4.4)."""
    if not message.from_user:
        return False
    return _is_admin(message.from_user.id, message.chat.type)


def _fmt_uptime(secs) -> str:
    """Render a seconds count as a compact ``2d 3h 4m 5s`` string (zero units trimmed)."""
    try:
        total = int(float(secs))
    except (TypeError, ValueError):
        return str(secs)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, sec = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def _fmt_secs(v) -> str:
    """Render a latency value (seconds) as ``0.84s`` / ``120ms``; pass through non-numbers."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f <= 0:
        return "—"
    if f < 1:
        return f"{f * 1000:.0f}ms"
    return f"{f:.2f}s"


def _mono_table(headers: list[str], rows: list[list], aligns: list[str] | None = None) -> str:
    """Build a space-aligned monospace table body (no surrounding ``<pre>``).

    ``aligns`` is a per-column ``"l"``/``"r"`` list; defaults to left for all. Column
    widths are sized to the widest cell (header included) so columns line up inside a
    Telegram ``<pre>`` block. Trailing padding is stripped per line.
    """
    aligns = aligns or ["l"] * len(headers)
    all_rows = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(headers))]

    def render(row: list[str]) -> str:
        cells = [
            row[i].rjust(widths[i]) if aligns[i] == "r" else row[i].ljust(widths[i])
            for i in range(len(headers))
        ]
        return "  ".join(cells).rstrip()

    # Skip the header line entirely when every header is blank (label/value tables).
    display_rows = all_rows if any(h.strip() for h in headers) else all_rows[1:]
    return "\n".join(render(r) for r in display_rows)


def _render_health(live: dict, ready: dict) -> str:
    """Render a readable HTML /health report (sent with parse_mode='HTML')."""
    summary = live.get("summary", {}) or {}
    status = live.get("status", "unknown")
    status_icon = "🟢" if status == "ok" else "🟠"
    if ready.get("ready"):
        mongo_line = "✅ ok"
    else:
        mongo_line = f"⚠️ degraded ({ready.get('reason', ready.get('mongo', 'error'))})"

    overview = _mono_table(
        ["", ""],
        [
            ["Status", f"{status_icon} {status}"],
            ["Uptime", _fmt_uptime(live.get("uptime_secs", 0))],
            ["MongoDB", mongo_line],
        ],
    )
    stats = _mono_table(
        ["", ""],
        [
            ["LLM calls", str(summary.get("llm_calls_total", 0))],
            ["Reply latency", f"avg {_fmt_secs(summary.get('reply_latency_avg', 0))} · "
                              f"max {_fmt_secs(summary.get('reply_latency_max', 0))}"],
            ["Throttle drops", str(summary.get("throttle_drops", 0))],
            ["Queue drops", str(summary.get("queue_drops", 0))],
            ["Active convos", str(summary.get("conversations_active", 0))],
            ["Extraction runs", str(summary.get("extraction_runs", 0))],
            ["Compression runs", str(summary.get("compression_runs", 0))],
        ],
    )
    return (
        f"🩺 {html.bold(f'{config.bot_display_name} · Health')}\n"
        f"<pre>{html.quote(overview)}</pre>\n"
        f"{html.bold('Metrics summary')}\n"
        f"<pre>{html.quote(stats)}</pre>"
    )


def _render_llm_by_task(snap: dict) -> str:
    """Render the per-task LLM table body (Req 6.4, 6.5, 6.8) as a monospace block.

    Driven by ``LLM_TASK_TYPES`` so every task type appears in a stable order even with
    zero recorded calls; ``.get(..., 0)`` defaults ensure an absent task type never raises.
    """
    counters = snap.get("counters", {}) or {}
    timers = snap.get("timers", {}) or {}
    rows = []
    for task_type, prefix in LLM_TASK_TYPES:
        lat = timers.get(f"llm.{prefix}.latency", {}) or {}
        rows.append([
            task_type,
            counters.get(f"llm.{prefix}.calls", 0),
            counters.get(f"llm.{prefix}.success", 0),
            counters.get(f"llm.{prefix}.failure", 0),
            _fmt_secs(lat.get("avg", 0)),
            _fmt_secs(lat.get("max", 0)),
        ])
    return _mono_table(
        ["task", "calls", "ok", "fail", "avg", "max"],
        rows,
        aligns=["l", "r", "r", "r", "r", "r"],
    )


def _render_metrics(snap: dict) -> str:
    """Render an HTML /metrics report (sent with parse_mode='HTML')."""
    counters = snap.get("counters", {}) or {}
    gauges = snap.get("gauges", {}) or {}
    timers = snap.get("timers", {}) or {}

    sections = [
        f"📊 {html.bold(f'{config.bot_display_name} · Metrics')}",
        f"🤖 {html.bold('LLM calls by task')}",
        f"<pre>{html.quote(_render_llm_by_task(snap))}</pre>",
    ]

    # Raw counters that are not part of the per-task LLM breakdown (those are shown above).
    other_counters = {
        name: value for name, value in counters.items()
        if not (name.startswith("llm.") and name.split(".")[-1] in ("calls", "success", "failure"))
    }
    sections.append(f"🔢 {html.bold('Counters')}")
    if other_counters:
        body = _mono_table(["", ""], [[n, v] for n, v in sorted(other_counters.items())], ["l", "r"])
        sections.append(f"<pre>{html.quote(body)}</pre>")
    else:
        sections.append("<i>none</i>")

    sections.append(f"📈 {html.bold('Gauges')}")
    if gauges:
        body = _mono_table(["", ""], [[n, v] for n, v in sorted(gauges.items())], ["l", "r"])
        sections.append(f"<pre>{html.quote(body)}</pre>")
    else:
        sections.append("<i>none</i>")

    sections.append(f"⏱ {html.bold('Timers')}")
    non_llm_timers = {n: a for n, a in timers.items() if not n.startswith("llm.")}
    if non_llm_timers:
        rows = [
            [n, agg.get("count", 0), _fmt_secs(agg.get("avg", 0)), _fmt_secs(agg.get("max", 0))]
            for n, agg in sorted(non_llm_timers.items())
        ]
        body = _mono_table(["timer", "count", "avg", "max"], rows, ["l", "r", "r", "r"])
        sections.append(f"<pre>{html.quote(body)}</pre>")
    else:
        sections.append("<i>none</i>")

    return "\n".join(sections)


# ===========================================================================
# Interactive guide (inline-button navigation)
# ===========================================================================
# A small, friendly "how this bot works" walkthrough that newcomers can page
# through with Telegram inline buttons. Each screen is identified by a short
# "gd:<screen>" callback key; on_guide_nav() edits the message in place so the
# whole tour happens inside one message. Keep callback_data well under
# Telegram's 64-byte limit.

GUIDE_PREFIX = "gd:"  # callback_data namespace for all guide buttons


def _btn(text: str, screen: str) -> InlineKeyboardButton:
    """Build a guide inline button targeting ``screen`` (e.g. ``home``)."""
    return InlineKeyboardButton(text=text, callback_data=f"{GUIDE_PREFIX}{screen}")


# Ordered guide topics: drives both the home menu and the sequential "Next ▶️"
# navigation so a newcomer can page straight through in a sensible reading order.
_GUIDE_TOPICS: tuple[tuple[str, str], ...] = (
    ("memory", "🧠 Memory"),
    ("privacy", "🔒 Privacy"),
    ("groups", "👥 Groups"),
    ("checkins", "🔔 Check-ins"),
    ("commands", "📋 Commands"),
)


def _kb_guide_home() -> InlineKeyboardMarkup:
    """The guide menu: one button per topic, in reading order."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🧠 How my memory works", "memory")],
        [_btn("🔒 Your privacy & control", "privacy")],
        [_btn("👥 Using me in groups", "groups")],
        [_btn("🔔 Staying in touch", "checkins")],
        [_btn("📋 All commands", "commands")],
    ])


def _kb_topic(screen: str) -> InlineKeyboardMarkup:
    """Footer navigation for a topic screen.

    Every topic gets a consistent ``⬅️ Menu`` button so there is never a dead-end, plus a
    ``Next ▶️`` button (labelled with the next topic) on all but the last screen so the
    whole tour can be paged through in order without bouncing back to the menu each time.
    """
    keys = [k for k, _ in _GUIDE_TOPICS]
    idx = keys.index(screen) if screen in keys else -1
    nav = [_btn("⬅️ Menu", "home")]
    if 0 <= idx < len(keys) - 1:
        next_key, next_label = _GUIDE_TOPICS[idx + 1]
        nav.append(_btn(f"Next: {next_label} ▶️", next_key))
    return InlineKeyboardMarkup(inline_keyboard=[nav])


def _kb_welcome(onboarded: bool) -> InlineKeyboardMarkup:
    """Buttons attached to /start: quick-start (new users), the guide, then commands."""
    rows: list[list[InlineKeyboardButton]] = []
    if not onboarded:
        rows.append([_btn("🚀 Quick start", "onboard")])
    rows.append([_btn("📖 How I work", "home")])
    rows.append([_btn("📋 Commands", "commands")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_onboard() -> InlineKeyboardMarkup:
    """Buttons attached to the /onboard intro (kept separate from its plain text)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📖 What I can do", "home")],
        [_btn("📋 Commands", "commands")],
    ])


def _build_help_text(is_admin: bool) -> str:
    """Render the grouped command list shown from /help and the /start guide."""
    resolved = config.COMMANDS

    def section(title: str, keys: tuple[str, ...]) -> list[str]:
        rows: list[str] = []
        for key in keys:
            trigger, enabled = resolved.get(key, (key, True))
            if not enabled:
                continue
            # Admin-only commands are hidden from non-admins.
            if key in ("health", "metrics") and not is_admin:
                continue
            _handler, desc = _COMMANDS[key]
            rows.append(f"/{trigger} — {desc}")
        if not rows:
            return []
        return [html.bold(title), *rows, ""]

    lines = [
        f"📋 {html.bold('Command cheat sheet')}",
        "",
        "You can skip commands and just talk to me normally. Use these when you want "
        "a specific shortcut or setting:",
        "",
    ]
    lines += section("Start here", ("start", "help", "onboard", "profile"))
    lines += section("Your controls", ("checkins", "reactions", "reset"))
    lines += section("Group chats", ("quiet", "chatty", "groupbot", "groupmode"))
    if is_admin:
        lines += section("Admin", ("health", "metrics"))
    lines += [
        f"New here? Try /{_trigger('onboard')} for starter questions, or send any "
        "normal message like “help me plan my week”."
    ]
    return "\n".join(lines)


def _guide_home_text() -> str:
    return (
        f"📖 {html.bold(f'{config.bot_display_name} — Quick Guide')}\n\n"
        "I'm not your usual bot. I'm an AI companion who actually "
        f"{html.bold('remembers')} you — our chats, the things you care about, how "
        "you've been doing — and picks up right where we left off.\n\n"
        "The easiest way to use me is simple: say what is on your mind, ask for help "
        "with a task, or tell me what you want me to remember.\n\n"
        "Pick a topic to see how I work 👇"
    )


def _guide_screen(screen: str, is_admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Return the (HTML text, keyboard) for a guide ``screen`` key.

    Command names are resolved through :func:`_trigger` so the copy stays correct even
    when a command has been renamed in the environment. Unknown screens fall back to the
    home menu, so a stale button can never dead-end.
    """
    if screen == "memory":
        text = (
            f"🧠 {html.bold('How my memory works')}\n\n"
            "Most bots forget everything the moment you close the chat. I don't.\n\n"
            "As we talk, I quietly pick out the things worth keeping — facts about you, "
            "what's going on in your life, what you're into — and tuck them away. Next "
            "time, I already know them, so you never have to repeat yourself.\n\n"
            "There's nothing to set up and no special format. Just talk to me normally: "
            "goals, preferences, plans, people, projects, moods, half-formed thoughts. "
            "The more we chat, the better I get.\n\n"
            f"• See what I've remembered → /{_trigger('profile')}\n"
            f"• Start completely fresh → /{_trigger('reset')}"
        )
        return text, _kb_topic(screen)

    if screen == "privacy":
        text = (
            f"🔒 {html.bold('Your privacy & control')}\n\n"
            "Your memories are yours, and you're always in charge of them.\n\n"
            f"• /{_trigger('profile')} — see everything I've saved about you, in plain text.\n"
            f"• /{_trigger('reset')} — wipe it all and start over. I'll ask you to confirm "
            "first, since there's no undo.\n\n"
            "Everyone's memories are kept completely separate — what you tell me stays "
            "between us."
        )
        return text, _kb_topic(screen)

    if screen == "groups":
        text = (
            f"👥 {html.bold('Using me in groups')}\n\n"
            "Add me to a group and I'll join in like another member.\n\n"
            "• I always reply when you @mention me, call me by name, or reply to one of "
            "my messages.\n"
            "• Otherwise I only chime in now and then, so I never spam the chat.\n\n"
            f"{html.bold('Fine-tune how chatty I am with you')} (just your own setting — it "
            "won't affect anyone else here):\n"
            f"• /{_trigger('quiet')} — I'll hang back.\n"
            f"• /{_trigger('chatty')} — I'll join in more.\n\n"
            f"{html.bold('Group admins can also:')}\n"
            f"• /{_trigger('groupbot')} on|off — turn me on or off for the whole group.\n"
            f"• /{_trigger('groupmode')} quiet|chatty|normal — set how chatty I am for "
            "everyone here (normal = each person's own setting applies).\n\n"
            "A group admin's choice takes priority over personal "
            f"/{_trigger('quiet')} or /{_trigger('chatty')} settings."
        )
        return text, _kb_topic(screen)

    if screen == "checkins":
        text = (
            f"🔔 {html.bold('Staying in touch')}\n\n"
            "Every so often, if it's been a while, I might send you a little check-in — "
            "a friendly nudge based on something we talked about. No spam, and never in "
            "the middle of the night.\n\n"
            f"{html.bold('Prefer I wait for you instead?')}\n"
            f"• /{_trigger('checkins')} off — I'll stop reaching out first.\n"
            f"• /{_trigger('checkins')} on — turn check-ins back on.\n"
            f"• /{_trigger('checkins')} — see your current setting."
        )
        return text, _kb_topic(screen)

    if screen == "commands":
        return _build_help_text(is_admin), _kb_topic(screen)

    # Default / "home": the topic menu.
    return _guide_home_text(), _kb_guide_home()


async def on_guide_nav(callback: CallbackQuery, db: AsyncIOMotorDatabase):
    """Handle every guide inline-button tap by editing the message in place.

    The ``gd:onboard`` button doubles as a real onboarding action (it seeds the profile
    and flips the ``onboarded`` flag), mirroring the /onboard command; all other screens
    are read-only. Edits are best-effort: Telegram rejects a no-op edit (same content),
    and the message may be too old to edit, so failures are swallowed after the spinner
    is dismissed.
    """
    data = (callback.data or "")[len(GUIDE_PREFIX):]
    user = callback.from_user
    msg = callback.message
    if msg is None:
        await callback.answer()
        return

    is_admin = _is_admin(user.id if user else None, msg.chat.type if msg.chat else None)

    if data == "onboard":
        if user:
            await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
            await models.set_onboarded(db, user.id, True)
        text, kb = _onboard_text(), _kb_onboard()
    else:
        text, kb = _guide_screen(data, is_admin)

    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:  # noqa: BLE001 — no-op edit / too-old message / etc.
        logger.debug(f"guide edit_text failed for {data!r}: {exc}")
    await callback.answer()


def _onboard_text() -> str:
    """The static, persona-consistent onboarding intro (plain text, no LLM, no markdown)."""
    return (
        f"hey, glad you're here. i'm {config.bot_display_name} — think of me less like an "
        "app and more like a friend who actually remembers your stuff. the more we talk, "
        "the better i get at it.\n\n"
        "easy way to start: send me one message with anything you want me to know. what "
        "should i call you, what do your days usually look like, and what are you working "
        "on or excited about lately?\n\n"
        f"after that, just talk to me normally. use /{_trigger('help')} anytime for "
        "commands, settings, memory controls, or group chat tips."
    )


async def cmd_start(message: Message, db: AsyncIOMotorDatabase):
    user = message.from_user
    if not user:
        return
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
    name = html.bold(user.first_name or "there")
    doc = await db["user_profiles"].find_one({"_id": user.id})
    onboarded = bool(doc and doc.get("onboarded"))

    if onboarded:
        # Returning user: no /onboard nudge (Req 4.5). Keep it warm and short.
        msg = (
            f"Hey {name}! 👋 Good to see you again.\n\n"
            "Want a refresher on what I can do, or to peek at what I remember about you? "
            f"Tap below, use /{_trigger('help')}, or just pick up right where we left off."
        )
    else:
        msg = (
            f"Hey {name}! 👋\n\n"
            f"I'm {html.bold(config.bot_display_name)}, an AI companion who actually "
            "remembers you — our chats, what you care about, how things are going. The "
            "more we talk, the better I get to know you.\n\n"
            f"{html.bold('Quick start:')} say anything you want help with, or try "
            f"/{_trigger('onboard')} if you want starter questions. Use "
            f"/{_trigger('help')} anytime for commands, settings, and memory controls."
        )
    await _reply(message, msg, parse_mode="HTML", reply_markup=_kb_welcome(onboarded))


async def cmd_help(message: Message, db: AsyncIOMotorDatabase):
    """Show the command cheat sheet directly, without making users enter the guide first."""
    _ = db
    user_id = message.from_user.id if message.from_user else None
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    await _reply(
        message,
        _build_help_text(_is_admin(user_id, chat_type)),
        parse_mode="HTML",
        reply_markup=_kb_topic("commands"),
    )


async def cmd_onboard(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    user = message.from_user
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")
    await models.set_onboarded(db, user.id, True)
    # Static, persona-consistent, plain-text intro (no markdown/bullets, no LLM call).
    # Does not gate or alter normal chat handling (Req 4.1, 4.2, 4.3). The inline
    # buttons are navigation only and live outside the (plain) message text.
    await _reply(message, _onboard_text(), reply_markup=_kb_onboard())


async def cmd_checkins(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    """Turn my occasional proactive check-ins on/off, or report the setting when used alone.

    ``/checkins`` reports the current state, ``/checkins on`` re-enables nudges, and
    ``/checkins off`` stops me from messaging first.
    """
    if not message.from_user:
        return
    user = message.from_user
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")

    trig = _trigger("checkins")
    desired = _parse_toggle(command.args)
    if desired is None:
        current = await models.get_proactive_enabled(db, user.id)
        if current:
            status = (
                f"My check-ins are {html.bold('on')} — I'll occasionally reach out if it's "
                "been a while."
            )
        else:
            status = f"My check-ins are {html.bold('off')} — I won't message you first."
        await _reply(message, 
            f"{status}\n\nUse /{trig} on or /{trig} off to change it.",
            parse_mode="HTML",
        )
        return

    await models.set_proactive_enabled(db, user.id, desired)
    if desired:
        await _reply(message, 
            "Got it — I'll check in now and then if it's been a while. Good to have you back. 🌱"
        )
    else:
        await _reply(message, 
            "Okay, I won't message you first anymore — I'll be right here whenever you want "
            f"to talk. Send /{trig} on if you'd like me to check in again now and then."
        )


async def cmd_reactions(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    """Turn emoji reactions on the user's messages on/off, or report the setting when alone.

    Per-user opt-out for people who find the little 👍/❤️ reactions on their messages
    annoying. Used alone, ``/reactions`` reports the current state; ``on``/``off`` set it.
    The preference is keyed on the user, so it follows them everywhere (the reaction is
    applied to *their* message regardless of chat).
    """
    if not message.from_user:
        return
    user = message.from_user
    await models.ensure_user(db, user.id, user.username or "", user.first_name or "")

    trig = _trigger("reactions")
    desired = _parse_toggle(command.args)
    if desired is None:
        current = await models.get_reactions_enabled(db, user.id)
        state = html.bold("on") if current else html.bold("off")
        await _reply(message, 
            f"Emoji reactions on your messages are currently {state}.\n\n"
            f"Use /{trig} on or /{trig} off to change it.",
            parse_mode="HTML",
        )
        return

    await models.set_reactions_enabled(db, user.id, desired)
    if desired:
        await _reply(message, "Okay, I'll add little emoji reactions to your messages again. 👍")
    else:
        await _reply(message, 
            "Got it — no more emoji reactions on your messages. Send "
            f"/{trig} on if you change your mind. 🙂"
        )


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
        await _reply(message, 
            "I don't have any memories saved for you yet — we just need to chat a bit "
            f"first! Say hi, or try /{_trigger('onboard')} for a quick intro. 🌱"
        )
        return

    profile_data, _ = await build_memory_block(db, user_id)
    await _reply(message, 
        f"📋 {html.bold('Here is what I remember about you:')}\n\n"
        f"{html.code(profile_data)}\n\n"
        f"Want me to forget all of it? Send /{_trigger('reset')}.",
        parse_mode="HTML",
    )


async def cmd_reset(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    if (command.args or "").strip().lower() != "confirm":
        await _reply(message, 
            "⚠️ This will erase everything I remember about you and our chats — there's "
            "no undo.\n\n"
            f"If you're sure, send: /{_trigger('reset')} confirm"
        )
        return

    user = message.from_user
    # Back up the full profile to the Logs_Channel BEFORE deleting, so an admin can
    # restore it later if the user changes their mind. Best-effort: a backup failure is
    # logged (and surfaces on the channel via the error sink) but never blocks the reset
    # the user explicitly asked for.
    try:
        snapshot = await models.export_user_data(db, user.id)
        if snapshot is not None:
            payload = json.dumps(snapshot, default=str, ensure_ascii=False, indent=2)
            uname = f"@{user.username}" if user.username else "—"
            caption = (
                f"🗂 Profile backup before /{_trigger('reset')}\n"
                f"👤 {user.first_name or 'user'} ({uname})\n"
                f"🆔 {user.id}\n"
                f"🕐 {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
            )
            await log_forwarder.send_document(
                message.bot,
                message.chat.id,
                filename=f"backup_{user.id}.json",
                content=payload.encode("utf-8"),
                caption=caption,
            )
    except Exception as exc:  # noqa: BLE001 - never block the reset on a backup failure
        is_perm = any(
            p in str(exc).lower()
            for p in ["forbidden", "permission", "write access", "not enough rights", "restricted", "kicked", "blocked"]
        )
        if is_perm:
            logger.info(f"reset backup failed (forbidden/restricted) for user {user.id}: {exc}")
        else:
            logger.warning(f"reset backup failed for user {user.id}: {exc}")

    await models.reset_user(db, user.id)
    await _reply(message, 
        "Done — I've cleared everything and we're starting fresh. 🌱\n\n"
        "Changed your mind later? A backup was just saved, so reach out to an admin and "
        "they can help bring your memories back."
    )


async def cmd_quiet(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    if message.chat.type == "private":
        await _reply(message, 
            f"/{_trigger('quiet')} and /{_trigger('chatty')} control how much I chime in "
            "inside a group. Here in our DM I always reply to you, so there's nothing to "
            "quiet. 🙂"
        )
        return
    await affinity_cache.set_mode(db, message.chat.id, message.from_user.id, "quiet")
    await _reply(message, 
        "Okay, I'll hang back around you here — this is just your own personal setting, it "
        "won't change how I act with anyone else in the group. Mention me anytime you need "
        "me. 🤫"
    )


async def cmd_chatty(message: Message, db: AsyncIOMotorDatabase):
    if not message.from_user:
        return
    if message.chat.type == "private":
        await _reply(message, 
            f"/{_trigger('quiet')} and /{_trigger('chatty')} control how much I chime in "
            "inside a group. Here in our DM I always reply to you, so there's nothing to "
            "boost. 🙂"
        )
        return
    await affinity_cache.set_mode(db, message.chat.id, message.from_user.id, "chatty")
    await _reply(message, 
        "You got it — I'll chime in more with you here! This is just your own personal "
        "setting and won't change how I act with anyone else in the group. 😄"
    )


async def _is_group_admin(message: Message) -> bool:
    """Authorize the group kill-switch commands.

    Allowed when the issuer is a configured global admin (``ADMIN_USER_IDS``) OR an
    administrator/creator of this group. The chat-member lookup is a network round-trip,
    so any failure degrades to "not allowed" — the command simply does nothing rather
    than raising. DMs are rejected by the caller before this is consulted.
    """
    user = message.from_user
    if not user:
        return False
    if config.ADMIN_USER_IDS and user.id in config.ADMIN_USER_IDS:
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, user.id)
        return getattr(member, "status", None) in ("administrator", "creator")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"get_chat_member failed for {user.id} in {message.chat.id}: {e}")
        return False


async def cmd_groupbot(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    """Turn me on/off for the whole group, or report the current state when used alone.

    Group-only. Viewing the state is open to anyone in the chat; changing it is
    admin-gated.
    """
    if message.chat.type not in ("group", "supergroup"):
        await _reply(message, 
            "This only works inside a group — it turns me on or off for everyone here. "
            "In our DM I'm always around. 🙂"
        )
        return

    trig = _trigger("groupbot")
    desired = _parse_toggle(command.args)
    if desired is None:
        current = await models.is_group_enabled(db, message.chat.id)
        state = html.bold("on") if current else html.bold("off")
        await _reply(message, 
            f"I'm currently {state} in this group.\n\n"
            f"A group admin can use /{trig} on or /{trig} off to change it.",
            parse_mode="HTML",
        )
        return

    if not await _is_group_admin(message):
        await _reply(message, "Only a group admin can do that. 🙂")
        return
    await models.set_group_enabled(db, message.chat.id, desired)
    if desired:
        await _reply(message, "I'm back on in this group — talk to me anytime. 👋")
    else:
        await _reply(message, 
            "Okay, I'll go quiet in this group — I won't reply or remember anything here "
            f"until an admin turns me back on with /{trig} on. 🤐"
        )


async def _set_group_mode(message: Message, db: AsyncIOMotorDatabase, *, mode: str) -> None:
    """Shared body for the group-wide ``/groupmode`` command (quiet/chatty/normal).

    Group-only and admin-gated (same authorization as the kill switch). Sets the
    group-wide ambient mode, which takes PRIORITY over each member's personal
    /quiet|/chatty: when an admin sets the group to quiet or chatty, that wins for
    everyone here regardless of their own setting. ``/groupmode normal`` (mode ``auto``)
    clears the override so personal settings apply again.
    """
    if message.chat.type not in ("group", "supergroup"):
        await _reply(message, 
            "This only works inside a group — it sets how chatty I am for everyone here. "
            "In our DM I'm always around. 🙂"
        )
        return
    if not await _is_group_admin(message):
        await _reply(message, "Only a group admin can do that. 🙂")
        return
    await models.set_group_mode(db, message.chat.id, mode)
    if mode == "quiet":
        await _reply(message, 
            "Okay, I'll hang back for the whole group — I'll still reply when someone "
            "@mentions me or replies to me, just no chiming in on my own. This overrides "
            f"everyone's personal /{_trigger('chatty')} here until an admin runs "
            f"/{_trigger('groupmode')} normal. 🤫"
        )
    elif mode == "chatty":
        await _reply(message, 
            "You got it — I'll join in more across the whole group! This overrides "
            f"everyone's personal /{_trigger('quiet')} here until an admin runs "
            f"/{_trigger('groupmode')} normal. 😄"
        )
    else:  # auto
        await _reply(message, 
            "Back to normal for the group — everyone's own "
            f"/{_trigger('quiet')} or /{_trigger('chatty')} setting applies again. 🙂"
        )


async def cmd_groupmode(message: Message, command: CommandObject, db: AsyncIOMotorDatabase):
    """Set how chatty I am for the WHOLE group: ``/groupmode quiet|chatty|normal``.

    Group-only. A bare ``/groupmode`` reports the current group-wide setting (open to
    anyone); changing it is admin-gated. The group-wide setting overrides each member's
    personal ``/quiet``|``/chatty`` until an admin sets it back to ``normal``.
    """
    if message.chat.type not in ("group", "supergroup"):
        await _reply(
            message,
            "This only works inside a group — it sets how chatty I am for everyone here. "
            "In our DM I'm always around. 🙂",
        )
        return

    trig = _trigger("groupmode")
    arg = (command.args or "").strip().lower()
    mode = {
        "quiet": "quiet",
        "chatty": "chatty",
        "normal": "auto", "auto": "auto", "reset": "auto", "clear": "auto",
    }.get(arg)

    if mode is None:
        # No (or unrecognized) argument -> report the current setting + usage. Reading the
        # state is open to anyone; only changing it is admin-gated (below).
        try:
            current = await models.get_group_mode(db, message.chat.id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"group-mode read failed for {message.chat.id}: {e}")
            current = "auto"
        label = {"quiet": "quiet", "chatty": "chatty"}.get(current, "normal")
        await _reply(
            message,
            f"My group-wide setting here is {html.bold(label)}.\n\n"
            f"A group admin can change it with /{trig} quiet, /{trig} chatty, "
            f"or /{trig} normal.",
            parse_mode="HTML",
        )
        return

    await _set_group_mode(message, db, mode=mode)


async def cmd_health(message: Message, db: AsyncIOMotorDatabase):
    if not _admin_allowed(message):
        return  # fail closed; never leak a report (Req 4.3, 4.4)
    live = liveness()
    ready = await readiness(db)
    await _reply(message, _render_health(live, ready), parse_mode="HTML")


async def cmd_metrics(message: Message, db: AsyncIOMotorDatabase):
    if not _admin_allowed(message):
        return  # same authorization rule as /health (Req 4.5)
    await _reply(message, _render_metrics(metrics.snapshot()), parse_mode="HTML")


# Static map: command_key -> (handler, help description). Order follows _BUILTIN_COMMANDS.
_COMMANDS: dict[str, tuple] = {
    "start":   (cmd_start,   "open the welcome menu and quick guide"),
    "help":    (cmd_help,    "show commands, examples, settings, and group tips"),
    "onboard": (cmd_onboard, "answer starter questions so I can learn your basics"),
    "checkins": (cmd_checkins, "view or change occasional check-ins — /checkins on|off"),
    "profile": (cmd_profile, "see what I've remembered about you"),
    "reset":   (cmd_reset,   "erase your saved memories — requires /reset confirm"),
    "reactions": (cmd_reactions, "view or change emoji reactions — /reactions on|off"),
    "quiet":   (cmd_quiet,   "personal group setting: I chime in less around you"),
    "chatty":  (cmd_chatty,  "personal group setting: I chime in more around you"),
    "groupbot": (cmd_groupbot, "group admin: turn me on or off here — /groupbot on|off"),
    "groupmode": (cmd_groupmode, "group admin: set group chattiness — /groupmode quiet|chatty|normal"),
    "health":  (cmd_health,  "ops health + readiness report (admin-only)"),
    "metrics": (cmd_metrics, "ops metrics snapshot, incl. LLM-by-task (admin-only)"),
}


# Public command menus surfaced in Telegram's "/" menu (published via set_my_commands at
# startup). DMs get the most useful self-service commands; groups get group-safe controls.
_MENU_DM_KEYS: tuple[str, ...] = (
    "start",
    "help",
    "onboard",
    "checkins",
    "profile",
    "reset",
    "reactions",
)

_MENU_GROUP_KEYS: tuple[str, ...] = (
    "start",
    "help",
    "quiet",
    "chatty",
    "groupbot",
    "groupmode",
)


def _menu_for(keys: tuple[str, ...]) -> list[BotCommand]:
    """Build the ``BotCommand`` list for ``keys``, honoring renames/disables from config."""
    resolved = config.COMMANDS
    out: list[BotCommand] = []
    for key in keys:
        trigger, enabled = resolved.get(key, (key, True))
        if not enabled:
            continue
        _handler, desc = _COMMANDS[key]
        out.append(BotCommand(command=trigger, description=desc[:256]))  # Telegram caps at 256
    return out


async def setup_bot_commands(bot) -> None:
    """Publish DM and group command menus to Telegram's "/" menu.

    The menus honor command renames/disables from config and stay best-effort: a failure
    is logged and never blocks startup.
    """
    if not config.TELEGRAM_PUBLISH_COMMANDS:
        logger.info("Telegram command menu publishing is disabled by config.")
        return
    try:
        await bot.set_my_commands(_menu_for(_MENU_DM_KEYS), scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            _menu_for(_MENU_GROUP_KEYS), scope=BotCommandScopeAllGroupChats()
        )
        logger.info("Published Telegram command menus for default and group scopes.")
    except Exception as exc:  # noqa: BLE001 - cosmetic; never block startup
        logger.warning(f"set_my_commands failed (command menu not published): {exc}")


def register_commands(router: Router) -> None:
    """Bind each ENABLED Built_In_Command to its configured trigger (Req 7.3, 7.4)."""
    resolved = config.COMMANDS
    for key, (handler, _desc) in _COMMANDS.items():
        trigger, enabled = resolved.get(key, (key, True))
        if not enabled:
            logger.info(f"command {key!r} disabled by config; not registering")
            continue  # disabled -> unregistered -> no response to its trigger (Req 7.3)
        try:
            router.message(Command(trigger))(handler)
        except Exception as exc:  # extreme defense: bad trigger slipped through
            logger.warning(
                f"failed to register command {key!r} as {trigger!r} ({exc}); "
                f"registering under default {key!r}"
            )
            router.message(Command(key))(handler)


# Bind at import time so handlers/__init__.py picks up a fully-wired router.
register_commands(router)

# Guide navigation is event-driven (inline buttons), not a slash command, so it is
# registered directly rather than through register_commands. It matches any callback
# whose data starts with the "gd:" namespace.
router.callback_query(F.data.startswith(GUIDE_PREFIX))(on_guide_nav)
