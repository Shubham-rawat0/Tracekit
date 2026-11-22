"""Local WebSocket server for streamed Tracekit events."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from collections.abc import Mapping
from typing import Any

from .manager import StreamManager


class StreamServer:
    def __init__(
        self,
        manager: StreamManager,
        host: str = "127.0.0.1",
        port: int = 8765,
        max_queue: int = 100,
    ) -> None:
        if not isinstance(host, str):
            raise TypeError("host must be a string")
        if int(port) < 0 or int(port) > 65535:
            raise ValueError("port must be between 0 and 65535")
        if int(max_queue) < 1:
            raise ValueError("max_queue must be at least 1")
        self.manager = manager
        self.host = host
        self.port = int(port)
        self.max_queue = int(max_queue)
        self._websockets: Any = None
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribed = False
        self._closed = False
        self._client_queues: set[asyncio.Queue[str]] = set()
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._writer_tasks: set[asyncio.Task[Any]] = set()
        self._actual_port = self.port

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self._actual_port}"

    @property
    def client_count(self) -> int:
        return len(self._client_queues)

    async def start(self) -> None:
        if self._server is not None:
            return
        # Import websockets lazily so the base SDK install has no WebSocket dependency.
        try:
            websockets = importlib.import_module("websockets")
        except ImportError as exc:
            raise RuntimeError(
                "WebSocket streaming requires websockets; "
                "install tracekit[streaming]"
            ) from exc
        self._websockets = websockets
        self._loop = asyncio.get_running_loop()
        self._closed = False

        handler = getattr(websockets, "serve", None)
        if not callable(handler):
            raise RuntimeError(
                "WebSocket streaming requires a compatible websockets package"
            )
        result = handler(self._client_handler, self.host, self.port)
        if inspect.isawaitable(result):
            result = await result
        self._server = result
        sockets = getattr(result, "sockets", None)
        if sockets:
            try:
                address = sockets[0].getsockname()
                # Port 0 asks the OS to pick a free port; url must report the bound one.
                self._actual_port = int(address[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                pass
        self.manager.subscribe(self._on_event)
        self._subscribed = True

    def _on_event(self, event: Mapping[str, Any]) -> None:
        if self._closed or self._loop is None:
            return
        if not isinstance(event, Mapping):
            return
        try:
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                return
            # Serialize at the boundary: unserializable events are dropped, not allowed to crash fan-out.
            snapshot = json.dumps(dict(event), default=str)
        except (TypeError, ValueError, OverflowError):
            return
        if self._loop.is_closed():
            return
        # StreamManager invokes callbacks on worker threads, so queue delivery must hop to the loop.
        try:
            self._loop.call_soon_threadsafe(self._enqueue, snapshot)
        except RuntimeError:
            return

    def _enqueue(self, payload: str) -> None:
        if self._closed:
            return
        for client_queue in list(self._client_queues):
            try:
                client_queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow clients lose their oldest event; broadcast never blocks on one consumer.
                try:
                    client_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    client_queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    async def _writer(
        self,
        websocket: Any,
        client_queue: asyncio.Queue[str],
    ) -> None:
        try:
            while not self._closed:
                payload = await client_queue.get()
                try:
                    await websocket.send(payload)
                except Exception:
                    # A dead socket ends only this writer; the handler cleans up its queue and tasks.
                    return
                finally:
                    client_queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _client_handler(self, websocket: Any, *args: Any) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._client_tasks.add(current)
        client_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.max_queue)
        self._client_queues.add(client_queue)
        writer = asyncio.create_task(self._writer(websocket, client_queue))
        self._writer_tasks.add(writer)
        try:
            recv = getattr(websocket, "recv", None)
            if callable(recv):
                # Incoming messages are ignored; recv only detects client disconnects.
                while not self._closed:
                    await recv()
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):
                pass
            self._writer_tasks.discard(writer)
            self._client_queues.discard(client_queue)
            if current is not None:
                self._client_tasks.discard(current)

    async def stop(self) -> None:
        self._closed = True
        # Unsubscribe first so in-flight manager callbacks cannot enqueue after close.
        if self._subscribed:
            self.manager.unsubscribe(self._on_event)
            self._subscribed = False
        for task in list(self._writer_tasks):
            task.cancel()
        for task in list(self._client_tasks):
            task.cancel()
        client_tasks = list(self._client_tasks)
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        if self._writer_tasks:
            await asyncio.gather(*self._writer_tasks, return_exceptions=True)
        self._writer_tasks.clear()
        self._client_queues.clear()
        if self._server is not None:
            close = getattr(self._server, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
            wait_closed = getattr(self._server, "wait_closed", None)
            if callable(wait_closed):
                result = wait_closed()
                if inspect.isawaitable(result):
                    await result
        self._server = None
