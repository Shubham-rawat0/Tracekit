"""Generic async connection pooling for storage adapters."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

ConnectionFactory = Callable[[], Any]
ConnectionCheck = Callable[[Any], Any]
ConnectionCloser = Callable[[Any], Any]


# Frozen so the queue bound can't drift from max_size after construction.
@dataclass(frozen=True)
class PoolConfig:
    min_size: int = 1
    max_size: int = 10
    timeout: float = 5.0

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("min_size must be non-negative")
        if self.max_size < 1:
            raise ValueError("max_size must be at least 1")
        if self.min_size > self.max_size:
            raise ValueError("min_size cannot exceed max_size")
        if self.timeout < 0:
            raise ValueError("timeout must be non-negative")


class PoolTimeoutError(TimeoutError):
    """Raised when a connection cannot be acquired within the configured timeout."""


class AsyncConnectionPool:
    def __init__(
        self,
        factory: ConnectionFactory,
        *,
        config: PoolConfig | None = None,
        health_check: ConnectionCheck | None = None,
        close_connection: ConnectionCloser | None = None,
    ) -> None:
        self.factory = factory
        self.config = config or PoolConfig()
        self._connection_health_check = health_check
        self._connection_closer = close_connection or self._default_close
        # The queue bounds live connections; the lock only serializes creation, not waiting.
        self._available: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.config.max_size)
        self._all: list[Any] = []
        self._created = 0
        self._discarded = 0
        self._timeouts = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("connection pool is closed")
            while self._created < self.config.min_size:
                await self._create_connection_locked()

    # Caller must hold _lock; factory may be either synchronous or awaitable.
    async def _create_connection_locked(self) -> Any:
        connection = self.factory()
        if inspect.isawaitable(connection):
            connection = await connection
        self._all.append(connection)
        self._created += 1
        self._available.put_nowait(connection)
        return connection

    # A failing check means "unhealthy"; exceptions never escape to the caller.
    async def _is_healthy(self, connection: Any) -> bool:
        if self._connection_health_check is None:
            return True
        try:
            result = self._connection_health_check(connection)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False

    async def _close_connection(self, connection: Any) -> None:
        # Ignore closer failures so the finally block still fixes up pool accounting.
        try:
            result = self._connection_closer(connection)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
        finally:
            for index, existing in enumerate(self._all):
                if existing is connection:
                    self._all.pop(index)
                    break
            self._created = max(0, self._created - 1)
            self._discarded += 1

    # Context manager guarantees release even when the caller's block raises.
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        connection = await self._acquire_connection()
        try:
            yield connection
        finally:
            await self.release(connection)

    async def _acquire_connection(self) -> Any:
        if self._closed:
            raise RuntimeError("connection pool is closed")
        while True:
            try:
                connection = await asyncio.wait_for(
                    self._available.get(), timeout=self.config.timeout
                )
            except asyncio.TimeoutError as exc:
                self._timeouts += 1
                raise PoolTimeoutError(
                    f"timed out acquiring connection after {self.config.timeout}s"
                ) from exc
            # Never hand out a stale connection; discard it and let the loop retry.
            if await self._is_healthy(connection):
                return connection
            await self._close_connection(connection)
            await self._replenish()

    async def _replenish(self) -> None:
        async with self._lock:
            if self._closed:
                return
            while self._created < self.config.min_size:
                await self._create_connection_locked()

    async def release(self, connection: Any) -> None:
        # Connections returned after close, or now-unhealthy ones, are discarded.
        if self._closed or not await self._is_healthy(connection):
            await self._close_connection(connection)
            await self._replenish()
            return
        try:
            self._available.put_nowait(connection)
        except asyncio.QueueFull:
            # Idle slots are full, so drop the extra connection instead of blocking release.
            await self._close_connection(connection)

    # Reuse acquire so the probe also exercises health checking and the timeout path.
    async def health_check(self) -> dict[str, Any]:
        if self._closed:
            return {"status": "closed", **self.stats()}
        try:
            async with self.acquire() as connection:
                healthy = await self._is_healthy(connection)
        except Exception:
            healthy = False
        return {
            "status": "ok" if healthy else "unavailable",
            **self.stats(),
        }

    def stats(self) -> dict[str, Any]:
        return {
            "min_size": self.config.min_size,
            "max_size": self.config.max_size,
            "size": self._created,
            "idle": self._available.qsize(),
            # Checked-out connections aren't queued, so in_use is derived from the difference.
            "in_use": max(0, self._created - self._available.qsize()),
            "timeouts": self._timeouts,
            "discarded": self._discarded,
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        connections: list[Any] = []
        while not self._available.empty():
            connections.append(self._available.get_nowait())
        connections.extend(self._all)
        # Dedupe by identity: drained queue entries also appear in _all.
        unique: list[Any] = []
        for connection in connections:
            if not any(connection is existing for existing in unique):
                unique.append(connection)
        for connection in unique:
            await self._close_connection(connection)

    @staticmethod
    async def _default_close(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result
