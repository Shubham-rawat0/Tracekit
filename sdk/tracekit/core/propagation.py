"""W3C Trace Context propagation helpers."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from tracekit.core.context import get_current_span, get_current_trace, get_trace_context

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"


@dataclass(frozen=True, init=False)
class TraceContext:
    """Trace and parent-span identity received from or sent to another service."""

    trace_id: str
    span_id: str
    sampled: bool
    trace_state: str | None

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        sampled: bool = True,
        trace_state: str | None = None,
        *,
        tracestate: str | None = None,
    ) -> None:
        # Normalize the legacy spelling here because the frozen dataclass has a custom init.
        if trace_state is None:
            trace_state = tracestate
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "span_id", span_id)
        object.__setattr__(self, "sampled", sampled)
        object.__setattr__(self, "trace_state", trace_state)

    @property
    def tracestate(self) -> str | None:
        """Compatibility spelling used by existing callers."""
        return self.trace_state


# Existing code uses SpanContext for the same incoming remote context.
SpanContext = TraceContext


def _is_hex(value: Any, length: int) -> bool:
    # W3C ids must be lowercase hex; uppercase input is rejected rather than normalized.
    if not isinstance(value, str) or len(value) != length:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _is_zero(value: str) -> bool:
    # W3C forbids all-zero trace and span ids.
    return all(char == "0" for char in value)


def format_traceparent(context: TraceContext) -> str:
    if not _is_hex(context.trace_id, 32) or _is_zero(context.trace_id):
        raise ValueError("trace_id must be 32 non-zero lowercase hex characters")
    if not _is_hex(context.span_id, 16) or _is_zero(context.span_id):
        raise ValueError("span_id must be 16 non-zero lowercase hex characters")
    flags = "01" if context.sampled else "00"
    # Version 00 is the broadly supported W3C traceparent format.
    return f"00-{context.trace_id}-{context.span_id}-{flags}"


def parse_traceparent(value: str | None) -> TraceContext | None:
    """Parse a W3C traceparent field, returning ``None`` for invalid input."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id, flags = parts[:4]
    # Version ff is reserved and must be treated as invalid.
    if not _is_hex(version, 2) or version == "ff":
        return None
    # Version 00 permits no extra trailing fields.
    if version == "00" and len(parts) != 4:
        return None
    if (
        not _is_hex(trace_id, 32)
        or _is_zero(trace_id)
        or not _is_hex(span_id, 16)
        or _is_zero(span_id)
        or not _is_hex(flags, 2)
    ):
        return None
    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        # Only the least significant flag bit is the sampling decision.
        sampled=bool(int(flags, 16) & 0x01),
    )


def _get_header(headers: Mapping[str, Any], name: str) -> Any:
    lowered = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def extract(headers: Mapping[str, Any] | None) -> TraceContext | None:
    """Extract traceparent and optional tracestate with case-insensitive names."""
    if not isinstance(headers, Mapping):
        return None
    try:
        context = parse_traceparent(_get_header(headers, TRACEPARENT_HEADER))
        if context is None:
            return None
        trace_state = _get_header(headers, TRACESTATE_HEADER)
        if trace_state is not None:
            context = TraceContext(
                trace_id=context.trace_id,
                span_id=context.span_id,
                sampled=context.sampled,
                trace_state=str(trace_state),
            )
        return context
    # Malformed header objects must never break request handling.
    except (AttributeError, TypeError, ValueError):
        return None


def as_parent_context(trace_context: TraceContext) -> SpanContext:
    """Convert extracted context to Tracer's ``parent_context`` argument.

    The returned span ID represents the remote parent, not a local parent span.
    """
    return SpanContext(
        trace_id=trace_context.trace_id,
        span_id=trace_context.span_id,
        sampled=trace_context.sampled,
        trace_state=trace_context.trace_state,
    )


def parent_context_kwargs(trace_context: TraceContext) -> dict[str, SpanContext]:
    """Return kwargs for ``Tracer.start_span(..., parent_context=...)``."""
    return {"parent_context": as_parent_context(trace_context)}


def current_span_context() -> TraceContext | None:
    """Build a propagation context from the active local trace and span."""
    trace = get_current_trace()
    span = get_current_span()
    # Refuse to propagate a mismatched local trace/span pair.
    if trace is None or span is None or span.trace_id != trace.trace_id:
        return None
    sampled = bool(trace.metadata.get("sampled", True))
    # A dropped span forces sampling off even when its trace was sampled.
    if span.attributes.get("sampled") is False:
        sampled = False
    baggage = get_trace_context()
    trace_state = trace.metadata.get("trace_state", trace.metadata.get("tracestate"))
    if trace_state is None and isinstance(baggage, dict):
        trace_state = baggage.get("trace_state", baggage.get("tracestate"))
    return TraceContext(
        trace_id=trace.trace_id,
        span_id=span.span_id,
        sampled=sampled,
        trace_state=None if trace_state is None else str(trace_state),
    )


def _remove_header(headers: MutableMapping[str, Any], name: str) -> None:
    lowered = name.lower()
    for key in list(headers):
        if isinstance(key, str) and key.lower() == lowered:
            del headers[key]


def inject(
    headers: MutableMapping[str, Any],
    context: TraceContext | None = None,
) -> None:
    """Inject the active or supplied context while preserving unrelated headers."""
    context = current_span_context() if context is None else context
    if context is None:
        return None
    # Replace any existing casing of the header instead of duplicating it.
    _remove_header(headers, TRACEPARENT_HEADER)
    headers[TRACEPARENT_HEADER] = format_traceparent(context)
    _remove_header(headers, TRACESTATE_HEADER)
    if context.trace_state:
        headers[TRACESTATE_HEADER] = context.trace_state
    return None
