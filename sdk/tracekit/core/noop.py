"""Non-recording span implementation for dropped or disabled traces."""

from __future__ import annotations

from typing import Any

from tracekit.core.event import Event
from tracekit.core.span import Span


class NoOpSpan(Span):
    """A Span-compatible object whose mutating recording methods are no-ops."""

    # Lets instrumented code stay branch-free when tracing is off or a trace is dropped.
    @property
    def is_recording(self) -> bool:
        return False

    def start(self) -> "NoOpSpan":
        # Fluent methods return self so callers can chain them even when nothing is recorded.
        return self

    def end(
        self,
        status: str | None = None,
        error: str | None = None,
        output: Any = None,
    ) -> "NoOpSpan":
        return self

    def add_event(
        self,
        event_or_name: Event | str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        # Mutators discard inputs without allocating event objects or storing attributes.
        return None

    def record_exception(
        self,
        exc: BaseException,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        return None

    def set_attribute(self, key: str, value: Any) -> None:
        return None
