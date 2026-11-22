"""ASGI middleware for distributed HTTP tracing.

Compatible with Starlette, FastAPI, and any ASGI framework without importing them.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, MutableMapping

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    reset_current_span,
    reset_current_trace,
)
from tracekit.core.decorators import get_default_tracer
from tracekit.core.propagation import extract, inject
from tracekit.core.span import SpanType
from tracekit.core.tracer import Tracer

from .http import (
    OTEL_HTTP_HOST,
    OTEL_HTTP_METHOD,
    OTEL_HTTP_ROUTE,
    OTEL_HTTP_SCHEME,
    OTEL_HTTP_TARGET,
    OTEL_NET_PEER_IP,
    OTEL_NET_PEER_PORT,
    SENSITIVE_HEADERS,
    filter_sensitive_headers,
    record_exception_on_span,
    record_response_attributes,
)

# Type aliases for ASGI
ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class ASGITraceMiddleware:
    """ASGI middleware for automatic HTTP request tracing.

    Usage:
        from tracekit.integrations.asgi import ASGITraceMiddleware
        app = ASGITraceMiddleware(app)
    """

    def __init__(
        self,
        app: ASGIApp,
        tracer: Tracer | None = None,
        trace_name: str = "http.request",
        exclude_paths: set[str] | None = None,
        exclude_header_names: set[str] | None = None,
        capture_request_headers: bool = True,
        capture_response_headers: bool = False,
    ) -> None:
        """Initialize ASGI tracing middleware.

        Args:
            app: The ASGI application to wrap.
            tracer: Tracer instance. Defaults to get_default_tracer().
            trace_name: Base name for traces.
            exclude_paths: Paths to exclude from tracing (exact match or prefix).
            exclude_header_names: Additional header names to exclude from capture.
            capture_request_headers: Whether to record request headers.
            capture_response_headers: Whether to record response headers (excluding sensitive).
        """
        self.app = app
        self.tracer = tracer or get_default_tracer()
        self.trace_name = trace_name
        self.exclude_paths = exclude_paths or {"/health", "/healthz", "/ready", "/metrics"}
        self.capture_request_headers = capture_request_headers
        self.capture_response_headers = capture_response_headers
        self.sensitive_headers = SENSITIVE_HEADERS | (exclude_header_names or set())

    def _should_trace(self, path: str) -> bool:
        """Check if path should be traced."""
        for exclude in self.exclude_paths:
            if path == exclude or path.startswith(exclude.rstrip("*")):
                return False
        return True

    def _get_headers(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        """Extract headers from ASGI scope."""
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        return headers

    def _headers_to_dict(self, headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
        """Convert ASGI headers to dict with string keys/values."""
        result: dict[str, str] = {}
        for key, value in headers:
            # ASGI specifies latin-1 for header bytes; UTF-8 would corrupt non-ASCII values.
            k = key.decode("latin-1")
            v = value.decode("latin-1")
            # Repeated headers are comma-joined to match HTTP header semantics.
            if k in result:
                result[k] = f"{result[k]},{v}"
            else:
                result[k] = v
        return result

    def _get_client_info(self, scope: Scope) -> tuple[str | None, int | None]:
        """Extract client IP and port from scope."""
        client = scope.get("client")
        if client:
            return client[0], client[1]
        return None, None

    def _get_route(self, scope: Scope) -> str | None:
        """Extract route pattern from scope (Starlette/FastAPI specific)."""
        # Try common places where route is stored
        # Prefer the parameterized route over the raw path to keep span attributes low-cardinality.
        if "route" in scope and scope["route"] is not None:
            route = scope["route"]
            if hasattr(route, "path"):
                return route.path
            if hasattr(route, "path_format"):
                return route.path_format
        return scope.get("path")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self._should_trace(path):
            await self.app(scope, receive, send)
            return

        # Extract headers
        headers = self._headers_to_dict(self._get_headers(scope))

        # Extract incoming trace context
        # W3C traceparent/tracestate from the client seed the parent of this server trace.
        trace_context = extract(headers)

        # Start trace with parent_context from extracted headers
        parent_context = None
        if trace_context:
            from tracekit.core.propagation import as_parent_context
            # Extracted headers need conversion into the tracer's parent-context shape.
            parent_context = as_parent_context(trace_context)

        trace = self.tracer.start_trace(
            name=self.trace_name,
            parent_context=parent_context,
        )

        # Get route pattern for http.route attribute
        route = self._get_route(scope)

        # Start span for the request
        client_ip, client_port = self._get_client_info(scope)
        span = self.tracer.start_span(
            name=self.trace_name,
            span_type=SpanType.HTTP,
            trace=trace,
            attributes={
                OTEL_HTTP_METHOD: scope.get("method", "GET"),
                OTEL_HTTP_SCHEME: scope.get("scheme", "http"),
                OTEL_HTTP_TARGET: path,
                OTEL_HTTP_ROUTE: route or path,
                OTEL_HTTP_HOST: headers.get("host"),
            } | (
                {OTEL_NET_PEER_IP: client_ip} if client_ip else {}
            ) | (
                {OTEL_NET_PEER_PORT: client_port} if client_port else {}
            ),
            set_current=True,
        )

        # Capture request headers if enabled
        # Redact credentials before copying headers into span attributes.
        if self.capture_request_headers:
            filtered_headers = filter_sensitive_headers(headers)
            for key, value in filtered_headers.items():
                span.set_attribute(f"http.request.header.{key.lower()}", value)

        # Store trace and span tokens for cleanup
        trace_token = getattr(trace, "_tracekit_trace_token", None)
        span_token = getattr(span, "_tracekit_span_token", None)

        start_time = time.time()
        # Default to 500 so a request that never sends a response start still records failure.
        response_status = 500
        response_headers: list[tuple[bytes, bytes]] = []
        response_started = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            # Observe response messages without consuming or changing what the app sends.
            nonlocal response_status, response_headers, response_started
            if message["type"] == "http.response.start":
                response_status = message.get("status", 500)
                response_headers = message.get("headers", [])
                response_started = True
            elif message["type"] == "http.response.body":
                # Could capture body here if needed
                pass
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
            if span.status == "running":
                span.end(status="ok")
            # End trace on success
            if trace.status == "running":
                self.tracer.end_trace(trace, status="ok")
        except BaseException as e:
            record_exception_on_span(span, e)
            if span.status == "running":
                span.end(status="error", error=str(e))
            # End trace on error
            if trace.status == "running":
                self.tracer.end_trace(trace, status="error", error=str(e))
            raise
        finally:
            # Record response attributes
            duration_ms = (time.time() - start_time) * 1000
            record_response_attributes(span, response_status, duration_ms)

            # Capture response headers if enabled
            if self.capture_response_headers and response_headers:
                filtered_response = filter_sensitive_headers(
                    {k.decode("latin-1"): v.decode("latin-1") for k, v in response_headers}
                )
                for key, value in filtered_response.items():
                    if key.lower() not in self.sensitive_headers:
                        span.set_attribute(f"http.response.header.{key.lower()}", value)

            # Inject trace context into response headers (for logging/documentation)
            if response_started:
                response_headers_dict: MutableMapping[str, Any] = {}
                for header_key, header_value in response_headers:
                    decoded_key = header_key.decode("latin-1")
                    decoded_value = header_value.decode("latin-1")
                    response_headers_dict[decoded_key] = decoded_value

                # Build context from current span for injection
                from tracekit.core.propagation import current_span_context
                current_ctx = current_span_context()
                # Without an active span there is no trace context to inject into headers.
                if current_ctx:
                    inject(response_headers_dict)

            # Clean up context
            # Identity checks ensure we only reset our own tokens if no nested instrumentation replaced them.
            if trace_token and get_current_trace() is trace:
                try:
                    reset_current_trace(trace_token)
                except Exception:
                    pass
            if span_token and get_current_span() is span:
                try:
                    reset_current_span(span_token)
                except Exception:
                    pass

            # End trace if no more spans
            self.tracer._maybe_end_auto_trace(span)