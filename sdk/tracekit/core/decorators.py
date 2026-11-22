"""Decorator and context helpers for tracing."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from tracekit.core.span import SpanType
from tracekit.core.tracer import Tracer

# The default is resolved at call time, so set_default_tracer affects already-decorated functions.
_default_tracer = Tracer()


def get_default_tracer() -> Tracer:
    return _default_tracer


def set_default_tracer(tracer: Tracer) -> None:
    global _default_tracer
    _default_tracer = tracer


def trace(name: str | None = None, tracer: Tracer | None = None, **trace_kwargs: Any):
    def decorator(func: Callable):
        trace_name = name or func.__name__
        # Wrapper kind is chosen once from the original function, so async functions are always awaited.
        if inspect.iscoroutinefunction(func):
            # wraps preserves the original name/docstring for introspection and framework discovery.
            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                t = tracer or _default_tracer
                async with t.async_trace(trace_name, **trace_kwargs):
                    return await func(*args, **kwargs)
            return awrapper
        else:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                t = tracer or _default_tracer
                with t.trace(trace_name, **trace_kwargs):
                    return func(*args, **kwargs)
            return wrapper
    return decorator


def span(
    name: str | None = None,
    span_type: SpanType | str = SpanType.INTERNAL,
    tracer: Tracer | None = None,
    **span_kwargs: Any,
):
    def decorator(func: Callable):
        span_name = name or func.__name__
        # The tracer argument is captured at decoration time; only the fallback stays dynamic.
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                t = tracer or _default_tracer
                async with t.async_span(span_name, span_type=span_type, **span_kwargs) as sp:
                    try:
                        result = await func(*args, **kwargs)
                        # A successful return is recorded as the span's output.
                        sp.output = result
                        return result
                    except Exception:
                        # Re-raising unchanged lets the span context manager observe the failure via __exit__.
                        raise
            return awrapper
        else:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                t = tracer or _default_tracer
                with t.span(span_name, span_type=span_type, **span_kwargs) as sp:
                    try:
                        result = func(*args, **kwargs)
                        sp.output = result
                        return result
                    except Exception:
                        raise
            return wrapper
    return decorator
