"""Flask middleware for distributed HTTP tracing.

Uses duck typing to avoid importing Flask at module import time.
"""

from __future__ import annotations

import time
from typing import Any, Optional

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


class FlaskTraceMiddleware:
    """Flask middleware for automatic HTTP request tracing.

    Usage:
        from tracekit.integrations.flask import FlaskTraceMiddleware
        app = Flask(__name__)
        FlaskTraceMiddleware(app)

    Or with init_app:
        tracer = Tracer()
        middleware = FlaskTraceMiddleware()
        middleware.init_app(app, tracer=tracer)
    """

    def __init__(
        self,
        app: Any = None,
        tracer: Tracer | None = None,
        trace_name: str = "http.request",
        exclude_paths: set[str] | None = None,
        capture_request_headers: bool = True,
        capture_response_headers: bool = False,
    ) -> None:
        """Initialize Flask tracing middleware.

        Args:
            app: Flask application instance. If None, call init_app() later.
            tracer: Tracer instance. Defaults to get_default_tracer().
            trace_name: Base name for traces.
            exclude_paths: Paths to exclude from tracing (exact match or prefix).
            capture_request_headers: Whether to record request headers.
            capture_response_headers: Whether to record response headers (excluding sensitive).
        """
        self.tracer = tracer
        self.trace_name = trace_name
        self.exclude_paths = exclude_paths or {"/health", "/healthz", "/ready", "/metrics"}
        self.capture_request_headers = capture_request_headers
        self.capture_response_headers = capture_response_headers
        self.sensitive_headers = SENSITIVE_HEADERS.copy()

        if app is not None:
            self.init_app(app)

    def init_app(
        self,
        app: Any,
        tracer: Tracer | None = None,
    ) -> None:
        """Initialize the middleware with a Flask app.

        Args:
            app: Flask application instance.
            tracer: Optional Tracer instance to override default.
        """
        if tracer is not None:
            self.tracer = tracer

        # Use before_request, after_request, and teardown_request
        app.before_request(self._before_request)
        app.after_request(self._after_request)
        app.teardown_request(self._teardown_request)

    def _should_trace(self, path: str) -> bool:
        """Check if path should be traced."""
        for exclude in self.exclude_paths:
            if path == exclude or path.startswith(exclude.rstrip("*")):
                return False
        return True

    def _get_headers_dict(self, headers_obj: Any) -> dict[str, str]:
        """Convert Flask Headers or dict-like to plain dict."""
        if hasattr(headers_obj, "items"):
            return dict(headers_obj.items())
        if hasattr(headers_obj, "__iter__"):
            return dict(headers_obj)
        return {}

    def _get_client_ip(self, request: Any) -> str | None:
        """Extract client IP from request, handling proxies."""
        # Check X-Forwarded-For first
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote_addr

    def _get_client_port(self, request: Any) -> int | None:
        """Extract client port if available."""
        # Not typically available in Flask, but check environ
        environ = getattr(request, "environ", {})
        return environ.get("REMOTE_PORT")

    def _get_route(self, request: Any) -> str | None:
        """Extract route pattern from request."""
        # Flask stores the URL rule in request.url_rule
        # The rule template beats the raw path for cardinality of http.route attributes.
        if hasattr(request, "url_rule") and request.url_rule is not None:
            return request.url_rule.rule
        return request.path

    def _before_request(self) -> Optional[Any]:
        """Flask before_request handler - starts trace and span."""
        # Import request here to avoid module-level Flask import
        try:
            from flask import g, request
        except ImportError:
            # Flask not available
            return None

        if not self._should_trace(request.path):
            return None

        tracer = self.tracer or get_default_tracer()

        # Extract headers
        headers = self._get_headers_dict(request.headers)

        # Extract incoming trace context
        trace_context = extract(headers)

        # Start trace
        # Forwarding the headers lets continue_trace adopt the client's remote parent span.
        trace = tracer.continue_trace(
            headers=headers if trace_context else None,
            name=self.trace_name,
        )

        # Get route pattern for http.route attribute
        route = self._get_route(request)

        # Client info
        client_ip = self._get_client_ip(request)
        client_port = self._get_client_port(request)

        # Start span for the request
        span = tracer.start_span(
            name=self.trace_name,
            span_type=SpanType.HTTP,
            trace=trace,
            attributes={
                OTEL_HTTP_METHOD: request.method,
                OTEL_HTTP_SCHEME: request.scheme,
                OTEL_HTTP_TARGET: request.path,
                OTEL_HTTP_ROUTE: route or request.path,
                OTEL_HTTP_HOST: request.host,
            } | (
                {OTEL_NET_PEER_IP: client_ip} if client_ip else {}
            ) | (
                {OTEL_NET_PEER_PORT: client_port} if client_port else {}
            ),
            set_current=True,
        )

        # Capture request headers if enabled
        if self.capture_request_headers:
            filtered_headers = filter_sensitive_headers(headers)
            for key, value in filtered_headers.items():
                span.set_attribute(f"http.request.header.{key.lower()}", value)

        # Store trace and span on g for access in after/teardown
        # g is request-local, so concurrent requests cannot mix up span state.
        g._tracekit_trace = trace
        g._tracekit_span = span
        g._tracekit_start_time = time.time()
        g._tracekit_tracer = tracer

        # Store trace/span tokens for cleanup
        g._tracekit_trace_token = getattr(trace, "_tracekit_trace_token", None)
        g._tracekit_span_token = getattr(span, "_tracekit_span_token", None)

        return None

    def _after_request(self, response: Any) -> Any:
        """Flask after_request handler - records response attributes."""
        try:
            from flask import g
        except ImportError:
            return response

        tracer = getattr(g, "_tracekit_tracer", None)
        trace = getattr(g, "_tracekit_trace", None)
        span = getattr(g, "_tracekit_span", None)
        start_time = getattr(g, "_tracekit_start_time", None)

        if tracer is None or span is None:
            return response

        duration_ms = None
        if start_time is not None:
            duration_ms = (time.time() - start_time) * 1000

        # Record response attributes
        record_response_attributes(span, response.status_code, duration_ms)

        # Capture response headers if enabled
        if self.capture_response_headers and hasattr(response, "headers"):
            filtered_response = filter_sensitive_headers(
                self._get_headers_dict(response.headers)
            )
            for key, value in filtered_response.items():
                if key.lower() not in self.sensitive_headers:
                    span.set_attribute(f"http.response.header.{key.lower()}", value)

        # Inject trace context into response headers
        trace_context = None
        if trace and span:
            from tracekit.core.propagation import current_span_context
            trace_context = current_span_context()
        if trace_context:
            response_headers = self._get_headers_dict(response.headers)
            inject(response_headers)
            # Update response headers
            # Write into the real response object so clients can observe the injected context.
            for key, value in response_headers.items():
                response.headers[key] = value

        return response

    def _teardown_request(self, exc: BaseException | None) -> None:
        """Flask teardown_request handler - ends span/trace and cleans up context."""
        # Teardown runs even on exceptions, making it the reliable place to finish spans.
        try:
            from flask import g
        except ImportError:
            return

        tracer = getattr(g, "_tracekit_tracer", None)
        trace = getattr(g, "_tracekit_trace", None)
        span = getattr(g, "_tracekit_span", None)
        trace_token = getattr(g, "_tracekit_trace_token", None)
        span_token = getattr(g, "_tracekit_span_token", None)

        if tracer is None or span is None:
            return

        try:
            if exc is not None:
                record_exception_on_span(span, exc)
                if span.status == "running":
                    span.end(status="error", error=str(exc))
            elif span.status == "running":
                span.end(status="ok")
        finally:
            # Clean up context
            # Only reset our own tokens; a nested span may have replaced the current ones.
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
            tracer._maybe_end_auto_trace(span)


def init_flask_tracing(
    app: Any,
    tracer: Tracer | None = None,
    trace_name: str = "http.request",
    exclude_paths: set[str] | None = None,
    capture_request_headers: bool = True,
    capture_response_headers: bool = False,
) -> FlaskTraceMiddleware:
    """Convenience function to initialize Flask tracing.

    Args:
        app: Flask application instance.
        tracer: Tracer instance. Defaults to get_default_tracer().
        trace_name: Base name for traces.
        exclude_paths: Paths to exclude from tracing.
        capture_request_headers: Whether to record request headers.
        capture_response_headers: Whether to record response headers.

    Returns:
        The initialized FlaskTraceMiddleware instance.
    """
    middleware = FlaskTraceMiddleware(
        tracer=tracer,
        trace_name=trace_name,
        exclude_paths=exclude_paths,
        capture_request_headers=capture_request_headers,
        capture_response_headers=capture_response_headers,
    )
    middleware.init_app(app)
    return middleware