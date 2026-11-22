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


class TestNestedTraceContextRestoration(unittest.TestCase):
    def setUp(self):
        self._clear_context()

    def tearDown(self):
        self._clear_context()

    @staticmethod
    def _clear_context():
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_nested_trace_restores_outer_context_after_normal_exit(self):
        tracer = Tracer()
        with tracer.trace("outer") as outer:
            with tracer.span("outer-span") as outer_span:
                outer_baggage = get_trace_context()
                with tracer.trace("inner") as inner:
                    self.assertIs(get_current_trace(), inner)
                    self.assertIsNone(get_current_span())
                    self.assertEqual(get_trace_context()["trace_id"], inner.trace_id)
                self.assertIs(get_current_trace(), outer)
                self.assertIs(get_current_span(), outer_span)
                self.assertEqual(get_trace_context(), outer_baggage)

        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_span())
        self.assertIsNone(get_trace_context())

    def test_nested_trace_restores_outer_context_after_exception(self):
        tracer = Tracer()
        with tracer.trace("outer") as outer:
            with tracer.span("outer-span") as outer_span:
                outer_baggage = get_trace_context()
                with self.assertRaisesRegex(RuntimeError, "inner failure"):
                    with tracer.trace("inner") as inner:
                        self.assertEqual(get_trace_context()["trace_id"], inner.trace_id)
                        raise RuntimeError("inner failure")

                self.assertIs(get_current_trace(), outer)
                self.assertIs(get_current_span(), outer_span)
                self.assertEqual(get_trace_context(), outer_baggage)

        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_span())
        self.assertIsNone(get_trace_context())


if __name__ == "__main__":
    unittest.main()
