import unittest

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    get_trace_context,
    set_current_span,
    set_current_trace,
    set_trace_context,
)
from tracekit.core.tracer import Tracer


class TestDirectAPIContextRestoration(unittest.TestCase):
    def setUp(self):
        self._clear_context()

    def tearDown(self):
        self._clear_context()

    @staticmethod
    def _clear_context():
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_direct_end_restores_nested_trace_and_span_context(self):
        tracer = Tracer()
        outer_trace = tracer.start_trace("outer")
        outer_span = tracer.start_span("outer-span")

        inner_trace = tracer.start_trace("inner")
        inner_span = tracer.start_span("inner-span")
        nested_span = tracer.start_span("nested-span")
        self.assertIs(get_current_trace(), inner_trace)
        self.assertIs(get_current_span(), nested_span)

        tracer.end_span(nested_span)
        self.assertIs(get_current_span(), inner_span)
        tracer.end_span(inner_span)
        self.assertIs(get_current_span(), outer_span)

        tracer.end_trace(inner_trace)
        self.assertIs(get_current_trace(), outer_trace)
        self.assertIs(get_current_span(), outer_span)
        self.assertEqual(get_trace_context()["trace_id"], outer_trace.trace_id)

        tracer.end_span(outer_span)
        self.assertIsNone(get_current_span())
        tracer.end_trace(outer_trace)
        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_trace_context())


if __name__ == "__main__":
    unittest.main()
