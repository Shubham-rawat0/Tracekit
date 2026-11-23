"""Span model with lifecycle support."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from tracekit.core.event import Event


class SpanType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    CHAIN = "chain"
    RETRIEVER = "retriever"
    EMBEDDING = "embedding"
    AGENT = "agent"
    INTERNAL = "internal"
    DATABASE = "database"
    HTTP = "http"

    def __str__(self) -> str:
        return self.value


def normalize_span_type(value: "SpanType | str | None") -> SpanType:
    if isinstance(value, SpanType):
        return value
    if value is None:
        return SpanType.INTERNAL
    if isinstance(value, str):
        try:
            return SpanType(value.strip().lower())
        except ValueError:
            valid = ", ".join(t.value for t in SpanType)
            raise ValueError(
                f"invalid span_type {value!r}; expected one of: {valid}"
            ) from None
    raise TypeError(f"span_type must be a string or SpanType, got {type(value)!r}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    # W3C trace-context requires 16 hex chars for span ids.
    return uuid.uuid4().hex[:16]


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str = field(default_factory=_new_id)
    parent_span_id: str | None = None
    span_type: SpanType = SpanType.INTERNAL
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: str = "running"
    input: Any = None
    output: Any = None
    error: str | None = None
    error_type: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    project_id: str | None = None
    environment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_source: str | None = None
    is_final: bool = False
    handled: bool = False

    def __post_init__(self) -> None:
        self.span_type = normalize_span_type(self.span_type)

    # compatibility with existing partial class signature
    @property
    def type(self) -> SpanType:
        return self.span_type

    @type.setter
    def type(self, value: "SpanType | str") -> None:
        self.span_type = normalize_span_type(value)

    @property
    def duration_ms(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds() * 1000
        return None

    def start(self) -> "Span":
        # Restarting a finished span clears stale end/error state.
        if self.end_time is not None:
            self.end_time = None
            self.error = None
        if self.start_time is None:
            self.start_time = _utcnow()
        self.status = "running"
        return self

    def end(self, status: str | None = None, error: str | None = None, output: Any = None) -> "Span":
        if self.end_time is None:
            self.end_time = _utcnow()
        if output is not None:
            self.output = output
        if error is not None:
            self.error = error
            self.status = "error"
        elif status is not None:
            self.status = status
        elif self.status == "running":
            # Default: set to "ok" unless an error was recorded via record_exception()
            self.status = "ok"
        return self

    def add_event(
        self,
        event_or_name: Event | str,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        if isinstance(event_or_name, str):
            event = Event(
                name=event_or_name,
                trace_id=self.trace_id,
                span_id=self.span_id,
                attributes=dict(attributes or {}),
            )
        elif isinstance(event_or_name, Event):
            event = event_or_name
            # Events built elsewhere inherit this span's ids when they were left unset.
            if event.trace_id is None:
                event.trace_id = self.trace_id
            if event.span_id is None:
                event.span_id = self.span_id
        else:
            raise TypeError("event must be an Event or a string name")
        self.events.append(event)
        return event

    def record_exception(
        self,
        exc: BaseException,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        import traceback
        event_attributes = dict(attributes or {})
        event_attributes.update(
            {
                "exception.type": type(exc).__name__,
                "exception.message": str(exc),
                "exception.stacktrace": traceback.format_exc(),
                "exception.timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        event = self.add_event("exception", event_attributes)
        self.status = "error"
        if self.error is None:
            self.error = str(exc)
        self.error_type = type(exc).__name__
        return event

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for k in ("start_time", "end_time"):
            v = data.get(k)
            if isinstance(v, datetime):
                data[k] = v.isoformat()
        # Duration is derived at serialization time so it can never go stale.
        data["duration_ms"] = self.duration_ms
        data["span_type"] = SpanType(self.span_type).value
        data["events"] = [
            e.to_dict() if isinstance(e, Event) else e
            for e in self.events
        ]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Span":
        d = dict(data)
        # Drop derived fields before constructing the dataclass.
        d.pop("duration_ms", None)
        # Accept the legacy "type" key only when "span_type" is absent.
        if "type" in d and "span_type" not in d:
            d["span_type"] = d.pop("type")
        else:
            d.pop("type", None)
        for k in ("start_time", "end_time"):
            v = d.get(k)
            if isinstance(v, str):
                d[k] = datetime.fromisoformat(v)
        if isinstance(d.get("events"), list):
            d["events"] = [
                Event.from_dict(event) if isinstance(event, dict) else event
                for event in d["events"]
            ]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# Backwards-compatible alias for existing partial class name
span = Span
