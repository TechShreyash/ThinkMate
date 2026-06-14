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


def make_error_log_sink(bot, loop):
    """Build a synchronous loguru sink closing over ``bot`` and the main event ``loop``."""

    def sink(message):
        # loguru passes a Message whose .record holds structured fields.
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

            text = (
                f"⚠️ {record['level'].name} | {record['name']}:{record['function']} | "
                f"{record['message']}"
            )

            def _dispatch():
                # Runs on the loop thread. Schedule the send; never block the logging call.
                async def _send():
                    token = _in_sink.set(True)
                    try:
                        await bot.send_message(chat_id=config.LOGS_CHANNEL_ID, text=text)
                    except Exception:  # noqa: BLE001 - never propagate (Req 4.7)
                        pass
                    finally:
                        _in_sink.reset(token)

                try:
                    asyncio.create_task(_send())
                except Exception:  # noqa: BLE001 - loop not running / shutting down
                    pass

            # 3) Hop onto the loop thread without blocking the originating logging call (Req 4.6).
            loop.call_soon_threadsafe(_dispatch)
        except Exception:  # noqa: BLE001 - the sink must NEVER raise into logging (Req 4.7)
            pass

    return sink
