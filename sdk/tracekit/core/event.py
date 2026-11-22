"""Event model for trace/span events."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    # Event ids are full UUIDs; W3C fixed-width ids apply only to traces and spans.
    return uuid.uuid4().hex


@dataclass
class Event:
    name: str
    event_id: str = field(default_factory=_new_id)
    trace_id: str | None = None
    span_id: str | None = None
    timestamp: datetime = field(default_factory=_utcnow)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        ts = data.get("timestamp")
        if isinstance(ts, datetime):
            data["timestamp"] = ts.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        d = dict(data)
        ts = d.get("timestamp")
        if isinstance(ts, str):
            d["timestamp"] = datetime.fromisoformat(ts)
        return cls(**d)
