import unittest
from dataclasses import fields

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
    set_trace_context,
)
from tracekit.core.noop import NoOpSpan
from tracekit.core.propagation import (
    SpanContext,
    TraceContext,
    as_parent_context,
    current_span_context,
    extract,
    format_traceparent,
    inject,
    parent_context_kwargs,
    parse_traceparent,
)
from tracekit.core.sampling import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    ParentBasedSampler,
    RateLimitedSampler,
    RateLimitingSampler,
    RateLimitSampler,
    TraceIdRatioSampler,
)
from tracekit.core.tracer import Tracer


class ContextIsolation(unittest.TestCase):
    def setUp(self):
        self.clear_context()

    def tearDown(self):
        self.clear_context()

    @staticmethod
    def clear_context():
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)


class TestTraceContextPropagation(ContextIsolation):
    def test_trace_context_dataclass_and_span_context_compatibility(self):
        self.assertIs(SpanContext, TraceContext)
        self.assertEqual(
            {field.name for field in fields(TraceContext)},
            {"trace_id", "span_id", "sampled", "trace_state"},
        )
        context = SpanContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            sampled=False,
            tracestate="vendor=value",
        )
        self.assertEqual(context.trace_state, "vendor=value")
        self.assertEqual(context.tracestate, "vendor=value")
        self.assertFalse(context.sampled)

    def test_traceparent_codec_and_strict_validation(self):
        context = TraceContext("a" * 32, "b" * 16, sampled=True)
        self.assertEqual(
            format_traceparent(context),
            "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        )
        self.assertEqual(parse_traceparent(format_traceparent(context)), context)

        unsampled = TraceContext("c" * 32, "d" * 16, sampled=False)
        self.assertEqual(format_traceparent(unsampled)[-2:], "00")
        self.assertFalse(parse_traceparent(format_traceparent(unsampled)).sampled)

        invalid_values = (
            None,
            "",
            "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
            "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "b" * 16 + "-zz",
            "00-" + "A" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "b" * 16 + "-01-extra",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(parse_traceparent(value))
        for trace_id, span_id in (("bad", "b" * 16), ("a" * 32, "0" * 16)):
            with self.subTest(trace_id=trace_id, span_id=span_id):
                with self.assertRaises(ValueError):
                    format_traceparent(TraceContext(trace_id, span_id))

    def test_extract_is_case_insensitive_safe_and_carries_tracestate(self):
        remote = TraceContext(
            "e" * 32,
            "f" * 16,
            sampled=True,
        )
        headers = {
            "TraceParent": format_traceparent(remote),
            "TraceState": "vendor=value",
        }
        extracted = extract(headers)
        self.assertEqual(extracted.trace_id, remote.trace_id)
        self.assertEqual(extracted.span_id, remote.span_id)
        self.assertEqual(extracted.trace_state, "vendor=value")
        self.assertIsNone(extract(None))
        self.assertIsNone(extract({"TraceParent": object()}))
        self.assertIsNone(extract({"TraceParent": "malformed"}))

    def test_inject_uses_active_span_and_preserves_unrelated_headers(self):
        tracer = Tracer()
        trace = tracer.start_trace("inject", tracestate="vendor=active")
        span = tracer.start_span("active")
        headers = {
            "X-Request-ID": "keep-me",
            "TraceParent": "stale",
            "TrAcEsTaTe": "stale=value",
        }
        result = inject(headers)

        self.assertIsNone(result)
        self.assertEqual(headers["X-Request-ID"], "keep-me")
        self.assertEqual(headers["traceparent"], format_traceparent(
            TraceContext(trace.trace_id, span.span_id, trace_state="vendor=active")
        ))
        self.assertEqual(headers["tracestate"], "vendor=active")
        self.assertNotIn("TraceParent", headers)
        self.assertNotIn("TrAcEsTaTe", headers)
        self.assertEqual(extract(headers).trace_state, "vendor=active")
        tracer.end_span(span)
        tracer.end_trace(trace)

    def test_inject_explicit_remote_context_and_parent_helpers(self):
        remote = TraceContext(
            "1" * 32,
            "2" * 16,
            sampled=False,
            trace_state="vendor=remote",
        )
        headers = {"X-Request-ID": "preserve"}
        self.assertIsNone(inject(headers, remote))
        self.assertEqual(headers["X-Request-ID"], "preserve")
        self.assertEqual(extract(headers), remote)

        parent_context = as_parent_context(remote)
        self.assertEqual(parent_context, remote)
        self.assertEqual(parent_context_kwargs(remote), {"parent_context": remote})
        tracer = Tracer()
        span = tracer.start_span("remote-child", **parent_context_kwargs(remote))
        self.assertEqual(span.trace_id, remote.trace_id)
        self.assertEqual(span.parent_span_id, remote.span_id)
        self.assertFalse(span.attributes["sampled"])
        tracer.end_span(span)


class TestSampling(ContextIsolation):
    def test_sampler_decisions_are_deterministic_and_parent_aware(self):
        trace_id = "a" * 32
        self.assertTrue(AlwaysOnSampler().should_sample(trace_id))
        self.assertFalse(AlwaysOffSampler().should_sample(trace_id))

        ratio = TraceIdRatioSampler(rate=0.1)
        self.assertEqual(
            ratio.should_sample(trace_id),
            ratio.should_sample(trace_id),
        )
        self.assertFalse(TraceIdRatioSampler(0.0).should_sample(trace_id))
        self.assertTrue(TraceIdRatioSampler(1.0).should_sample(trace_id))
        self.assertIs(RateLimitedSampler, RateLimitSampler)
        self.assertIs(RateLimitingSampler, RateLimitSampler)
        self.assertTrue(RateLimitSampler(1.0).should_sample(trace_id))
        with self.assertRaises(ValueError):
            TraceIdRatioSampler(1.1)

        root_off = ParentBasedSampler(root=AlwaysOffSampler())
        self.assertFalse(root_off.should_sample(trace_id))
        self.assertFalse(root_off.should_sample(trace_id, parent_sampled=False))
        self.assertTrue(root_off.should_sample(trace_id, parent_sampled=True))
        root_on = ParentBasedSampler(root=AlwaysOnSampler())
        self.assertTrue(root_on.should_sample(trace_id))

    def test_local_sampling_decisions_propagate_to_child_spans(self):
        off_tracer = Tracer(sampler=ParentBasedSampler(root=AlwaysOffSampler()))
        off_trace = off_tracer.start_trace("local-off")
        off_child = off_tracer.start_span("off-child")
        self.assertFalse(off_trace.metadata["sampled"])
        self.assertFalse(off_child.attributes["sampled"])
        self.assertNotIn(off_trace.trace_id, off_tracer._traces)
        self.assertNotIn(off_child.span_id, off_tracer._spans)
        off_tracer.end_span(off_child)
        off_tracer.end_trace(off_trace)

        on_tracer = Tracer(sampler=AlwaysOnSampler())
        on_trace = on_tracer.start_trace("local-on")
        on_child = on_tracer.start_span("on-child")
        self.assertTrue(on_trace.metadata["sampled"])
        self.assertIn(on_trace.trace_id, on_tracer._traces)
        self.assertIn(on_child.span_id, on_tracer._spans)
        on_tracer.end_span(on_child)
        on_tracer.end_trace(on_trace)

    def test_parent_based_sampling_respects_remote_trace_decision(self):
        unsampled_parent = TraceContext(
            "3" * 32,
            "4" * 16,
            sampled=False,
        )
        sampled_parent = TraceContext(
            "5" * 32,
            "6" * 16,
            sampled=True,
        )
        sampler = ParentBasedSampler(root=AlwaysOnSampler())
        self.assertFalse(
            sampler.should_sample(
                unsampled_parent.trace_id,
                parent_sampled=unsampled_parent.sampled,
            )
        )
        self.assertTrue(
            sampler.should_sample(
                sampled_parent.trace_id,
                parent_sampled=sampled_parent.sampled,
            )
        )

        tracer = Tracer(sampler=sampler)
        remote_trace = tracer.start_trace(
            "remote-unsampled",
            parent_context=unsampled_parent,
        )
        self.assertFalse(remote_trace.metadata["sampled"])
        self.assertNotIn(remote_trace.trace_id, tracer._traces)
        child = tracer.start_span("remote-child")
        self.assertFalse(child.attributes["sampled"])
        self.assertNotIn(child.span_id, tracer._spans)
        tracer.end_span(child)
        tracer.end_trace(remote_trace)


class TestNoOpSpan(ContextIsolation):
    def test_noop_span_preserves_span_api_without_recording(self):
        span = NoOpSpan(name="dropped", trace_id="trace-id")
        self.assertFalse(span.is_recording)
        self.assertIs(span.start(), span)
        self.assertIs(span.end(status="ok"), span)
        self.assertIsNone(span.add_event("ignored", {"key": "value"}))
        self.assertIsNone(span.record_exception(RuntimeError("ignored")))
        self.assertIsNone(span.set_attribute("ignored", True))
        self.assertEqual(span.events, [])
        self.assertEqual(span.attributes, {})
        self.assertEqual(span.status, "running")
        self.assertNotIn("storage", vars(span))
        self.assertEqual(span.to_dict()["events"], [])


if __name__ == "__main__":
    unittest.main()
