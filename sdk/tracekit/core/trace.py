"""Trace model and trace context helpers."""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from tracekit.core.context import (
    get_trace_context,
    reset_trace_context,
    set_trace_context,
)
from tracekit.core.event import Event


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


logger = logging.getLogger(__name__)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def warn_on_hierarchy_violations(spans: list[dict[str, Any]]) -> None:
    by_id: dict[str, dict[str, Any]] = {}
    for span in spans:
        span_id = span.get("span_id")
        if span_id is None:
            continue
        key = str(span_id)
        if key in by_id:
            logger.warning("Duplicate span_id %s in trace %s", key, span.get("trace_id"))
        by_id[key] = span

    for span in spans:
        span_id = str(span.get("span_id") or "")
        parent_id = span.get("parent_span_id")
        if parent_id is None or parent_id == "":
            continue
        parent_key = str(parent_id)
        parent = by_id.get(parent_key)
        if parent is None:
            logger.warning(
                "Span %s (%s) references missing parent %s in trace %s",
                span_id,
                span.get("name"),
                parent_key,
                span.get("trace_id"),
            )
            continue
        if str(parent.get("trace_id") or "") != str(span.get("trace_id") or ""):
            logger.warning(
                "Span %s (%s) parent %s belongs to a different trace",
                span_id,
                span.get("name"),
                parent_key,
            )
        start = _parse_time(span.get("start_time"))
        end = _parse_time(span.get("end_time"))
        parent_start = _parse_time(parent.get("start_time"))
        parent_end = _parse_time(parent.get("end_time"))
        if (
            start is not None
            and end is not None
            and parent_start is not None
            and parent_end is not None
            and (start < parent_start or end > parent_end)
        ):
            logger.warning(
                "Span %s (%s) is not contained within parent %s",
                span_id,
                span.get("name"),
                parent_key,
            )


def _get_trace_context() -> dict[str, Any] | None:
    """Compatibility helper used by existing code."""
    return get_trace_context()


@dataclass
class Trace:
    name: str = "trace"
    trace_id: str = field(default_factory=_new_id)
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: str = "running"
    attributes: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    environment: str | None = None
    prompt: str | None = None
    response: str | None = None
    token_count: int | None = None
    model_name: str | None = None
    error_type: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_source: str | None = None

    def start(self) -> "Trace":
        if self.end_time is not None:
            self.end_time = None
            self.error = None
        if self.start_time is None:
            self.start_time = _utcnow()
        self.status = "running"
        # Avoid stacking context tokens if start() is called more than once.
        if getattr(self, "_tracekit_context_token", None) is None:
            baggage = {
                "trace_id": self.trace_id,
                "name": self.name,
                "attributes": self.attributes,
            }
            object.__setattr__(self, "_tracekit_context_token", set_trace_context(baggage))
        return self

    def end(self, status: str = "ok", error: str | None = None) -> "Trace":
        if self.end_time is None:
            self.end_time = _utcnow()
        if error is not None:
            self.error = error
            self.status = "error"
        else:
            self.status = status
        token = getattr(self, "_tracekit_context_token", None)
        # Context cleanup is best-effort; the token may already be stale in another scope.
        if token is not None:
            try:
                reset_trace_context(token)
            except Exception:
                pass
            try:
                delattr(self, "_tracekit_context_token")
            except Exception:
                pass
        return self

    def add_event(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(
            name=name,
            trace_id=self.trace_id,
            span_id=None,
            attributes=dict(attributes or {}),
        )
        # Serialize immediately because trace metadata is persisted as JSON.
        self.metadata.setdefault("events", []).append(event.to_dict())
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
        # Mirror the error into metadata so persisted traces expose the failure reason.
        self.metadata["failure_reason"] = str(exc)
        self.error_type = type(exc).__name__
        return event

    @property
    def duration_ms(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds() * 1000
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for k in ("start_time", "end_time"):
            v = data.get(k)
            if isinstance(v, datetime):
                data[k] = v.isoformat()
        data["duration_ms"] = self.duration_ms
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trace":
        d = dict(data)
        d.pop("duration_ms", None)
        for k in ("start_time", "end_time"):
            v = d.get(k)
            if isinstance(v, str):
                d[k] = datetime.fromisoformat(v)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
