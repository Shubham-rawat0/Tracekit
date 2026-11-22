"""Framework-agnostic HTTP tracing helpers for distributed trace propagation."""

from __future__ import annotations

import time
from collections.abc import Mapping, MutableMapping
from typing import Any

from tracekit.core.decorators import get_default_tracer
from tracekit.core.propagation import TraceContext, extract, format_traceparent, inject
from tracekit.core.span import SpanType
from tracekit.core.tracer import Tracer

# OpenTelemetry semantic convention attribute names
OTEL_HTTP_METHOD = "http.request.method"
OTEL_HTTP_SCHEME = "http.request.scheme"
OTEL_HTTP_TARGET = "http.request.target"
OTEL_HTTP_ROUTE = "http.route"
OTEL_HTTP_STATUS_CODE = "http.response.status_code"
OTEL_HTTP_URL = "http.url"
OTEL_HTTP_HOST = "http.host"
OTEL_NET_PEER_IP = "net.peer.ip"
OTEL_NET_PEER_PORT = "net.peer.port"
OTEL_ERROR_TYPE = "error.type"
OTEL_EXCEPTION_STACKTRACE = "exception.stacktrace"


# Denylist consumed by filter_sensitive_headers; names are matched case-insensitively.
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
    "www-authenticate",
}


__all__ = [
    "OTEL_HTTP_METHOD",
    "OTEL_HTTP_SCHEME",
    "OTEL_HTTP_TARGET",
    "OTEL_HTTP_ROUTE",
    "OTEL_HTTP_STATUS_CODE",
    "OTEL_HTTP_URL",
    "OTEL_HTTP_HOST",
    "OTEL_NET_PEER_IP",
    "OTEL_NET_PEER_PORT",
    "OTEL_ERROR_TYPE",
    "OTEL_EXCEPTION_STACKTRACE",
    "SENSITIVE_HEADERS",
    "filter_sensitive_headers",
    "extract_trace_context",
    "start_server_trace",
    "record_request_attributes",
    "record_response_attributes",
    "record_exception_on_span",
    "inject_trace_context",
    "make_traceparent_header",
    "HTTPTraceHelper",
]


def filter_sensitive_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Return a copy of headers with sensitive values redacted."""
    filtered = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            filtered[key] = "[REDACTED]"
        else:
            filtered[key] = str(value)
    return filtered


def extract_trace_context(headers: Mapping[str, Any] | None) -> TraceContext | None:
    """Extract W3C trace context from HTTP headers."""
    return extract(headers)


def start_server_trace(
    tracer: Tracer | None = None,
    trace_context: TraceContext | None = None,
    name: str = "http.request",
    attributes: dict[str, Any] | None = None,
) -> TraceContext:
    """Start or continue a trace from an incoming trace context.

    Args:
        tracer: Tracer instance. Defaults to get_default_tracer().
        trace_context: Incoming TraceContext from extract_trace_context().
        name: Base name for the trace.
        attributes: Additional attributes to set on the trace.

    Returns:
        The TraceContext for the started trace (can be used for injection).
    """
    tracer = tracer or get_default_tracer()

    if trace_context is not None:
        # Empty headers avoid implicit parsing; the incoming trace_id is assigned explicitly below.
        trace = tracer.continue_trace(
            headers={},
            name=name,
            attributes=attributes,
        )
        # Override with the actual incoming trace context
        trace.trace_id = trace_context.trace_id
    else:
        trace = tracer.start_trace(name=name, attributes=attributes)

    # Set trace_state if available
    # Vendor-specific propagation data would be lost otherwise.
    if trace_context and trace_context.trace_state:
        trace.metadata["trace_state"] = trace_context.trace_state

    # Get the current span context for propagation
    # This span is only for propagation: set_current=False leaves the existing current span intact.
    span = tracer.start_span(
        name=name,
        span_type=SpanType.INTERNAL,
        trace=trace,
        attributes=attributes,
        set_current=False,
    )

    # Build return context
    sampled = bool(trace.metadata.get("sampled", True))
    # An explicit span-level opt-out overrides the trace's sampling decision.
    if span.attributes.get("sampled") is False:
        sampled = False

    return TraceContext(
        trace_id=trace.trace_id,
        span_id=span.span_id,
        sampled=sampled,
        trace_state=trace.metadata.get("trace_state"),
    )


def record_request_attributes(
    span,
    method: str,
    path: str,
    scheme: str = "http",
    host: str | None = None,
    query_string: str | None = None,
    client_ip: str | None = None,
    client_port: int | None = None,
    route: str | None = None,
) -> None:
    """Record HTTP request attributes using OpenTelemetry semantic conventions."""
    if method:
        span.set_attribute(OTEL_HTTP_METHOD, method.upper())
    if scheme:
        span.set_attribute(OTEL_HTTP_SCHEME, scheme)
    if path:
        span.set_attribute(OTEL_HTTP_TARGET, path)
    if route:
        span.set_attribute(OTEL_HTTP_ROUTE, route)
    if host:
        span.set_attribute(OTEL_HTTP_HOST, host)
    if client_ip:
        span.set_attribute(OTEL_NET_PEER_IP, client_ip)
    if client_port:
        span.set_attribute(OTEL_NET_PEER_PORT, client_port)
    if query_string:
        span.set_attribute(OTEL_HTTP_URL, f"{scheme}://{host or ''}{path}?{query_string}")
    elif host and path:
        span.set_attribute(OTEL_HTTP_URL, f"{scheme}://{host}{path}")


def record_response_attributes(
    span,
    status_code: int,
    duration_ms: float | None = None,
) -> None:
    """Record HTTP response attributes."""
    span.set_attribute(OTEL_HTTP_STATUS_CODE, status_code)
    if duration_ms is not None:
        span.attributes["http.duration_ms"] = duration_ms


def record_exception_on_span(span, exc: BaseException) -> None:
    """Record an exception on a span without sensitive data."""
    import traceback

    # Store only the exception type and traceback, never request bodies or headers.
    span.set_attribute(OTEL_ERROR_TYPE, type(exc).__name__)
    span.set_attribute(OTEL_EXCEPTION_STACKTRACE, traceback.format_exc())
    span.status = "error"
    span.error = str(exc)


def inject_trace_context(
    headers: MutableMapping[str, Any],
    trace_context: TraceContext | None = None,
) -> None:
    """Inject trace context into outgoing HTTP headers.

    Preserves unrelated headers. Does not inject if trace_context is None
    and no active trace is available.
    """
    inject(headers, trace_context)


def make_traceparent_header(trace_context: TraceContext) -> str:
    """Generate a traceparent header value from a TraceContext."""
    return format_traceparent(trace_context)


class HTTPTraceHelper:
    """Helper class for manual HTTP trace instrumentation.

    Example:
        helper = HTTPTraceHelper()
        ctx = helper.extract(request.headers)
        trace_ctx = helper.start_trace(ctx, method="GET", path="/api/users")
        try:
            helper.record_request(span, method="GET", path="/api/users")
            response = await handler(request)
            helper.record_response(span, 200)
            helper.inject(response.headers)
        except Exception as e:
            helper.record_exception(span, e)
            raise
        finally:
            helper.end()
    """

    def __init__(
        self,
        tracer: Tracer | None = None,
        trace_name: str = "http.request",
    ) -> None:
        self.tracer = tracer or get_default_tracer()
        self.trace_name = trace_name
        self.trace = None
        self.span = None
        self.trace_context = None

    def extract(self, headers: Mapping[str, Any] | None) -> TraceContext | None:
        """Extract incoming trace context from headers."""
        return extract_trace_context(headers)

    def start_trace(
        self,
        trace_context: TraceContext | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceContext:
        """Start or continue a trace."""
        self.trace_context = start_server_trace(
            tracer=self.tracer,
            trace_context=trace_context,
            name=self.trace_name,
            attributes=attributes,
        )
        return self.trace_context

    def record_request(
        self,
        method: str,
        path: str,
        scheme: str = "http",
        host: str | None = None,
        query_string: str | None = None,
        client_ip: str | None = None,
        client_port: int | None = None,
        route: str | None = None,
    ) -> None:
        """Record request attributes on the active span."""
        span = self.tracer.get_current_span() if hasattr(self.tracer, "get_current_span") else None
        if span is None:
            # Try to get from context
            # Tracers need not expose a getter, so fall back to the global context.
            from tracekit.core.context import get_current_span
            span = get_current_span()
        if span:
            record_request_attributes(
                span,
                method=method,
                path=path,
                scheme=scheme,
                host=host,
                query_string=query_string,
                client_ip=client_ip,
                client_port=client_port,
                route=route,
            )

    def record_response(
        self,
        status_code: int,
        start_time: float | None = None,
    ) -> None:
        """Record response attributes on the active span."""
        span = self.tracer.get_current_span() if hasattr(self.tracer, "get_current_span") else None
        if span is None:
            from tracekit.core.context import get_current_span
            span = get_current_span()
        if span:
            duration_ms = None
            if start_time is not None:
                duration_ms = (time.time() - start_time) * 1000
            record_response_attributes(span, status_code, duration_ms)

    def record_exception(self, exc: BaseException) -> None:
        """Record an exception on the active span."""
        span = self.tracer.get_current_span() if hasattr(self.tracer, "get_current_span") else None
        if span is None:
            from tracekit.core.context import get_current_span
            span = get_current_span()
        if span:
            record_exception_on_span(span, exc)

    def inject(self, headers: MutableMapping[str, Any]) -> None:
        """Inject trace context into response headers."""
        inject_trace_context(headers, self.trace_context)

    def end(self) -> None:
        """End the active span and trace."""
        from tracekit.core.context import get_current_span, get_current_trace

        span = get_current_span()
        trace = get_current_trace()

        if span and span.status == "running":
            span.end(status="ok")

        # End the span first so the trace sees its final state before closing.
        if trace and trace.status == "running":
            self.tracer.end_trace(trace)