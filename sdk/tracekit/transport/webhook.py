"""Non-blocking webhook transport with bounded delivery and retries."""

from __future__ import annotations

import asyncio
import importlib
import queue
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any


class WebhookTransport:
    def __init__(
        self,
        endpoint: str,
        max_queue: int = 100,
        timeout: float = 5.0,
        max_retries: int = 2,
        transport: Callable[[str, dict[str, str], dict[str, Any]], Any] | None = None,
        http_client: Any = None,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a non-empty string")
        if int(max_queue) < 1:
            raise ValueError("max_queue must be at least 1")
        if float(timeout) <= 0:
            raise ValueError("timeout must be positive")
        if int(max_retries) < 0:
            raise ValueError("max_retries must be non-negative")
        self.endpoint = endpoint
        self.max_queue = int(max_queue)
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self._transport = transport
        self._http_client = http_client
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=self.max_queue
        )
        self._worker: threading.Thread | None = None
        self._started = False
        self._stopping = False
        self._lock = threading.Lock()
        self.dropped = 0
        self.successes = 0
        self.failures = 0
        self.retries = 0

    async def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._transport is None and self._http_client is None:
                # httpx is optional; injected transports or clients bypass the dependency.
                try:
                    httpx = importlib.import_module("httpx")
                except ImportError as exc:
                    raise RuntimeError(
                        "Webhook transport requires httpx; "
                        "install tracekit[webhook]"
                    ) from exc
                self._http_client = httpx.Client(timeout=self.timeout)
            self._started = True
            self._stopping = False
            # Delivery runs on a daemon thread so publish() never blocks on the network.
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="tracekit-webhook",
                daemon=True,
            )
            self._worker.start()

    async def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._stopping = True
        # None is the stop sentinel; evict one event if the bounded queue is full.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        worker = self._worker
        if worker is not None:
            await asyncio.to_thread(worker.join)
        client = self._http_client
        close = getattr(client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
        with self._lock:
            self._started = False

    def publish(self, event: Mapping[str, Any]) -> bool:
        if not isinstance(event, Mapping):
            return False
        if not isinstance(event.get("type"), str):
            return False
        if self._stopping:
            return False
        snapshot = deepcopy(dict(event))
        try:
            self._queue.put_nowait(snapshot)
            return True
        except queue.Full:
            # Drop the oldest event to keep memory bounded instead of blocking the caller.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self.dropped += 1
            try:
                self._queue.put_nowait(snapshot)
                return True
            except queue.Full:
                with self._lock:
                    self.dropped += 1
                return False
        finally:
            pass

    def _send_once(self, event: dict[str, Any]) -> bool:
        headers = {"Content-Type": "application/json"}
        if self._transport is not None:
            try:
                result = self._transport(self.endpoint, headers, event)
                # Injected transports can signal success as a bool or as an HTTP status.
                if isinstance(result, bool):
                    return result
                if isinstance(result, int):
                    return 200 <= result < 300
                return False
            except Exception:
                return False
        client = self._http_client
        if client is None:
            return False
        try:
            response = client.post(
                self.endpoint,
                headers=headers,
                json=event,
                timeout=self.timeout,
            )
            status = getattr(response, "status_code", None)
            return isinstance(status, int) and 200 <= status < 300
        except Exception:
            return False

    def _worker_loop(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                # max_retries counts retries after the initial attempt.
                attempts = self.max_retries + 1
                delivered = False
                for attempt in range(attempts):
                    if self._send_once(event):
                        delivered = True
                        # Counters are read by callers on other threads, so guard every update.
                        with self._lock:
                            self.successes += 1
                        break
                    if attempt + 1 < attempts:
                        with self._lock:
                            self.retries += 1
                        # Exponential backoff is capped so a failing endpoint cannot stall the worker for long.
                        time.sleep(min(5.0, 0.1 * (2.0 ** attempt)))
                if not delivered:
                    with self._lock:
                        self.failures += 1
            finally:
                self._queue.task_done()
