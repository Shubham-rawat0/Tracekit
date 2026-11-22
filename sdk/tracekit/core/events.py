"""Event policies and factories for structured, privacy-safe tracing events."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tracekit.core.event import Event

LLM_TOKEN_CHUNK = "llm.token_chunk"
TOOL_RETRY = "tool.retry"
DB_QUERY_START = "db.query.start"
DB_QUERY_COMPLETE = "db.query.complete"
CACHE_HIT = "cache.hit"
CACHE_MISS = "cache.miss"
EXCEPTION = "exception"

# Payload capture is opt-in, so common secret-bearing attribute names are dropped by default.
_SENSITIVE_ATTRIBUTE_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "credential",
        "content",
        "data",
        "input",
        "output",
        "password",
        "payload",
        "prompt",
        "query",
        "raw",
        "secret",
        "sql",
        "text",
        "token",
        "value",
    }
)


@dataclass(frozen=True)
class EventPolicy:
    max_events: int = 100
    max_attribute_length: int = 256
    capture_payloads: bool = False
    include_exception_message: bool = False
    clock: Callable[[], datetime] | None = None

    def __post_init__(self) -> None:
        if self.max_events < 0:
            raise ValueError("max_events must be non-negative")
        if self.max_attribute_length <= 0:
            raise ValueError("max_attribute_length must be positive")

    def utcnow(self) -> datetime:
        value = self.clock() if self.clock is not None else datetime.now(timezone.utc)
        # Naive datetimes are assumed UTC so event ordering stays comparable.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def clean_value(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > self.max_attribute_length:
            return value[: self.max_attribute_length] + "..."
        return value

    def safe_attributes(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in (attributes or {}).items():
            name = str(key)
            tail = name.rsplit(".", 1)[-1].lower()
            if not self.capture_payloads and tail in _SENSITIVE_ATTRIBUTE_NAMES:
                continue
            cleaned[name] = self.clean_value(value)
        return cleaned


DEFAULT_EVENT_POLICY = EventPolicy()


def _timestamp(value: datetime | None, policy: EventPolicy) -> datetime:
    if value is None:
        return policy.utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_event(
    name: str,
    *,
    attributes: Mapping[str, Any] | None,
    trace_id: str | None,
    span_id: str | None,
    policy: EventPolicy | None,
    timestamp: datetime | None,
    extra: Mapping[str, Any] | None,
) -> Event:
    effective = policy or DEFAULT_EVENT_POLICY
    merged = effective.safe_attributes(attributes)
    # Caller kwargs override base fields after passing through the same privacy filter.
    merged.update(effective.safe_attributes(extra))
    return Event(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        timestamp=_timestamp(timestamp, effective),
        attributes=merged,
    )


def llm_token_chunk_event(
    chunk_index: int,
    *,
    token_count: int | None = None,
    model: str | None = None,
    is_final: bool = False,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy: EventPolicy | None = None,
    timestamp: datetime | None = None,
    **attributes: Any,
) -> Event:
    base: dict[str, Any] = {
        "llm.chunk_index": int(chunk_index),
        "llm.is_final": bool(is_final),
    }
    if token_count is not None:
        base["llm.token_count"] = int(token_count)
    if model is not None:
        base["llm.model"] = str(model)
    return _build_event(
        LLM_TOKEN_CHUNK,
        attributes=base,
        trace_id=trace_id,
        span_id=span_id,
        policy=policy,
        timestamp=timestamp,
        extra=attributes,
    )


def tool_retry_event(
    attempt: int,
    *,
    tool_name: str | None = None,
    reason: str | None = None,
    next_delay_ms: float | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy: EventPolicy | None = None,
    timestamp: datetime | None = None,
    **attributes: Any,
) -> Event:
    base: dict[str, Any] = {"tool.attempt": int(attempt)}
    if tool_name is not None:
        base["tool.name"] = str(tool_name)
    if reason is not None:
        base["tool.retry_reason"] = str(reason)
    if next_delay_ms is not None:
        base["tool.next_delay_ms"] = float(next_delay_ms)
    return _build_event(
        TOOL_RETRY,
        attributes=base,
        trace_id=trace_id,
        span_id=span_id,
        policy=policy,
        timestamp=timestamp,
        extra=attributes,
    )


def db_query_start_event(
    query_id: str,
    *,
    operation: str | None = None,
    table: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy: EventPolicy | None = None,
    timestamp: datetime | None = None,
    **attributes: Any,
) -> Event:
    base: dict[str, Any] = {"db.query_id": str(query_id)}
    if operation is not None:
        base["db.operation"] = str(operation)
    if table is not None:
        base["db.table"] = str(table)
    return _build_event(
        DB_QUERY_START,
        attributes=base,
        trace_id=trace_id,
        span_id=span_id,
        policy=policy,
        timestamp=timestamp,
        extra=attributes,
    )


def db_query_complete_event(
    query_id: str,
    *,
    duration_ms: float | None = None,
    row_count: int | None = None,
    success: bool = True,
    error_type: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy: EventPolicy | None = None,
    timestamp: datetime | None = None,
    **attributes: Any,
) -> Event:
    base: dict[str, Any] = {
        "db.query_id": str(query_id),
        "db.success": bool(success),
    }
    if duration_ms is not None:
        base["db.duration_ms"] = float(duration_ms)
    if row_count is not None:
        base["db.row_count"] = int(row_count)
    if error_type is not None:
        base["db.error_type"] = str(error_type)
    return _build_event(
        DB_QUERY_COMPLETE,
        attributes=base,
        trace_id=trace_id,
        span_id=span_id,
        policy=policy,
        timestamp=timestamp,
        extra=attributes,
    )


def cache_event(
    hit: bool,
    *,
    cache_name: str | None = None,
    key_prefix: str | None = None,
    duration_ms: float | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy: EventPolicy | None = None,
    timestamp: datetime | None = None,
    **attributes: Any,
) -> Event:
    base: dict[str, Any] = {"cache.hit": bool(hit)}
    if cache_name is not None:
        base["cache.name"] = str(cache_name)
    if key_prefix is not None:
        base["cache.key_prefix"] = str(key_prefix)
    if duration_ms is not None:
        base["cache.duration_ms"] = float(duration_ms)
    return _build_event(
        CACHE_HIT if hit else CACHE_MISS,
        attributes=base,
        trace_id=trace_id,
        span_id=span_id,
        policy=policy,
        timestamp=timestamp,
        extra=attributes,
    )


def cache_hit_event(**kwargs: Any) -> Event:
    return cache_event(True, **kwargs)


def cache_miss_event(**kwargs: Any) -> Event:
    return cache_event(False, **kwargs)


def exception_event(
    exc: BaseException,
    *,
    context: Mapping[str, Any] | None = None,
    include_message: bool | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    policy: EventPolicy | None = None,
    timestamp: datetime | None = None,
    **attributes: Any,
) -> Event:
    effective = policy or DEFAULT_EVENT_POLICY
    base: dict[str, Any] = {"exception.type": type(exc).__name__}
    # Exception text is opt-in because it often contains sensitive data.
    show_message = (
        effective.include_exception_message
        if include_message is None
        else bool(include_message)
    )
    if show_message:
        base["exception.message"] = str(exc)
    if context:
        base.update(dict(context))
    return _build_event(
        EXCEPTION,
        attributes=base,
        trace_id=trace_id,
        span_id=span_id,
        policy=policy,
        timestamp=timestamp,
        extra=attributes,
    )


def _item_sequence(item: Any) -> int | None:
    if isinstance(item, Event):
        source = item.attributes
    elif isinstance(item, Mapping):
        source = item
    else:
        return None
    value = source.get("event.seq")
    # bool is an int subclass but makes no sense as a sequence number.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _next_sequence(items: Iterable[Any]) -> int:
    seen = [_item_sequence(item) for item in items]
    return max((value for value in seen if value is not None), default=-1) + 1


def _assign_sequence(event: Event, items: Iterable[Any]) -> Event:
    # Preserve caller-supplied sequence numbers so merged events keep their order.
    if _item_sequence(event) is None:
        event.attributes["event.seq"] = _next_sequence(items)
    return event


def _trim(items: list[Any], limit: int) -> None:
    while len(items) > limit:
        items.pop(0)


def _event_order(event: Event) -> tuple[float, int]:
    timestamp = event.timestamp
    try:
        # Out-of-range timestamps fall back to 0 so sorting never raises.
        moment = float(timestamp.timestamp()) if isinstance(timestamp, datetime) else 0.0
    except (OverflowError, OSError, ValueError):
        moment = 0.0
    return moment, _item_sequence(event) or 0


class EventCollector:
    """Bounded, ordered collection of events with stable sequence numbers."""

    def __init__(self, policy: EventPolicy | None = None) -> None:
        self.policy = policy or DEFAULT_EVENT_POLICY
        self._events: list[Event] = []

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def add(self, event: Event) -> Event:
        _assign_sequence(event, self._events)
        self._events.append(event)
        _trim(self._events, self.policy.max_events)
        return event

    def extend(self, events: Iterable[Event]) -> list[Event]:
        return [self.add(event) for event in events]

    def ordered(self) -> list[Event]:
        return sorted(self._events, key=_event_order)

    def clear(self) -> None:
        self._events.clear()

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)


def record_span_event(span: Any, event: Event, *, policy: EventPolicy | None = None) -> Event:
    effective = policy or DEFAULT_EVENT_POLICY
    events = getattr(span, "events", None)
    # Tolerate duck-typed targets that never initialized an events list.
    if events is None:
        events = []
        setattr(span, "events", events)
    _assign_sequence(event, events)
    add_event = getattr(span, "add_event", None)
    if callable(add_event):
        add_event(event)
    else:
        events.append(event)
    _trim(events, effective.max_events)
    return event


def record_trace_event(trace: Any, event: Event, *, policy: EventPolicy | None = None) -> Event:
    effective = policy or DEFAULT_EVENT_POLICY
    events = trace.metadata.setdefault("events", [])
    _assign_sequence(event, events)
    events.append(event.to_dict())
    _trim(events, effective.max_events)
    return event


def record_event(target: Any, event: Event, *, policy: EventPolicy | None = None) -> Event:
    # Traces expose metadata; everything else is treated as a span.
    if hasattr(target, "metadata"):
        return record_trace_event(target, event, policy=policy)
    return record_span_event(target, event, policy=policy)


def record_exception(
    target: Any,
    exc: BaseException,
    *,
    policy: EventPolicy | None = None,
    **kwargs: Any,
) -> Event:
    event = exception_event(exc, policy=policy, **kwargs)
    return record_event(target, event, policy=policy)
