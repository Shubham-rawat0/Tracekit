import asyncio
import time

import pytest
from backend.apps.db.memory import MemoryBackend
from backend.apps.db.pool import AsyncConnectionPool, PoolConfig
from tracekit.core.async_writer import BoundedAsyncWriter
from tracekit.core.reliability import BoundedBatchQueue


@pytest.mark.asyncio
async def test_async_writer_handles_bounded_high_volume_load():
    flushed: list[int] = []

    async def write_batch(items: list[int]) -> None:
        flushed.extend(items)

    writer = BoundedAsyncWriter(
        write_batch,
        queue_size=128,
        batch_size=64,
        flush_interval=None,
        overflow="block",
    )
    writer.start()
    for item in range(5000):
        assert await writer.enqueue(item)
    await writer.close()

    assert flushed == list(range(5000))
    assert writer.metrics.flushed == 5000
    assert writer.metrics.dropped == 0
    assert writer.queue_depth == 0


@pytest.mark.asyncio
async def test_memory_backend_batch_storage_load():
    backend = MemoryBackend()
    await backend.initialize()
    traces = [
        {"trace_id": f"trace-{index}", "name": "load", "status": "ok"}
        for index in range(2000)
    ]

    started = time.perf_counter()
    await backend.save_traces(traces)
    elapsed = time.perf_counter() - started

    assert await backend.count_documents("traces") == 2000
    assert elapsed < 10.0


@pytest.mark.asyncio
async def test_connection_pool_concurrent_load_respects_max_size():
    active = 0
    peak = 0

    async def factory() -> dict[str, bool]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        return {"in_use": True}

    async def closer(connection: dict[str, bool]) -> None:
        nonlocal active
        active -= 1

    pool = AsyncConnectionPool(
        factory,
        config=PoolConfig(min_size=1, max_size=5, timeout=1.0),
        close_connection=closer,
    )
    await pool.initialize()

    async def worker(index: int) -> int:
        async with pool.acquire() as connection:
            await asyncio.sleep(0)
            assert connection["in_use"] is True
            return index

    results = await asyncio.gather(*(worker(index) for index in range(50)))
    assert results == list(range(50))
    assert peak <= 5
    await pool.close()


def test_bounded_reliability_queue_caps_memory():
    queue = BoundedBatchQueue(max_items=100)
    accepted = sum(queue.put(index) for index in range(1000))
    assert accepted == 100
    assert queue.size == 100
    assert len(queue.drain()) == 100
