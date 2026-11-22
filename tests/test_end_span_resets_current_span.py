import unittest

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
    set_trace_context,
)
from tracekit.core.tracer import Tracer


class TestEndSpanCurrentContext(unittest.TestCase):
    def setUp(self):
        self._clear_context()

    def tearDown(self):
        self._clear_context()

    @staticmethod
    def _clear_context():
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_end_current_span_clears_it_and_noncurrent_end_preserves_current(self):
        tracer = Tracer()
        trace = tracer.start_trace("trace")
        current_span = tracer.start_span("current")
        noncurrent_span = tracer.start_span(
            "noncurrent",
            trace=trace,
            parent=current_span,
            set_current=False,
        )

        self.assertIs(get_current_span(), current_span)
        tracer.end_span(noncurrent_span)
        self.assertIs(get_current_span(), current_span)

        tracer.end_span(current_span)
        self.assertIsNone(get_current_span())
        tracer.end_trace(trace)
        self.assertIsNone(get_current_trace())


if __name__ == "__main__":
    unittest.main()
