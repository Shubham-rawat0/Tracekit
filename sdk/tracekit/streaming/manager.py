"""Thread-safe fan-out delivery for Tracekit streaming events."""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, cast

Subscriber = Callable[[dict[str, Any]], Any]


class StreamManager:
    def __init__(self, max_queue: int = 1000) -> None:
        if int(max_queue) < 1:
            raise ValueError("max_queue must be at least 1")
        self.max_queue = int(max_queue)
        self._subscribers: dict[
            Subscriber,
            tuple[queue.Queue[dict[str, Any] | None], threading.Thread],
        ] = {}
        self._lock = threading.RLock()
        self._closed = False

    def subscribe(self, callback: Subscriber) -> Subscriber:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("StreamManager is closed")
            # Duplicate subscriptions are no-ops, so one callback gets only one worker.
            if callback in self._subscribers:
                return callback
            event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
                maxsize=self.max_queue
            )
            worker = threading.Thread(
                target=self._worker,
                args=(callback, event_queue),
                name="tracekit-stream-subscriber",
                daemon=True,
            )
            self._subscribers[callback] = (event_queue, worker)
            worker.start()
        return callback

    def unsubscribe(self, callback: Subscriber) -> None:
        with self._lock:
            entry = self._subscribers.pop(callback, None)
            if entry is None:
                return
            event_queue, worker = entry
            # None is the worker's stop sentinel; evict one event if the queue is full.
            try:
                event_queue.put_nowait(None)
            except queue.Full:
                try:
                    event_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    event_queue.put_nowait(None)
                except queue.Full:
                    pass
        worker.join(timeout=1.0)

    def publish(self, event: Mapping[str, Any]) -> bool:
        if not isinstance(event, Mapping):
            raise ValueError("event must be a mapping")
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event.type must be a non-empty string")
        # Snapshot before locking: callers may reuse or mutate their dict after publish returns.
        try:
            snapshot = deepcopy(dict(event))
        except Exception as exc:
            raise ValueError("event must be serializable data") from exc

        with self._lock:
            if self._closed:
                return False
            entries = list(self._subscribers.items())

        published = False
        for callback, (event_queue, _worker) in entries:
            del callback
            try:
                # Per-subscriber copies prevent one callback from mutating another's event.
                event_queue.put_nowait(deepcopy(snapshot))
                published = True
            except queue.Full:
                # Drop the oldest event for a slow subscriber instead of blocking every publisher.
                try:
                    event_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    event_queue.put_nowait(deepcopy(snapshot))
                    published = True
                except queue.Full:
                    pass
        return published

    @staticmethod
    def _invoke_callback(callback: Subscriber, event: dict[str, Any]) -> None:
        # Coroutine callbacks are driven to completion on the worker thread via asyncio.run.
        if inspect.iscoroutinefunction(callback):
            coroutine = cast(Any, callback(event))
            asyncio.run(cast(Any, coroutine))
            return
        result = callback(event)
        if inspect.isawaitable(result):
            asyncio.run(cast(Any, result))

    def _worker(
        self,
        callback: Subscriber,
        event_queue: queue.Queue[dict[str, Any] | None],
    ) -> None:
        while True:
            item = event_queue.get()
            try:
                if item is None:
                    return
                try:
                    self._invoke_callback(callback, item)
                except Exception:
                    # One faulty subscriber must not kill fan-out for the others.
                    continue
            finally:
                event_queue.task_done()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callbacks = list(self._subscribers)
        for callback in callbacks:
            self.unsubscribe(callback)
