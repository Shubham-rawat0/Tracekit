"""Compatibility shim for trace context access."""

# Re-exports keep older "from tracekit.context import ..." imports working after the move to core.context.
from tracekit.core.context import (
    _current_trace_context,
    get_current_span,
    get_current_trace,
    reset_current_span,
    reset_current_trace,
    set_current_span,
    set_current_trace,
)

__all__ = [
    "_current_trace_context",  # Exported despite the underscore; legacy callers import it directly.
    "get_current_trace",
    "set_current_trace",
    "reset_current_trace",
    "get_current_span",
    "set_current_span",
    "reset_current_span",
]
