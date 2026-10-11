"""Loguru sink that forwards bot-wide ``WARNING``+ records to the Logs_Channel.

loguru invokes a sink synchronously inside the originating logging call, frequently
from threads with no running event loop. This sink therefore:

* never blocks that call (it only schedules work onto the captured loop) (Req 4.6),
* never raises back into it (every layer swallows exceptions) (Req 4.7),
* never forwards records it (or the Log_Forwarder) produced, avoiding an infinite
  forward loop (Req 4.9), and
* only forwards ``WARNING`` and above (Req 4.5).
"""

import asyncio
import contextvars

from app.config import config

# Re-entry guard: set while the sink is dispatching, so any logging triggered by the
# sink (or by send_message internals) is not itself forwarded (Req 4.9).
_in_sink: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_error_log_sink", default=False
)


class ErrorLogSink:
    """Callable Loguru sink with owned, drainable forwarding tasks."""

    def __init__(self, bot, loop):
        self._bot = bot
        self._loop = loop
        self._tasks: set[asyncio.Task] = set()
        self._accepting = True
        self._closed = False

    async def close(self, *, timeout: float = 5.0) -> None:
        self._accepting = False
        # Dispatch callbacks queued before admission stopped, then include their
        # forwarding tasks in the drain rather than dropping the last warning.
        await asyncio.sleep(0)
        self._closed = True
        tasks = set(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def __call__(self, message):
        # loguru passes a Message whose .record holds structured fields.
        if not self._accepting or not config.LOGS_CHANNEL_ID:
            return
        try:
            record = message.record
            # 1) Re-entry / self-forward guard (Req 4.9).
            if _in_sink.get():
                return
            if record["extra"].get("no_forward"):
                return
            # 2) Level guard (defense-in-depth; logger.add already filters < WARNING) (Req 4.5).
            if record["level"].no < 30:  # WARNING == 30
                return

            # 3) Filter out noisy Telegram permission/blocked messages to avoid log channel spam (Req 4.11)
            msg_lower = record["message"].lower()
            exc_lower = ""
            if record.get("exception"):
                exc_lower = str(record["exception"]).lower()

            is_noise = False
            for phrase in [
                "forbidden",
                "permission",
                "write access",
                "blocked by the user",
                "kicked",
                "chat not found",
                "user is deactivated",
                "not a member of",
                "not enough rights",
                "admin rights",
                "restricted",
            ]:
                if phrase in msg_lower or phrase in exc_lower:
                    is_noise = True
                    break
            if is_noise:
                return

            text = (
                f"⚠️ {record['level'].name} | {record['name']}:{record['function']} | "
                f"{record['message']}"
            )

            def _dispatch():
                # Runs on the loop thread. Schedule the send; never block the logging call.
                if self._closed:
                    return

                async def _send():
                    token = _in_sink.set(True)
                    try:
                        from app.services import log_forwarder

                        await log_forwarder.send(self._bot, None, text)
                    # Logging failures must not recurse or escape the sink.
                    except Exception:  # noqa: BLE001, S110
                        pass
                    finally:
                        _in_sink.reset(token)

                try:
                    task = asyncio.create_task(_send())
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                # Logging failures must not recurse or escape the sink.
                except Exception:  # noqa: BLE001, S110
                    pass

            # 4) Hop onto the loop thread without blocking the originating logging call (Req 4.6).
            self._loop.call_soon_threadsafe(_dispatch)
        # Logging failures must not recurse or escape the sink.
        except Exception:  # noqa: BLE001, S110
            pass


def make_error_log_sink(bot, loop) -> ErrorLogSink:
    """Build a synchronous sink that can be drained before closing Telegram."""
    return ErrorLogSink(bot, loop)
