"""Dependency-free counters and retry helpers for exporters."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class ReliabilityMetrics:
    # Lightweight dependency-free counters shared by exporters/reliability wrappers.
    dropped: int = 0
    success: int = 0
    failure: int = 0
    retry: int = 0
    latency_count: int = 0
    latency_ms_total: float = 0.0
    latency_ms_max: float = 0.0

    def record_success(self, latency_ms: float = 0.0) -> None:
        self.success += 1
        self._record_latency(latency_ms)

    def record_failure(self, latency_ms: float = 0.0) -> None:
        self.failure += 1
        self._record_latency(latency_ms)

    def record_retry(self) -> None:
        self.retry += 1

    def record_dropped(self, count: int = 1) -> None:
        self.dropped += max(0, int(count))

    def _record_latency(self, latency_ms: float) -> None:
        latency = max(0.0, float(latency_ms))
        self.latency_count += 1
        self.latency_ms_total += latency
        self.latency_ms_max = max(self.latency_ms_max, latency)

    @property
    def latency_ms_average(self) -> float:
        if self.latency_count == 0:
            return 0.0
        return self.latency_ms_total / self.latency_count

    def snapshot(self) -> dict[str, int | float]:
        return {
            "dropped": self.dropped,
            "success": self.success,
            "failure": self.failure,
            "retry": self.retry,
            "latency_count": self.latency_count,
            "latency_ms_total": self.latency_ms_total,
            "latency_ms_average": self.latency_ms_average,
            "latency_ms_max": self.latency_ms_max,
        }


class ExportMetrics(ReliabilityMetrics):
    """Compatibility name for exporter-focused metrics."""


def bounded_backoff(
    attempt: int,
    base: float = 0.1,
    maximum: float = 5.0,
) -> float:
    """Return bounded exponential backoff for a zero-based attempt."""
    if attempt < 0:
        return 0.0
    # Cap exponent to avoid float overflow while still enforcing max delay.
    value = float(base) * (2.0 ** min(int(attempt), 30))
    return min(max(0.0, float(maximum)), value)


class BoundedBatchQueue:
    """Thread-safe FIFO with an upper bound."""

    def __init__(self, max_items: int = 1000) -> None:
        self.max_items = max(0, int(max_items))
        self._items: list[Any] = []
        # Lock guards list mutations for producers/consumers on different threads.
        self._lock = threading.Lock()

    def put(self, item: Any) -> bool:
        with self._lock:
            # Non-blocking bounded put: caller decides how to handle backpressure on False.
            if self.max_items > 0 and len(self._items) >= self.max_items:
                return False
            self._items.append(item)
            return True

    def drain(self, limit: int | None = None) -> list[Any]:
        with self._lock:
            count = len(self._items) if limit is None else max(0, int(limit))
            count = min(count, len(self._items))
            items = self._items[:count]
            del self._items[:count]
            return items

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)
