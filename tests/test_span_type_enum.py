import unittest

from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.span import Span, SpanType, normalize_span_type
from tracekit.core.tracer import Tracer


class TestSpanTypeEnum(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_enum_values_normalization_setter_and_serialization(self):
        self.assertEqual(
            {span_type.value for span_type in SpanType},
            {
                "llm",
                "tool",
                "chain",
                "retriever",
                "embedding",
                "agent",
                "internal",
                "database",
                "http",
            },
        )
        self.assertIs(normalize_span_type("  LLM  "), SpanType.LLM)
        self.assertIs(normalize_span_type("Tool"), SpanType.TOOL)
        self.assertIs(normalize_span_type(" DATABASE "), SpanType.DATABASE)
        self.assertIs(normalize_span_type("http"), SpanType.HTTP)
        self.assertIs(normalize_span_type(SpanType.CHAIN), SpanType.CHAIN)
        with self.assertRaises(ValueError):
            normalize_span_type("unknown")

        span = Span(name="typed", trace_id="trace")
        span.type = "  TOOL  "
        self.assertIs(span.type, SpanType.TOOL)
        self.assertEqual(span.to_dict()["span_type"], "tool")

    def test_end_trace_aggregates_llm_span_enum_metrics(self):
        tracer = Tracer()
        trace = tracer.start_trace("llm-aggregation")
        llm_span = tracer.start_llm_span(prompt="hello")
        self.assertIs(llm_span.span_type, SpanType.LLM)
        tracer.record_llm_result(
            llm_span,
            response="world",
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
        )
        tracer.end_span(llm_span)
        tracer.end_trace(trace)

        self.assertEqual(trace.metadata["prompt"], "hello")
        self.assertEqual(trace.metadata["response"], "world")
        self.assertEqual(trace.metadata["total_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
