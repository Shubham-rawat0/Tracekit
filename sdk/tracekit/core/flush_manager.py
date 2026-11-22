"""Deterministic automatic flush helper for tracer-style record buffers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

FlushCallable = Callable[[], Any]
SleepCallable = Callable[[float], Awaitable[Any]]


class AutomaticFlushManager:
    """Coordinate periodic and threshold-triggered flushes without extra threads."""
    # Uses asyncio tasks/locks instead of threads so flushes align with the caller event loop.

    def __init__(
        self,
        flush: FlushCallable,
        *,
        flush_interval_seconds: float | None = 30.0,
        max_spans_in_memory: int = 1000,
        logger: logging.Logger | None = None,
        sleep: SleepCallable | None = None,
    ) -> None:
        if not callable(flush):
            raise TypeError("flush must be callable")
        interval: float | None = None
        if flush_interval_seconds is not None:
            interval = float(flush_interval_seconds)
            if interval < 0:
                raise ValueError("flush_interval_seconds must be non-negative")
        limit = int(max_spans_in_memory)
        if limit < 0:
            raise ValueError("max_spans_in_memory must be non-negative")
        self.flush = flush
        self.flush_interval_seconds = interval
        self.max_spans_in_memory = limit
        self.logger = logger or logging.getLogger("tracekit.flush_manager")
        self._sleep = sleep or asyncio.sleep
        self._pending = 0
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._loop_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._stopping = False
        self.successful_flushes = 0
        self.failure_count = 0
        self.last_error: BaseException | None = None
        self.periodic_ticks = 0

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def is_flushing(self) -> bool:
        return self._flush_task is not None and not self._flush_task.done()

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def threshold_reached(self) -> bool:
        # max_spans_in_memory <= 0 disables threshold-triggered flushing.
        return self.max_spans_in_memory > 0 and self._pending >= self.max_spans_in_memory

    def notify_pending(self, count: int = 1) -> bool:
        amount = int(count)
        if amount <= 0:
            return False
        self._pending += amount
        return self.threshold_reached

    def notify_span(self, count: int = 1) -> bool:
        return self.notify_pending(count)

    def notify_flushed(self, count: int = 1) -> None:
        self._pending = max(0, self._pending - int(count))

    def clear_pending(self) -> None:
        self._pending = 0

    def maybe_flush(self) -> bool:
        if not self.threshold_reached:
            return False
        return self.schedule_flush()

    def schedule_flush(self) -> bool:
        # Coalesce concurrent triggers; only one in-flight flush task is allowed.
        if self.is_flushing:
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return self._start_flush_task()

    def start_loop(self) -> bool:
        if self.flush_interval_seconds is None or self.flush_interval_seconds <= 0:
            return False
        if self.is_running:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._stopping = False
        self._loop_task = loop.create_task(self._periodic_loop())
        return True

    async def start(self) -> bool:
        return self.start_loop()

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        # Locks are loop-bound; recreate when used from a different event loop.
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _start_flush_task(self) -> bool:
        loop = asyncio.get_running_loop()
        pending_at_start = self._pending
        task = loop.create_task(self._run_flush(pending_at_start))
        task.add_done_callback(self._observe_flush_task)
        self._flush_task = task
        return True

    def _observe_flush_task(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.logger.error(
                "tracekit automatic flush task failed: %s",
                error,
                exc_info=error,
            )

    async def _run_flush(self, pending_at_start: int) -> bool:
        async with self._get_lock():
            try:
                result = self.flush()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # On failure keep pending count so records can be retried by a later flush.
                self.failure_count += 1
                self.last_error = exc
                self.logger.exception(
                    "tracekit automatic flush failed; pending records preserved"
                )
                return False
            # Only decrement by the snapshot captured at schedule time; new arrivals stay pending.
            self._pending = max(0, self._pending - pending_at_start)
            self.successful_flushes += 1
            return True

    async def flush_now(self) -> bool:
        if self.is_flushing:
            return False
        if not self._start_flush_task():
            return False
        task = self._flush_task
        if task is None:
            return False
        return bool(await task)

    async def _periodic_loop(self) -> None:
        interval = self.flush_interval_seconds or 0.0
        try:
            while not self._stopping:
                await self._sleep(interval)
                if self._stopping:
                    break
                self.periodic_ticks += 1
                await self.flush_now()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure_count += 1
            self.last_error = exc
            self.logger.exception("tracekit periodic flush loop stopped by error")

    async def stop(self) -> None:
        self._stopping = True
        loop_task = self._loop_task
        self._loop_task = None
        if loop_task is not None:
            loop_task.cancel()
            await _await_cancelled(loop_task, self.logger)
        flush_task = self._flush_task
        self._flush_task = None
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
        if flush_task is not None:
            await _await_cancelled(flush_task, self.logger)

    async def close(self) -> None:
        await self.stop()


async def _await_cancelled(task: asyncio.Task, logger: logging.Logger) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("tracekit flush task raised during shutdown")
