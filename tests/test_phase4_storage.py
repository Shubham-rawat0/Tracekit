import asyncio

import pytest
from backend.apps.db.memory import MemoryBackend
from backend.apps.db.pool import AsyncConnectionPool, PoolConfig, PoolTimeoutError
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.async_writer import BoundedAsyncWriter, RetryPolicy
from tracekit.core.storage import TraceStorage


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_trace_storage_defaults_are_compatible_with_memory_backend():
    backend = MemoryBackend()
    await backend.initialize()

    assert isinstance(backend, TraceStorage)
    await backend.save_trace({"trace_id": "t1", "name": "test"})
    assert await backend.get_trace("t1") == {"trace_id": "t1", "name": "test"}
    assert len(await backend.search_traces(name="test")) == 1
    assert await backend.delete_trace("t1") is True

    health = await backend.health_check()
    assert health == {
        "status": "ok",
        "storage_type": backend.storage_type,
        "storage_detail": backend.storage_detail,
    }
    await backend.close()


@pytest.mark.asyncio
async def test_sqlite_backend_exposes_trace_storage():
    backend = SQLiteBackend(":memory:")
    await backend.initialize()
    assert isinstance(backend, TraceStorage)
    await backend.close()


def test_retry_policy_validation_and_backoff():
    policy = RetryPolicy(max_attempts=4, initial_backoff=0.1, multiplier=3, max_backoff=0.5)
    assert policy.backoff_for(1) == 0.1
    assert policy.backoff_for(2) == pytest.approx(0.3)
    assert policy.backoff_for(3) == 0.5
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_backoff=1, max_backoff=0.5)


@pytest.mark.asyncio
async def test_async_writer_batches_and_drains_on_close():
    batches: list[list[int]] = []

    async def write_batch(items: list[int]) -> None:
        batches.append(list(items))
        await asyncio.sleep(0)

    writer = BoundedAsyncWriter(
        write_batch,
        queue_size=32,
        batch_size=3,
        flush_interval=None,
        sleep=_no_sleep,
    )
    writer.start()
    for item in range(7):
        assert await writer.enqueue(item)

    await writer.close()

    assert sum(len(batch) for batch in batches) == 7
    assert writer.metrics.flushed == 7
    assert writer.metrics.dropped == 0
    assert writer.health_check()["status"] == "stopped"


@pytest.mark.asyncio
async def test_async_writer_retries_transient_failures():
    attempts = 0
    written: list[str] = []

    async def write_batch(items: list[str]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        written.extend(items)

    writer = BoundedAsyncWriter(
        write_batch,
        batch_size=1,
        flush_interval=None,
        retry_policy=RetryPolicy(max_attempts=3, initial_backoff=0, max_backoff=0),
        sleep=_no_sleep,
    )
    await writer.enqueue("span")
    await writer.close()

    assert attempts == 3
    assert written == ["span"]
    assert writer.metrics.retries == 2
    assert writer.metrics.failed_batches == 2
    assert writer.metrics.dropped == 0
    assert writer.health_check()["status"] == "stopped"


@pytest.mark.asyncio
async def test_async_writer_drops_after_retry_exhaustion():
    async def write_batch(_: list[str]) -> None:
        raise RuntimeError("permanent")

    writer = BoundedAsyncWriter(
        write_batch,
        batch_size=1,
        flush_interval=None,
        retry_policy=RetryPolicy(max_attempts=2, initial_backoff=0, max_backoff=0),
        sleep=_no_sleep,
    )
    await writer.enqueue("span")
    await writer.close()

    assert writer.metrics.dropped == 1
    assert writer.health_check()["status"] == "degraded"


@pytest.mark.asyncio
async def test_async_writer_drop_oldest_overflow():
    async def write_batch(_: list[int]) -> None:
        await asyncio.sleep(0.01)

    writer = BoundedAsyncWriter(
        write_batch,
        queue_size=2,
        batch_size=100,
        flush_interval=None,
        overflow="drop_oldest",
        sleep=_no_sleep,
    )
    writer._queue.put_nowait(1)
    writer._queue.put_nowait(2)

    assert writer.try_enqueue(3) is True
    assert [writer._queue.get_nowait(), writer._queue.get_nowait()] == [2, 3]
    assert writer.metrics.dropped == 1


@pytest.mark.asyncio
async def test_async_writer_enqueue_timeout_drops_without_blocking():
    started = asyncio.Event()
    release = asyncio.Event()

    async def write_batch(_: list[int]) -> None:
        started.set()
        await release.wait()

    writer = BoundedAsyncWriter(
        write_batch,
        queue_size=1,
        batch_size=100,
        flush_interval=None,
        overflow="block",
        enqueue_timeout=0.01,
        sleep=_no_sleep,
    )
    writer.start()
    assert await writer.enqueue(1)
    await started.wait()
    writer._queue.put_nowait(0)
    assert await writer.enqueue(2) is False

    release.set()
    await writer.close()
    assert writer.metrics.dropped == 1


@pytest.mark.asyncio
async def test_connection_pool_initializes_reuses_and_closes():
    created: list[dict[str, bool]] = []

    async def factory() -> dict[str, bool]:
        connection = {"closed": False}
        created.append(connection)
        return connection

    async def closer(connection: dict[str, bool]) -> None:
        connection["closed"] = True

    pool = AsyncConnectionPool(
        factory,
        config=PoolConfig(min_size=2, max_size=2, timeout=0.1),
        health_check=lambda connection: not connection["closed"],
        close_connection=closer,
    )
    await pool.initialize()
    assert pool.stats()["idle"] == 2

    async with pool.acquire() as connection:
        assert connection in created
        assert pool.stats()["in_use"] == 1

    assert pool.stats()["idle"] == 2
    assert (await pool.health_check())["status"] == "ok"
    await pool.close()

    assert all(connection["closed"] for connection in created)
    assert (await pool.health_check())["status"] == "closed"


@pytest.mark.asyncio
async def test_connection_pool_times_out_and_discards_unhealthy_connections():
    healthy = {"value": True}

    async def factory() -> dict[str, bool]:
        return {"value": healthy["value"]}

    pool = AsyncConnectionPool(
        factory,
        config=PoolConfig(min_size=1, max_size=1, timeout=0.01),
        health_check=lambda connection: connection["value"],
    )
    await pool.initialize()

    async with pool.acquire() as connection:
        with pytest.raises(PoolTimeoutError):
            async with pool.acquire():
                pass
        connection["value"] = False

    async with pool.acquire() as replacement:
        assert replacement["value"] is True

    stats = pool.stats()
    assert stats["discarded"] == 1
    assert stats["timeouts"] == 1
    await pool.close()
