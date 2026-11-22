"""Sampling strategies for traces and spans."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Any


class Sampler(ABC):
    """Decide whether a trace should be recorded."""

    @abstractmethod
    def should_sample(
        self,
        trace_id: str,
        name: str | None = None,
        parent_sampled: bool | None = None,
    ) -> bool:
        raise NotImplementedError


class AlwaysOnSampler(Sampler):
    def should_sample(
        self,
        trace_id: str,
        name: str | None = None,
        parent_sampled: bool | None = None,
    ) -> bool:
        return True


class AlwaysOffSampler(Sampler):
    def should_sample(
        self,
        trace_id: str,
        name: str | None = None,
        parent_sampled: bool | None = None,
    ) -> bool:
        return False


class RateLimitSampler(Sampler):
    """Deterministic ratio sampler.

    ``rate`` is the fraction of traces to sample (0.0 - 1.0). The decision is
    derived from a hash of the trace id, so the same trace is always sampled or
    dropped consistently across services.
    """

    def __init__(self, rate: float = 1.0, salt: str = "") -> None:
        rate = float(rate)
        if not 0.0 <= rate <= 1.0:
            raise ValueError("rate must be between 0.0 and 1.0")
        self.rate = rate
        self.salt = salt

    def should_sample(
        self,
        trace_id: str,
        name: str | None = None,
        parent_sampled: bool | None = None,
    ) -> bool:
        # Parent context is ignored here; ParentBasedSampler is responsible for honoring it.
        if self.rate <= 0.0:
            return False
        if self.rate >= 1.0:
            return True
        digest = hashlib.sha256(f"{self.salt}:{trace_id}".encode("utf-8")).hexdigest()
        # Only the leading 64 bits of the digest participate in the ratio comparison.
        value = int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        return value < self.rate


class TraceIdRatioSampler(RateLimitSampler):
    """Deterministic trace-ID ratio sampler with a default ten-percent rate."""

    def __init__(self, rate: float = 0.1) -> None:
        super().__init__(rate=rate)


# Common aliases used by other tracing libraries.
RateLimitedSampler = RateLimitSampler
RateLimitingSampler = RateLimitSampler


class ParentBasedSampler(Sampler):
    """Honor an upstream sampling decision, otherwise defer to ``root``."""

    def __init__(self, root: Sampler | None = None) -> None:
        self.root = root or AlwaysOnSampler()

    def should_sample(
        self,
        trace_id: str,
        name: str | None = None,
        parent_sampled: bool | None = None,
    ) -> bool:
        if parent_sampled is not None:
            return bool(parent_sampled)
        return self.root.should_sample(
            trace_id=trace_id, name=name, parent_sampled=parent_sampled
        )


def sampler_from_value(value: Any) -> Sampler:
    """Coerce a few convenient representations into a ``Sampler``."""

    if isinstance(value, Sampler):
        return value
    # None and True both mean "sample everything" for convenience.
    if value is True or value is None:
        return AlwaysOnSampler()
    if value is False:
        return AlwaysOffSampler()
    if isinstance(value, (int, float)):
        return RateLimitSampler(float(value))
    raise TypeError(f"cannot convert {value!r} to a Sampler")
