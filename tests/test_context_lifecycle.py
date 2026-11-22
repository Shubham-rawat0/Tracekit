import unittest

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
)
from tracekit.core.tracer import Tracer


class TestContextLifecycle(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)

    def test_nested_spans_restore_grandparent_context(self):
        tracer = Tracer()

        with tracer.trace("trace"):
            with tracer.span("grandparent") as grandparent:
                with tracer.span("parent") as parent:
                    with tracer.span("child") as child:
                        self.assertIs(get_current_span(), child)
                        self.assertEqual(child.parent_span_id, parent.span_id)
                    self.assertIs(get_current_span(), parent)
                self.assertIs(get_current_span(), grandparent)

        self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())

    def test_parentless_inner_span_does_not_clobber_outer_span(self):
        tracer = Tracer()

        with tracer.trace("outer"):
            with tracer.span("outer-span") as outer_span:
                with tracer.trace("inner"):
                    self.assertIsNone(get_current_span())
                    with tracer.span("parentless-inner-span") as inner_span:
                        self.assertIsNone(inner_span.parent_span_id)
                    self.assertIsNone(get_current_span())
                self.assertIs(get_current_span(), outer_span)

        self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())

    def test_direct_api_restores_outer_trace_and_span(self):
        tracer = Tracer()

        with tracer.trace("outer") as outer_trace:
            with tracer.span("outer-span") as outer_span:
                direct_trace = tracer.start_trace("direct")
                self.assertIs(get_current_trace(), direct_trace)
                tracer.end_trace(direct_trace)
                self.assertIs(get_current_trace(), outer_trace)
                self.assertIs(get_current_span(), outer_span)

                direct_span = tracer.start_span("direct-span")
                self.assertIs(get_current_span(), direct_span)
                tracer.end_span(direct_span)
                self.assertIs(get_current_span(), outer_span)
                self.assertIs(get_current_trace(), outer_trace)

        self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())

    def test_direct_api_restores_none_at_top_level(self):
        tracer = Tracer()

        direct_trace = tracer.start_trace("direct")
        tracer.end_trace(direct_trace)
        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_span())

        direct_span = tracer.start_span("direct-span")
        auto_trace = get_current_trace()
        try:
            tracer.end_span(direct_span)
            self.assertIsNone(get_current_span())
            self.assertIsNone(get_current_trace())
        finally:
            if auto_trace is not None:
                tracer.end_trace(auto_trace)

    def test_end_span_clears_current_span(self):
        tracer = Tracer()
        trace = tracer.start_trace("trace")
        span = tracer.start_span("span")

        tracer.end_span(span)

        self.assertIsNot(get_current_span(), span)
        self.assertIsNone(get_current_span())
        tracer.end_trace(trace)
        self.assertIsNone(get_current_trace())


class TestAsyncContextLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)

    async def test_nested_async_trace_and_span_restore_context(self):
        tracer = Tracer()

        async with tracer.async_trace("outer") as outer_trace:
            async with tracer.async_span("outer-span") as outer_span:
                async with tracer.async_trace("inner") as inner_trace:
                    self.assertIs(get_current_trace(), inner_trace)
                    self.assertIsNone(get_current_span())
                    async with tracer.async_span("inner-span") as inner_span:
                        self.assertIs(get_current_span(), inner_span)
                        self.assertIsNone(inner_span.parent_span_id)

                self.assertIs(get_current_trace(), outer_trace)
                self.assertIs(get_current_span(), outer_span)

        self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())


if __name__ == "__main__":
    unittest.main()
