"""Best-effort operational event forwarding to the configured Telegram logs channel.

Forwards exactly three explicit events (identity captured/refreshed, extraction-saved,
extraction-skipped). Every send is wrapped so a forwarding failure can never raise on a hot
path. Events whose SOURCE chat is the logs channel are dropped (Req 4.10). Forwarder logs are
bound with extra={"no_forward": True} so the Error_Log_Sink will not re-forward them (Req 4.9).
"""
from loguru import logger

from app.config import config

# Forwarder logs must never be re-forwarded by the Error_Log_Sink, so bind a marker.
_log = logger.bind(no_forward=True)

# Optional process-wide bot reference for callers that lack a Message (background
# extractor). Set once at startup; handlers pass message.bot directly.
_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


async def send(bot, source_chat_id: int | None, text: str) -> None:
    """Forward `text` to LOGS_CHANNEL_ID. No-op if disabled, recursive, or bot missing."""
    try:
        target = config.LOGS_CHANNEL_ID
        if not target:
            return
        # Anti-recursion: never forward events whose source chat is the logs channel (Req 4.10).
        if source_chat_id is not None and source_chat_id == target:
            return
        b = bot or _bot
        if b is None:
            return
        await b.send_message(chat_id=target, text=text)
    except Exception as e:  # noqa: BLE001 - discard failures (Req 4.8)
        _log.debug(f"log_forwarder send failed (discarded): {e}")
