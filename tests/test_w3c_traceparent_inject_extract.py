import unittest

from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.propagation import (
    SpanContext,
    extract,
    format_traceparent,
    inject,
    parse_traceparent,
)
from tracekit.core.tracer import Tracer


class TestTraceparentCodec(unittest.TestCase):
    def test_format_parse_round_trip_and_unsampled_flag(self):
        sampled = SpanContext(trace_id="a" * 32, span_id="b" * 16)
        encoded = format_traceparent(sampled)
        self.assertEqual(parse_traceparent(encoded), sampled)

        unsampled = SpanContext(
            trace_id="c" * 32,
            span_id="d" * 16,
            sampled=False,
        )
        encoded_unsampled = format_traceparent(unsampled)
        self.assertTrue(encoded_unsampled.endswith("-00"))
        self.assertEqual(parse_traceparent(encoded_unsampled), unsampled)

    def test_invalid_values_are_rejected(self):
        for value in (
            None,
            "",
            "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
            "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "A" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "b" * 16 + "-zz",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_traceparent(value))

        with self.assertRaises(ValueError):
            format_traceparent(SpanContext(trace_id="short", span_id="b" * 16))
        with self.assertRaises(ValueError):
            format_traceparent(SpanContext(trace_id="a" * 32, span_id="short"))


class TestTraceparentPropagation(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_inject_extract_case_insensitive_and_remote_parent_link(self):
        remote = SpanContext(
            trace_id="e" * 32,
            span_id="f" * 16,
            sampled=True,
        )
        headers = {}
        inject(headers, remote)
        extracted = extract({"TraceParent": headers["traceparent"]})
        self.assertEqual(extracted, remote)

        tracer = Tracer()
        child = tracer.start_span("remote-child", parent_context=extracted)
        self.assertEqual(child.trace_id, remote.trace_id)
        self.assertEqual(child.parent_span_id, remote.span_id)
        tracer.end_span(child)

    def test_continue_trace_preserves_remote_trace_id(self):
        remote = SpanContext(
            trace_id="1" * 32,
            span_id="2" * 16,
            sampled=True,
        )
        headers = {"traceparent": format_traceparent(remote)}
        tracer = Tracer()

        trace = tracer.continue_trace(headers, name="continued")
        self.assertEqual(trace.trace_id, remote.trace_id)
        tracer.end_trace(trace)


if __name__ == "__main__":
    unittest.main()
