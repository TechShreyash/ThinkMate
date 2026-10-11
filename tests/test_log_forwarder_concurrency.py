import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import log_forwarder


async def test_concurrent_flush_preserves_logs_arriving_during_upload(monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()
    documents = []

    async def upload(**kwargs):
        documents.append(kwargs["document"].data)
        if len(documents) == 1:
            started.set()
            await finish.wait()

    monkeypatch.setattr(log_forwarder, "_bot", AsyncMock(send_document=upload))
    log_forwarder._buffer = ["old entry"]
    first = asyncio.create_task(log_forwarder.flush_buffer())
    await asyncio.wait_for(started.wait(), timeout=1)
    log_forwarder._buffer.append("new entry")
    second = asyncio.create_task(log_forwarder.flush_buffer())
    finish.set()
    await asyncio.gather(first, second)
    assert documents == [b"old entry", b"new entry"]
    assert log_forwarder._buffer == []


async def test_failed_upload_restores_snapshot_before_new_arrivals(monkeypatch):
    async def upload(**kwargs):
        log_forwarder._buffer.append("new entry")
        raise RuntimeError("temporary outage")

    monkeypatch.setattr(log_forwarder, "_bot", AsyncMock(send_document=upload))
    log_forwarder._buffer = ["old entry"]
    await log_forwarder.flush_buffer()
    assert log_forwarder._buffer == ["old entry", "new entry"]


async def test_cancelled_upload_restores_snapshot(monkeypatch):
    started = asyncio.Event()

    async def upload(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(log_forwarder, "_bot", AsyncMock(send_document=upload))
    log_forwarder._buffer = ["old entry"]
    task = asyncio.create_task(log_forwarder.flush_buffer())
    await asyncio.wait_for(started.wait(), timeout=1)
    log_forwarder._buffer.append("new entry")
    task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert log_forwarder._buffer == ["old entry", "new entry"]


async def test_close_flushes_without_a_periodic_task(monkeypatch):
    bot = AsyncMock()
    monkeypatch.setattr(log_forwarder, "_bot", bot)
    monkeypatch.setattr(log_forwarder, "_flush_task", None)
    log_forwarder._buffer = ["last entry"]
    await log_forwarder.close()
    bot.send_document.assert_awaited_once()
    assert log_forwarder._buffer == []
    assert log_forwarder._bot is None


@pytest.mark.parametrize("send_count", [2, 20])
async def test_expired_window_upload_cannot_reset_concurrent_rate_reservations(
    monkeypatch, send_count
):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def upload(**kwargs):
        started.set()
        await finish.wait()

    bot = AsyncMock(send_document=upload)
    monkeypatch.setattr(log_forwarder, "_bot", bot)
    monkeypatch.setattr(log_forwarder.time, "time", lambda: 200.0)
    monkeypatch.setattr(log_forwarder, "BURST_LIMIT_COUNT", 99)
    log_forwarder._window_start = 100.0
    log_forwarder._clubber_activated = True
    log_forwarder._buffer = ["previous window"]

    first = asyncio.create_task(log_forwarder.send(bot, None, "new entry 0"))
    await asyncio.wait_for(started.wait(), timeout=1)
    remaining = [
        asyncio.create_task(log_forwarder.send(bot, None, f"new entry {number}"))
        for number in range(1, send_count)
    ]
    await asyncio.gather(*remaining)
    finish.set()
    await first

    assert bot.send_message.await_count == min(send_count, 10)
    assert log_forwarder._window_count == min(send_count, 10)
    assert len(log_forwarder._buffer) == max(0, send_count - 10)
