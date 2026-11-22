"""Comprehensive tests for Phase 4 HTTP middleware."""

import asyncio
import sys
import time
import unittest
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, create_autospec, patch

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
)
from tracekit.core.propagation import TraceContext, extract, format_traceparent, inject
from tracekit.core.span import SpanType
from tracekit.core.tracer import Tracer

# Import integrations
from tracekit.integrations.http import (
    OTEL_HTTP_METHOD,
    OTEL_HTTP_ROUTE,
    OTEL_HTTP_SCHEME,
    OTEL_HTTP_STATUS_CODE,
    OTEL_HTTP_TARGET,
    SENSITIVE_HEADERS,
    HTTPTraceHelper,
    extract_trace_context,
    filter_sensitive_headers,
    inject_trace_context,
    make_traceparent_header,
    record_exception_on_span,
    record_request_attributes,
    record_response_attributes,
    start_server_trace,
)

# Create mock flask module for tests
flask_mock = ModuleType("flask")
flask_mock.request = MagicMock()
flask_mock.g = MagicMock()
flask_mock.Flask = MagicMock()
sys.modules["flask"] = flask_mock


class FakeSpan:
    """Fake span for testing without real tracer."""
    def __init__(self):
        self.attributes = {}
        self.status = "running"
        self.error = None
        self.span_id = "fake-span-id"
        self.trace_id = "fake-trace-id"

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self, status: str = "ok", error: str | None = None) -> None:
        self.status = status
        self.error = error


class TestHTTPHelpers(unittest.TestCase):
    """Test framework-agnostic HTTP helpers."""

    def test_extract_trace_context_valid(self):
        headers = {
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "tracestate": "vendor=value",
        }
        ctx = extract_trace_context(headers)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.trace_id, "0af7651916cd43dd8448eb211c80319c")
        self.assertEqual(ctx.span_id, "b7ad6b7169203331")
        self.assertTrue(ctx.sampled)
        self.assertEqual(ctx.trace_state, "vendor=value")

    def test_extract_trace_context_invalid(self):
        headers = {"traceparent": "invalid"}
        ctx = extract_trace_context(headers)
        self.assertIsNone(ctx)

    def test_extract_trace_context_none(self):
        ctx = extract_trace_context(None)
        self.assertIsNone(ctx)

    def test_extract_trace_context_case_insensitive(self):
        headers = {
            "TRACEPARENT": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        }
        ctx = extract_trace_context(headers)
        self.assertIsNotNone(ctx)

    def test_filter_sensitive_headers(self):
        headers = {
            "Authorization": "Bearer secret",
            "Cookie": "session=abc",
            "X-API-Key": "key123",
            "Content-Type": "application/json",
        }
        filtered = filter_sensitive_headers(headers)
        self.assertEqual(filtered["Authorization"], "[REDACTED]")
        self.assertEqual(filtered["Cookie"], "[REDACTED]")
        self.assertEqual(filtered["X-API-Key"], "[REDACTED]")
        self.assertEqual(filtered["Content-Type"], "application/json")

    def test_record_request_attributes(self):
        span = FakeSpan()
        record_request_attributes(
            span,
            method="GET",
            path="/api/users",
            scheme="https",
            host="example.com",
            query_string="page=1",
            client_ip="192.168.1.1",
            client_port=12345,
            route="/api/users/{id}",
        )
        self.assertEqual(span.attributes[OTEL_HTTP_METHOD], "GET")
        self.assertEqual(span.attributes[OTEL_HTTP_SCHEME], "https")
        self.assertEqual(span.attributes[OTEL_HTTP_TARGET], "/api/users")
        self.assertEqual(span.attributes[OTEL_HTTP_ROUTE], "/api/users/{id}")
        self.assertEqual(span.attributes["http.host"], "example.com")
        self.assertEqual(span.attributes["net.peer.ip"], "192.168.1.1")
        self.assertEqual(span.attributes["net.peer.port"], 12345)

    def test_record_response_attributes(self):
        span = FakeSpan()
        record_response_attributes(span, 200, 42.5)
        self.assertEqual(span.attributes[OTEL_HTTP_STATUS_CODE], 200)
        self.assertEqual(span.attributes["http.duration_ms"], 42.5)

    def test_record_exception_on_span(self):
        span = FakeSpan()
        try:
            raise ValueError("test error")
        except ValueError as e:
            record_exception_on_span(span, e)
        self.assertEqual(span.status, "error")
        self.assertEqual(span.error, "test error")
        self.assertEqual(span.attributes["error.type"], "ValueError")
        self.assertIn("ValueError: test error", span.attributes["exception.stacktrace"])

    def test_make_traceparent_header(self):
        ctx = TraceContext(
            trace_id="0af7651916cd43dd8448eb211c80319c",
            span_id="b7ad6b7169203331",
            sampled=True,
        )
        header = make_traceparent_header(ctx)
        self.assertEqual(header, "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")

    def test_inject_trace_context(self):
        ctx = TraceContext(
            trace_id="0af7651916cd43dd8448eb211c80319c",
            span_id="b7ad6b7169203331",
            sampled=True,
        )
        headers = {"Content-Type": "application/json"}
        inject_trace_context(headers, ctx)
        self.assertIn("traceparent", headers)
        self.assertEqual(headers["Content-Type"], "application/json")


class TestHTTPTraceHelper(unittest.TestCase):
    """Test HTTPTraceHelper class."""

    def setUp(self):
        self.tracer = Tracer()
        self.helper = HTTPTraceHelper(tracer=self.tracer)

    def test_extract_and_start_trace(self):
        headers = {
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        }
        ctx = self.helper.extract(headers)
        self.assertIsNotNone(ctx)
        trace_ctx = self.helper.start_trace(ctx)
        self.assertEqual(trace_ctx.trace_id, "0af7651916cd43dd8448eb211c80319c")

    def test_start_trace_no_incoming(self):
        trace_ctx = self.helper.start_trace(None)
        self.assertIsNotNone(trace_ctx)
        self.assertIsNotNone(trace_ctx.trace_id)

    def test_record_request_response(self):
        headers = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
        ctx = self.helper.extract(headers)
        self.helper.start_trace(ctx)

        # Need to set current span for record_request to work
        from tracekit.core.context import set_current_span
        fake_span = FakeSpan()
        set_current_span(fake_span)

        self.helper.record_request(
            method="POST",
            path="/api/items",
            scheme="http",
            host="localhost",
            route="/api/items",
        )
        span = get_current_span()
        self.assertEqual(span.attributes[OTEL_HTTP_METHOD], "POST")
        self.assertEqual(span.attributes[OTEL_HTTP_TARGET], "/api/items")

        self.helper.record_response(201, start_time=time.time() - 0.1)
        self.assertEqual(span.attributes[OTEL_HTTP_STATUS_CODE], 201)

    def test_record_exception(self):
        self.helper.start_trace(None)
        from tracekit.core.context import set_current_span
        set_current_span(FakeSpan())

        try:
            raise RuntimeError("server error")
        except RuntimeError as e:
            self.helper.record_exception(e)

        span = get_current_span()
        self.assertEqual(span.status, "error")
        self.assertEqual(span.attributes["error.type"], "RuntimeError")

    def test_inject(self):
        headers = {}
        self.helper.start_trace(None)
        self.helper.inject(headers)
        self.assertIn("traceparent", headers)


class TestASGIMiddleware(unittest.IsolatedAsyncioTestCase):
    """Test ASGI middleware with fake ASGI app."""

    async def test_asgi_normal_request(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()
        app_called = False
        request_received = {}

        async def fake_app(scope, receive, send):
            nonlocal app_called, request_received
            app_called = True
            request_received["scope"] = scope
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"ok": true}',
            })

        middleware = ASGITraceMiddleware(fake_app, tracer=tracer)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/users",
            "scheme": "http",
            "headers": [
                (b"host", b"localhost"),
                (b"user-agent", b"test-client"),
            ],
            "client": ("127.0.0.1", 12345),
        }

        messages = []
        async def fake_send(msg):
            messages.append(msg)

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        await middleware(scope, fake_receive, fake_send)

        self.assertTrue(app_called)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["status"], 200)

        # Check trace was created
        trace = list(tracer._traces.values())[0]
        self.assertEqual(trace.name, "http.request")
        # Check span has the request attributes
        span = list(tracer._spans.values())[0]
        self.assertEqual(span.attributes.get("http.request.method"), "GET")

    async def test_asgi_exception_path(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()

        async def failing_app(scope, receive, send):
            raise ValueError("app error")

        middleware = ASGITraceMiddleware(failing_app, tracer=tracer)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/items",
            "scheme": "https",
            "headers": [(b"host", b"localhost")],
            "client": ("10.0.0.1", 54321),
        }

        messages = []
        async def fake_send(msg):
            messages.append(msg)

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        with self.assertRaises(ValueError):
            await middleware(scope, fake_receive, fake_send)

        # Check trace recorded error
        trace = list(tracer._traces.values())[0]
        self.assertEqual(trace.status, "error")
        span = list(tracer._spans.values())[0]
        self.assertEqual(span.status, "error")

    async def test_asgi_malformed_traceparent(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()
        app_called = False

        async def fake_app(scope, receive, send):
            nonlocal app_called
            app_called = True
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        middleware = ASGITraceMiddleware(fake_app, tracer=tracer)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "scheme": "http",
            "headers": [(b"traceparent", b"invalid-format")],
        }

        async def fake_send(msg):
            pass

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        await middleware(scope, fake_receive, fake_send)

        self.assertTrue(app_called)
        # Should still create trace even with malformed traceparent
        trace = list(tracer._traces.values())[0]
        self.assertIsNotNone(trace)

    async def test_asgi_outgoing_header_preservation(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()

        async def fake_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-custom", b"custom-value"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})

        middleware = ASGITraceMiddleware(fake_app, tracer=tracer)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "scheme": "http",
            "headers": [],
        }

        messages = []
        async def fake_send(msg):
            messages.append(msg)

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        await middleware(scope, fake_receive, fake_send)

        # Check response was sent
        start_msg = next(m for m in messages if m["type"] == "http.response.start")
        self.assertEqual(start_msg["status"], 200)

    async def test_asgi_nested_operations(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()
        nested_span_created = False

        async def nested_app(scope, receive, send):
            nonlocal nested_span_created
            # Simulate nested operation
            nested_span = tracer.start_span("nested.op", span_type=SpanType.INTERNAL)
            nested_span.end()
            nested_span_created = True

            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        middleware = ASGITraceMiddleware(nested_app, tracer=tracer)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/nested",
            "scheme": "http",
            "headers": [],
        }

        async def fake_send(msg):
            pass

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        await middleware(scope, fake_receive, fake_send)

        self.assertTrue(nested_span_created)
        # Main request span should still be active
        spans = list(tracer._spans.values())
        self.assertGreaterEqual(len(spans), 1)

    async def test_asgi_exclude_paths(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()

        async def fake_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        middleware = ASGITraceMiddleware(
            fake_app,
            tracer=tracer,
            exclude_paths={"/health", "/metrics"},
        )

        # Health check should not be traced
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "scheme": "http",
            "headers": [],
        }

        async def fake_send(msg):
            pass

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        await middleware(scope, fake_receive, fake_send)

        # No trace should be created for excluded path
        self.assertEqual(len(tracer._traces), 0)

    async def test_asgi_no_sensitive_headers_captured(self):
        from tracekit.integrations.asgi import ASGITraceMiddleware

        tracer = Tracer()

        async def fake_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        middleware = ASGITraceMiddleware(
            fake_app,
            tracer=tracer,
            capture_request_headers=True,
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "scheme": "http",
            "headers": [
                (b"authorization", b"Bearer secret123"),
                (b"cookie", b"session=abc"),
                (b"x-custom", b"safe-value"),
            ],
        }

        async def fake_send(msg):
            pass

        async def fake_receive():
            return {"type": "http.request", "body": b""}

        await middleware(scope, fake_receive, fake_send)

        spans = list(tracer._spans.values())
        self.assertEqual(len(spans), 1)
        span = spans[0]
        # Sensitive headers should be redacted
        self.assertEqual(span.attributes.get("http.request.header.authorization"), "[REDACTED]")
        self.assertEqual(span.attributes.get("http.request.header.cookie"), "[REDACTED]")
        # Non-sensitive header should be captured
        self.assertEqual(span.attributes.get("http.request.header.x-custom"), "safe-value")


class TestFlaskMiddleware(unittest.TestCase):
    """Test Flask middleware with fake request objects."""

    def setUp(self):
        # Reset flask mocks
        flask_mock.request = MagicMock()
        flask_mock.g = MagicMock()

    def test_flask_normal_request(self):
        from tracekit.integrations.flask import FlaskTraceMiddleware

        tracer = Tracer()
        middleware = FlaskTraceMiddleware(tracer=tracer)

        # Create fake request object
        request = flask_mock.request
        request.path = "/api/users"
        request.method = "GET"
        request.scheme = "http"
        request.headers = {
            "host": "localhost",
            "user-agent": "test-client",
            "authorization": "Bearer secret",
        }
        request.remote_addr = "127.0.0.1"
        request.url_rule = MagicMock()
        request.url_rule.rule = "/api/users/<id>"

        # Create fake response object
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}

        # Create fake g object
        g = flask_mock.g

        # before_request
        middleware._before_request()

        # Check trace and span created
        self.assertIsNotNone(g._tracekit_trace)
        self.assertIsNotNone(g._tracekit_span)
        trace = g._tracekit_trace
        span = g._tracekit_span

        self.assertEqual(trace.name, "http.request")
        self.assertEqual(span.attributes["http.request.method"], "GET")
        self.assertEqual(span.attributes["http.request.target"], "/api/users")
        self.assertEqual(span.attributes["http.route"], "/api/users/<id>")

        # after_request
        middleware._after_request(response)

        # Check response recorded
        self.assertEqual(span.attributes["http.response.status_code"], 200)
        # Sensitive header should be redacted
        self.assertEqual(span.attributes.get("http.request.header.authorization"), "[REDACTED]")

        # teardown_request
        middleware._teardown_request(None)

    def test_flask_exception_path(self):
        from tracekit.integrations.flask import FlaskTraceMiddleware

        tracer = Tracer()
        middleware = FlaskTraceMiddleware(tracer=tracer)

        request = flask_mock.request
        request.path = "/api/items"
        request.method = "POST"
        request.scheme = "https"
        request.headers = {"host": "localhost"}
        request.remote_addr = "10.0.0.1"
        request.url_rule = MagicMock()
        request.url_rule.rule = "/api/items"

        response = MagicMock()
        response.status_code = 500
        response.headers = {}

        g = flask_mock.g

        middleware._before_request()

        # teardown with exception
        middleware._teardown_request(ValueError("server error"))

        span = g._tracekit_span
        self.assertEqual(span.status, "error")
        self.assertEqual(span.error, "server error")
        self.assertIn("error.type", span.attributes)
        self.assertEqual(span.attributes["error.type"], "ValueError")

    def test_flask_exclude_paths(self):
        from tracekit.integrations.flask import FlaskTraceMiddleware

        tracer = Tracer()
        middleware = FlaskTraceMiddleware(tracer=tracer, exclude_paths={"/health"})

        request = flask_mock.request
        request.path = "/health"
        request.method = "GET"

        result = middleware._before_request()

        # Should return None and not set trace/span
        self.assertIsNone(result)
        # Check that trace was not started by checking tracer has no traces
        self.assertEqual(len(tracer._traces), 0)

    def test_flask_no_sensitive_body_capture(self):
        from tracekit.integrations.flask import FlaskTraceMiddleware

        tracer = Tracer()
        middleware = FlaskTraceMiddleware(tracer=tracer)

        request = flask_mock.request
        request.path = "/api/data"
        request.method = "POST"
        request.scheme = "http"
        request.headers = {"content-type": "application/json"}
        request.remote_addr = "127.0.0.1"
        request.url_rule = None

        g = flask_mock.g

        middleware._before_request()
        middleware._teardown_request(None)

        span = g._tracekit_span
        # No request/response body should be captured
        self.assertNotIn("http.request.body", span.attributes)
        self.assertNotIn("http.response.body", span.attributes)

    def test_flask_init_flask_tracing(self):
        from tracekit.integrations.flask import FlaskTraceMiddleware, init_flask_tracing

        tracer = Tracer()
        app = MagicMock()

        middleware = init_flask_tracing(app, tracer=tracer)

        self.assertIsInstance(middleware, FlaskTraceMiddleware)
        app.before_request.assert_called()
        app.after_request.assert_called()
        app.teardown_request.assert_called()


class TestPropagationIntegration(unittest.TestCase):
    """Test trace context propagation end-to-end."""

    def test_extract_inject_roundtrip(self):
        """Test extracting from incoming and injecting to outgoing."""
        # Incoming headers
        incoming = {
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "tracestate": "vendor=value",
        }

        # Extract
        ctx = extract(incoming)
        self.assertIsNotNone(ctx)

        # Outgoing headers
        outgoing = {"content-type": "application/json"}
        inject(outgoing, ctx)

        # Verify
        self.assertIn("traceparent", outgoing)
        self.assertEqual(outgoing["traceparent"], "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
        self.assertEqual(outgoing["tracestate"], "vendor=value")
        self.assertEqual(outgoing["content-type"], "application/json")

    def test_extract_none_then_inject_none(self):
        """Test no context extracted, nothing injected."""
        incoming = {}
        ctx = extract(incoming)
        self.assertIsNone(ctx)

        outgoing = {"content-type": "application/json"}
        # inject with None should not call current_span_context
        inject(outgoing, ctx)
        self.assertNotIn("traceparent", outgoing)

    def test_continue_trace_preserves_trace_id(self):
        """Test continue_trace uses incoming trace_id."""
        tracer = Tracer()
        incoming = {
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        }
        trace = tracer.continue_trace(incoming, name="test")
        self.assertEqual(trace.trace_id, "0af7651916cd43dd8448eb211c80319c")


if __name__ == "__main__":
    unittest.main()