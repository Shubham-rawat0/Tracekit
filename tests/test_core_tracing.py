import unittest

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
)
from tracekit.core.tracer import Tracer


class TestCoreTracing(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)

    def test_trace_span_lifecycle_and_replay(self):
        tracer = Tracer()
        with tracer.trace("root") as tr:
            self.assertIs(get_current_trace(), tr)
            with tracer.span("llm", span_type="llm", input="hi") as s1:
                tracer.record_llm_result(s1, response="hello", total_tokens=5)
            with tracer.span("tool", span_type="tool", input={"q": 1}) as s2:
                tracer.record_tool_result(s2, output={"a": 2})
        replay = tracer.build_trace_replay(tr.trace_id)
        self.assertIsNotNone(replay)
        self.assertEqual(replay["trace"]["status"], "ok")
        self.assertEqual(len(replay["spans"]), 2)
        self.assertEqual({s["span_type"] for s in replay["spans"]}, {"llm", "tool"})

    def test_context_restored_after_span(self):
        tracer = Tracer()
        tr = tracer.start_trace("t")
        self.assertIsNone(get_current_span())
        with tracer.span("a") as a:
            self.assertIs(get_current_span(), a)
            with tracer.span("b") as b:
                self.assertIs(get_current_span(), b)
                self.assertEqual(b.parent_span_id, a.span_id)
            self.assertIs(get_current_span(), a)
        self.assertIsNone(get_current_span())
        tracer.end_trace(tr)
        self.assertIsNone(get_current_trace())

    def test_error_recording(self):
        tracer = Tracer()
        with self.assertRaises(ValueError):
            with tracer.trace("t"):
                with tracer.span("boom"):
                    raise ValueError("fail")
        tr = list(tracer._traces.values())[0]
        sp = list(tracer._spans.values())[0]
        self.assertEqual(tr.status, "error")
        self.assertEqual(sp.status, "error")
        self.assertIn("fail", sp.error or "")

    def test_span_context_manager_ends_implicit_auto_trace(self):
        tracer = Tracer()
        self.assertIsNone(get_current_trace())
        with tracer.span("solo") as solo:
            auto_trace = get_current_trace()
            self.assertIsNotNone(auto_trace)
            self.assertEqual(solo.trace_id, auto_trace.trace_id)
        self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())
        self.assertEqual(auto_trace.status, "ok")

    def test_nested_trace_restores_outer_context(self):
        tracer = Tracer()
        with tracer.trace("outer") as outer_trace:
            with tracer.span("outer-span") as outer_span:
                with tracer.trace("inner") as inner_trace:
                    self.assertIs(get_current_trace(), inner_trace)
                    self.assertIsNone(get_current_span())
                    with tracer.span("inner-span"):
                        self.assertIs(get_current_trace(), inner_trace)
                self.assertIs(get_current_trace(), outer_trace)
                self.assertIs(get_current_span(), outer_span)
                with tracer.span("resumed-outer-span") as resumed_span:
                    self.assertEqual(resumed_span.trace_id, outer_trace.trace_id)
                    self.assertEqual(resumed_span.parent_span_id, outer_span.span_id)
        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_span())


if __name__ == "__main__":
    unittest.main()
