"""Trace and span context management using contextvars."""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracekit.core.span import Span
    from tracekit.core.trace import Trace

# contextvars keep the active trace/span isolated per async task and thread.
_current_trace_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_current_trace_context", default=None
)
_current_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "_current_trace", default=None
)
_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "_current_span", default=None
)


def get_trace_context() -> dict[str, Any] | None:
    return _current_trace_context.get()


def set_trace_context(ctx: dict[str, Any] | None) -> contextvars.Token:
    # set() returns a token so reset() restores the previous value, not just clears it.
    return _current_trace_context.set(ctx)


def reset_trace_context(token: contextvars.Token) -> None:
    # Tokens are context-local; resetting one from another task will raise.
    _current_trace_context.reset(token)


def get_current_trace() -> "Trace | None":
    return _current_trace.get()


def set_current_trace(trace: "Trace | None") -> contextvars.Token:
    return _current_trace.set(trace)


def reset_current_trace(token: contextvars.Token) -> None:
    _current_trace.reset(token)


def get_current_span() -> "Span | None":
    return _current_span.get()


def set_current_span(span: "Span | None") -> contextvars.Token:
    return _current_span.set(span)


def reset_current_span(token: contextvars.Token) -> None:
    _current_span.reset(token)
