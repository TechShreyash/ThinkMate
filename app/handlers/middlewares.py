"""aiogram middlewares: per-user throttling and database-session injection."""
import time
from typing import Callable, Dict, Any, Awaitable
from collections import defaultdict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, Update
from app.config import config
from app.database.connection import db_session
from app.services.metrics import metrics
from app.services import log_forwarder


class ThrottlingMiddleware(BaseMiddleware):
    """Sliding-window rate limiter, applied before any DB session is opened."""

    def __init__(self):
        super().__init__()
        self.users: dict[int, list[float]] = defaultdict(list)
        # Users currently in a throttled state who have already received the single
        # "slow down" warning for THIS spam episode. Cleared the moment a user drops
        # back under the limit, so a later, separate burst warns them once again.
        self.warned: set[int] = set()
        self._last_prune = 0.0

    def _prune(self, now: float):
        """Drop entries for users with no activity in the window (bounds memory)."""
        cutoff = now - config.RATE_LIMIT_WINDOW_SECS
        self.users = defaultdict(
            list,
            {uid: recent for uid, ts in self.users.items()
             if (recent := [t for t in ts if t > cutoff])},
        )
        # Forget the warned-flag for any user no longer tracked, so memory stays bounded.
        self.warned &= self.users.keys()
        self._last_prune = now

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        message = None
        if isinstance(event, Update) and event.message:
            message = event.message
        elif isinstance(event, Message):
            message = event

        if not message or not message.from_user:
            return await handler(event, data)

        now = time.time()

        # Resolve the message's REAL send time (``message.date``, timezone-aware UTC) for
        # the sliding-window math. Using the send time instead of the processing time is
        # what prevents the burst-catch-up false positive: when Telegram delivers several
        # messages together (after downtime, lag, or a long-poll catch-up) they're all
        # processed at the same instant, so stamping them with ``time.time()`` collapses
        # them into one window and trips the limiter even though the user sent them
        # seconds apart. Fall back to wall-clock ``now`` if the date is missing/odd.
        msg_ts = now
        msg_date = getattr(message, "date", None)
        if msg_date is not None:
            try:
                msg_ts = msg_date.timestamp()
            except Exception:  # noqa: BLE001
                msg_ts = now

        # Drop stale backlog before anything else. On (re)start or catch-up, Telegram can
        # deliver a burst of old messages at once. A real-time chat bot must not reply to
        # them. Compare the message's own send time to wall-clock ``now`` and ignore
        # anything older than the staleness window (no counting, no warning, no reply).
        if config.STALE_MESSAGE_SECS > 0 and (now - msg_ts) > config.STALE_MESSAGE_SECS:
            metrics.incr("throttle.stale_dropped")
            return

        # Never engage with other bots. Without this guard, a second bot in the same
        # chat gets counted in the sliding window like a human; when it posts faster
        # than the limit, ThinkMate fires the "Slow down!" warning, and as timestamps
        # slide out of the window the count keeps re-crossing the threshold — flooding
        # the chat with repeated warnings (bot-to-bot loop). Drop bot-authored updates
        # entirely: no throttling, no warning, and no downstream handler/reply.
        if message.from_user.is_bot:
            return

        # The per-user rate limit applies in EVERY chat (a single user still can't flood
        # the bot). What differs by chat type is the user-facing warning: a public
        # "Slow down" in a group scolds ordinary chatter and floods active groups with
        # warnings for people the bot was never going to answer. So the warning is sent
        # ONLY in private chats; in groups the over-limit message is dropped silently.
        is_private = getattr(getattr(message, "chat", None), "type", None) == "private"

        user_id = message.from_user.id
        # Memory pruning uses wall-clock time (it evicts idle users), independent of the
        # send-time window math below.
        if now - self._last_prune > config.RATE_LIMIT_WINDOW_SECS:
            self._prune(now)
        # Sliding window is computed against the message's send time so catch-up bursts
        # spread across their real timestamps instead of collapsing onto one instant.
        window = [t for t in self.users[user_id] if msg_ts - t < config.RATE_LIMIT_WINDOW_SECS]

        if len(window) >= config.RATE_LIMIT_MAX_REQUESTS:
            # Warn at most once per spam episode, and only in a DM. The per-user "warned"
            # flag is only ever set for private chats, so group over-limit drops stay
            # completely silent (no public message, no re-arming flood). It is cleared
            # below the moment the user falls back under the limit.
            if is_private and user_id not in self.warned:
                self.warned.add(user_id)
                try:
                    await message.answer(
                        "⚠️ *Slow down!* You're sending messages too fast. Give me a sec.",
                        parse_mode="Markdown",
                    )
                except Exception:  # noqa: BLE001
                    pass
                # Early-phase visibility: forward the throttle event once per episode.
                try:
                    chat_id = getattr(getattr(message, "chat", None), "id", None)
                    await log_forwarder.diagnostic(
                        message.bot,
                        chat_id,
                        f"⏳ throttle user={user_id} chat={chat_id} "
                        f"(>{config.RATE_LIMIT_MAX_REQUESTS}/{config.RATE_LIMIT_WINDOW_SECS:g}s) — warned once",
                    )
                except Exception:  # noqa: BLE001
                    pass
            window.append(msg_ts)  # extend the window if they keep spamming
            self.users[user_id] = window
            metrics.incr("throttle.drops")
            return

        # Under the limit: clear any prior warning flag so a future, separate burst
        # warns the user again exactly once.
        self.warned.discard(user_id)
        window.append(msg_ts)
        self.users[user_id] = window
        return await handler(event, data)


class DbSessionMiddleware(BaseMiddleware):
    """Open a DB session per update and inject it as ``db`` into handler kwargs."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with db_session() as db:
            data["db"] = db
            return await handler(event, data)


class ProactiveResetMiddleware(BaseMiddleware):
    """Reset a user's unanswered-proactive streak whenever they issue a command.

    Using any command counts as the user engaging with the bot, so the streak that
    auto-pauses proactive check-ins after ``PROACTIVE_MAX_UNANSWERED`` ignored DMs is
    cleared, making the user eligible for check-ins again. The DM chat path clears the
    same streak inline via ``touch_and_get_last_interaction``; this covers the command
    path. Best-effort: a failure never blocks command handling.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        message = event if isinstance(event, Message) else None
        db = data.get("db")
        if message is not None and message.from_user and db is not None:
            try:
                from app.database import models
                await models.reset_proactive_unanswered(db, message.from_user.id)
            except Exception:  # noqa: BLE001 - never block command handling
                pass
        return await handler(event, data)
