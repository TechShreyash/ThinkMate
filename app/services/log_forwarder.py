"""Best-effort operational event forwarding to the configured Telegram logs channel.

Forwards a small set of explicit operational events: the three memory events (identity
captured/refreshed, extraction-saved, extraction-skipped) plus the process lifecycle
notices (startup / shutdown) emitted from ``main.py``. Every send is wrapped so a
forwarding failure can never raise on a hot path. Events whose SOURCE chat is the logs
channel are dropped (Req 4.10). Forwarder logs are bound with extra={"no_forward": True}
so the Error_Log_Sink will not re-forward them (Req 4.9).
"""
from loguru import logger

import asyncio
import time
from app.config import config

# Forwarder logs must never be re-forwarded by the Error_Log_Sink, so bind a marker.
_log = logger.bind(no_forward=True)

# Optional process-wide bot reference for callers that lack a Message (background
# extractor). Set once at startup; handlers pass message.bot directly.
_bot = None

# Log clubber state
_window_start = 0.0
_window_count = 0
_buffer = []
_flush_task = None
_loop = None
_clubber_activated = False
_recent_send_times = []

LOG_LIMIT_PER_MINUTE = 10
BURST_LIMIT_COUNT = 3
BURST_LIMIT_WINDOW_SECS = 5.0


def _reset_clubber_window(now: float | None = None) -> None:
    """Start a fresh direct-send window after a flush or long quiet period."""
    global _window_start, _window_count, _clubber_activated, _recent_send_times
    _window_start = now if now is not None else time.time()
    _window_count = 0
    _clubber_activated = False
    _recent_send_times = []


def _buffer_log(now: float, text: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
    _buffer.append(f"[{timestamp} UTC] {text}")


def set_bot(bot) -> None:
    global _bot, _loop, _flush_task
    _bot = bot
    _reset_clubber_window()
    if bot is not None:
        try:
            _loop = asyncio.get_running_loop()
            _flush_task = _loop.create_task(_periodic_flush())
            _log.info("Log clubber: periodic flush task started.")
        except RuntimeError:
            # Running tests outside a running event loop
            pass


async def _periodic_flush() -> None:
    """Periodically flush buffered logs every 60 seconds."""
    _log.info("Log clubber: periodic flush loop running.")
    try:
        while True:
            await asyncio.sleep(60.0)
            await flush_buffer()
            _reset_clubber_window()
    except asyncio.CancelledError:
        _log.info("Log clubber: periodic flush loop cancelled; performing final flush.")
        await flush_buffer()
        raise
    except Exception as e:  # noqa: BLE001
        _log.exception(f"Log clubber: periodic flush loop crashed: {e}")


async def flush_buffer() -> None:
    """Flush all currently buffered logs to the logs channel as a single logs.txt file."""
    global _buffer
    if not _buffer:
        return

    logs_text = "\n".join(_buffer)
    line_count = len(logs_text.splitlines())

    _log.info(f"Log clubber: flushing {line_count} buffered logs to Telegram...")

    try:
        from aiogram.types import BufferedInputFile
        target = config.LOGS_CHANNEL_ID
        if not target:
            _log.warning("Log clubber: LOGS_CHANNEL_ID not configured, aborting flush.")
            _buffer = []
            _reset_clubber_window()
            return
        b = _bot
        if b is None:
            _log.warning("Log clubber: bot reference is None, aborting flush.")
            return

        await b.send_document(
            chat_id=target,
            document=BufferedInputFile(logs_text.encode("utf-8"), filename="logs.txt"),
            caption=f"📋 Clubbed logs ({line_count} lines) due to rate limit threshold exceeded.",
        )
        _buffer = []
        _reset_clubber_window()
        _log.info("Log clubber: flushed buffered logs to Telegram successfully.")
    except Exception as e:  # noqa: BLE001
        _log.exception(f"Log clubber: failed to flush buffered logs to Telegram: {e}")


async def close() -> None:
    """Cancel the periodic flush task and perform a final log flush."""
    global _flush_task
    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            _log.exception(f"Error during log_forwarder task cancellation: {e}")
        _flush_task = None


async def send(bot, source_chat_id: int | None, text: str) -> None:
    """Forward `text` to LOGS_CHANNEL_ID. No-op if disabled, recursive, or bot missing."""
    global _window_start, _window_count, _clubber_activated, _recent_send_times
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

        now = time.time()
        if _window_start <= 0:
            _reset_clubber_window(now)

        if _clubber_activated:
            if now - _window_start < 60.0:
                _buffer_log(now, text)
                return
            await flush_buffer()
            _reset_clubber_window(now)

        # Clean up recent send times to only count messages in the last window
        _recent_send_times = [t for t in _recent_send_times if now - t < BURST_LIMIT_WINDOW_SECS]

        # Reset minute window if 60 seconds elapsed since the start of the current window
        if now - _window_start >= 60.0:
            if _buffer:
                await flush_buffer()
            _reset_clubber_window(now)

        # Check burst rate limit or minute rate limit
        if len(_recent_send_times) >= BURST_LIMIT_COUNT or _window_count >= LOG_LIMIT_PER_MINUTE:
            _clubber_activated = True
            reason = "burst detected" if len(_recent_send_times) >= BURST_LIMIT_COUNT else "minute threshold exceeded"
            _log.info(f"Log clubber: {reason}. Buffering logs until the next flush window.")
            _buffer_log(now, text)
        else:
            _window_count += 1
            _recent_send_times.append(now)
            await b.send_message(chat_id=target, text=text)
    except Exception as e:  # noqa: BLE001 - discard failures (Req 4.8)
        _log.exception(f"log_forwarder send failed (discarded): {e}")


async def diagnostic(bot, source_chat_id: int | None, text: str) -> None:
    """Forward an early-phase *diagnostic* trace to the Logs_Channel.

    A no-op unless ``FORWARD_DIAGNOSTICS`` is enabled (and a channel/bot exist), so the
    verbose per-message routing traces can be switched off in one place once the bot's
    behavior is trusted. Shares :func:`send`'s best-effort, never-raise contract.
    """
    if not config.FORWARD_DIAGNOSTICS:
        return
    await send(bot, source_chat_id, text)


async def send_document(
    bot,
    source_chat_id: int | None,
    filename: str,
    content: bytes,
    caption: str | None = None,
) -> bool:
    """Upload ``content`` as a file named ``filename`` to LOGS_CHANNEL_ID.

    Used to archive backups (e.g. a user's exported profile before a destructive
    ``/reset``) to the logs channel. Mirrors :func:`send`'s safety contract: a no-op when
    the channel is unset, recursive, or no bot is available, and any delivery failure is
    swallowed. Returns ``True`` only when the upload was actually attempted and succeeded,
    so callers can warn an admin if the backup did not land.
    """
    try:
        from aiogram.types import BufferedInputFile

        target = config.LOGS_CHANNEL_ID
        if not target:
            return False
        if source_chat_id is not None and source_chat_id == target:
            return False
        b = bot or _bot
        if b is None:
            return False
        await b.send_document(
            chat_id=target,
            document=BufferedInputFile(content, filename=filename),
            caption=caption,
        )
        return True
    except Exception as e:  # noqa: BLE001 - discard failures (Req 4.8)
        _log.debug(f"log_forwarder send_document failed (discarded): {e}")
        return False
