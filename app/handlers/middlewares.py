"""aiogram middlewares: per-user throttling and database-session injection."""
import time
from typing import Callable, Dict, Any, Awaitable
from collections import defaultdict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, Update
from app.config import config
from app.database.connection import db_session
from app.services.metrics import metrics


class ThrottlingMiddleware(BaseMiddleware):
    """Sliding-window rate limiter, applied before any DB session is opened."""

    def __init__(self):
        super().__init__()
        self.users: dict[int, list[float]] = defaultdict(list)
        self._last_prune = 0.0

    def _prune(self, now: float):
        """Drop entries for users with no activity in the window (bounds memory)."""
        cutoff = now - config.RATE_LIMIT_WINDOW_SECS
        self.users = defaultdict(
            list,
            {uid: recent for uid, ts in self.users.items()
             if (recent := [t for t in ts if t > cutoff])},
        )
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

        user_id = message.from_user.id
        now = time.time()
        if now - self._last_prune > config.RATE_LIMIT_WINDOW_SECS:
            self._prune(now)
        window = [t for t in self.users[user_id] if now - t < config.RATE_LIMIT_WINDOW_SECS]

        if len(window) >= config.RATE_LIMIT_MAX_REQUESTS:
            # Warn exactly once when first crossing the limit.
            if len(window) == config.RATE_LIMIT_MAX_REQUESTS:
                try:
                    await message.answer(
                        "⚠️ *Slow down!* You're sending messages too fast. Give me a sec.",
                        parse_mode="Markdown",
                    )
                except Exception:  # noqa: BLE001
                    pass
            window.append(now)  # extend the window if they keep spamming
            self.users[user_id] = window
            metrics.incr("throttle.drops")
            return

        window.append(now)
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
