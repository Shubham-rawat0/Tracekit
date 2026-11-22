"""Bounded asynchronous persistence primitives."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Literal, TypeVar

T = TypeVar("T")
# Sentinel propagated through the queue so each worker can exit after draining.
SHUTDOWN = object()
OverflowPolicy = Literal["block", "drop_new", "drop_oldest", "raise"]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff: float = 0.01
    multiplier: float = 2.0
    max_backoff: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff < 0:
            raise ValueError("initial_backoff must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.max_backoff < self.initial_backoff:
            raise ValueError("max_backoff must be at least initial_backoff")

    def backoff_for(self, attempt: int) -> float:
        if attempt < 1:
            return self.initial_backoff
        # Exponential backoff capped at max_backoff to bound retry latency.
        delay = self.initial_backoff * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_backoff)


@dataclass
class WriterMetrics:
    # Counters expose backpressure/failure behavior for observability and health checks.
    enqueued: int = 0
    flushed: int = 0
    failed_batches: int = 0
    retries: int = 0
    dropped: int = 0
    last_error: BaseException | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "flushed": self.flushed,
            "failed_batches": self.failed_batches,
            "retries": self.retries,
            "dropped": self.dropped,
            "last_error": None if self.last_error is None else repr(self.last_error),
        }


class BoundedAsyncWriter(Generic[T]):
    def __init__(
        self,
        write_batch: Callable[[list[T]], Any],
        *,
        queue_size: int = 1000,
        batch_size: int = 100,
        flush_interval: float | None = 1.0,
        retry_policy: RetryPolicy | None = None,
        overflow: OverflowPolicy = "block",
        enqueue_timeout: float | None = None,
        workers: int = 1,
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if flush_interval is not None and flush_interval < 0:
            raise ValueError("flush_interval must be non-negative")
        if overflow not in {"block", "drop_new", "drop_oldest", "raise"}:
            raise ValueError(f"unsupported overflow policy: {overflow}")
        if not callable(write_batch):
            raise TypeError("write_batch must be callable")
        self.write_batch = write_batch
        self.queue_size = queue_size
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.retry_policy = retry_policy or RetryPolicy()
        self.overflow = overflow
        self.enqueue_timeout = enqueue_timeout
        self.workers = workers
        self.logger = logger or logging.getLogger("tracekit.async_writer")
        self._sleep = sleep
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self.metrics = WriterMetrics()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        return self._started and not self._closing

    def start(self) -> bool:
        if self._started or self._closing:
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._started = True
        self._worker_tasks = [
            asyncio.create_task(self._worker()) for _ in range(self.workers)
        ]
        if self.flush_interval:
            self._flush_task = asyncio.create_task(self._periodic_flush())
        return True

    async def enqueue(self, item: T) -> bool:
        # Start lazily so writers work when first used inside a running event loop.
        self._ensure_started()
        # Overflow policy controls backpressure: block, reject, or evict oldest.
        if self.overflow == "drop_new" and self._queue.full():
            self.metrics.dropped += 1
            return False
        if self.overflow == "drop_oldest" and self._queue.full():
            try:
                self._queue.get_nowait()
                self.metrics.dropped += 1
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
        try:
            if self.overflow == "block":
                if self.enqueue_timeout is not None:
                    await asyncio.wait_for(self._queue.put(item), timeout=self.enqueue_timeout)
                else:
                    await self._queue.put(item)
            else:
                self._queue.put_nowait(item)
        except (asyncio.QueueFull, asyncio.TimeoutError):
            self.metrics.dropped += 1
            return False
        self.metrics.enqueued += 1
        return True

    def try_enqueue(self, item: T) -> bool:
        # Non-blocking enqueue used from sync/loop contexts where awaiting is unsafe.
        self._ensure_started()
        if self._queue.full():
            if self.overflow == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self.metrics.dropped += 1
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            else:
                self.metrics.dropped += 1
                return False
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.metrics.dropped += 1
            return False
        self.metrics.enqueued += 1
        return True

    def enqueue_sync(self, item: T) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.enqueue(item))
        return self.try_enqueue(item)

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    async def flush(self) -> None:
        self._ensure_started()
        await self._queue.join()

    def flush_sync(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.flush())
            return
        raise RuntimeError("flush_sync cannot be used while an event loop is running")

    async def _worker(self) -> None:
        while True:
            first = await self._queue.get()
            # Re-enqueue sentinel so all workers observe shutdown without coordination races.
            if first is SHUTDOWN:
                self._queue.put_nowait(SHUTDOWN)
                self._queue.task_done()
                return
            batch: list[T] = [first]
            deadline = self._loop_time() + (self.flush_interval or 0)
            while len(batch) < self.batch_size:
                remaining = deadline - self._loop_time()
                if remaining <= 0 and self.flush_interval:
                    break
                if remaining <= 0:
                    remaining = 0.001
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if item is SHUTDOWN:
                    self._queue.put_nowait(SHUTDOWN)
                    self._queue.task_done()
                    break
                batch.append(item)
            try:
                await self._write_with_retry(batch)
            finally:
                # task_done must match every dequeued item, even on write failure.
                for _ in batch:
                    self._queue.task_done()

    def _loop_time(self) -> float:
        return asyncio.get_running_loop().time()

    async def _write_with_retry(self, batch: list[T]) -> bool:
        policy = self.retry_policy
        # Retry transient failures per batch; after exhaustion items are dropped and counted.
        for attempt in range(1, policy.max_attempts + 1):
            try:
                result = self.write_batch(batch)
                if inspect.isawaitable(result):
                    await result
                self.metrics.flushed += len(batch)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.failed_batches += 1
                self.metrics.last_error = exc
                if attempt >= policy.max_attempts:
                    self.metrics.dropped += len(batch)
                    self.logger.error(
                        "tracekit async writer dropped %d items after %d attempts",
                        len(batch),
                        attempt,
                        exc_info=exc,
                    )
                    return False
                self.metrics.retries += 1
                await self._sleep(policy.backoff_for(attempt))
        return False

    async def _periodic_flush(self) -> None:
        interval = self.flush_interval or 1.0
        try:
            while not self._closing:
                await self._sleep(interval)
                if self._closing:
                    break
                await self.flush()
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        # Idempotent close: concurrent callers wait for the same shutdown to finish.
        if self._closing:
            await self._wait_for_workers()
            return
        self._closing = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        if self._started:
            # One sentinel per worker ensures every worker receives a shutdown signal.
            for _ in self._worker_tasks:
                await self._queue.put(SHUTDOWN)
        await self._wait_for_workers()
        self._started = False

    async def _wait_for_workers(self) -> None:
        if not self._worker_tasks:
            return
        try:
            await asyncio.gather(*self._worker_tasks)
        finally:
            self._worker_tasks = []
            remaining: list[Any] = []
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is SHUTDOWN:
                    self._queue.task_done()
                else:
                    remaining.append(item)
            for item in remaining:
                self._queue.put_nowait(item)

    def health_check(self) -> dict[str, Any]:
        status = "ok"
        if self.metrics.dropped:
            status = "degraded"
        if self._closing or not self._started:
            status = "stopped" if status == "ok" else status
        return {
            "status": status,
            "running": self.is_running,
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue_size,
            "workers": self.workers,
            **self.metrics.snapshot(),
        }
