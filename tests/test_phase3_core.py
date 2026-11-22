"""Phase 3 core: W3C propagation, sampling, and auto-flush."""

from __future__ import annotations

import asyncio
import unittest

from tracekit import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    ParentBasedSampler,
    RateLimitSampler,
    SpanContext,
    Tracer,
    extract,
    format_traceparent,
    inject,
    parse_traceparent,
)
from tracekit.core.storage import StorageBackend


class FakeStorage(StorageBackend):
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}
        self._connected = False
        self.closed = False

    async def initialize(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self.closed = True
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def storage_type(self) -> str:
        return "fake"

    async def insert_one(self, collection: str, document: dict) -> str:
        key = document.get("trace_id") or document.get("span_id") or document.get("id")
        self.collections.setdefault(collection, {})[str(key)] = dict(document)
        return str(key)

    async def find_one(self, collection: str, filter: dict | None = None) -> dict | None:
        rows = await self.find_many(collection, filter=filter, limit=1)
        return rows[0] if rows else None

    async def find_many(
        self,
        collection: str,
        filter: dict | None = None,
        sort: list | None = None,
        limit: int | None = None,
        skip: int = 0,
    ) -> list[dict]:
        rows = [dict(v) for v in self.collections.get(collection, {}).values()]
        if filter:
            rows = [r for r in rows if all(r.get(k) == v for k, v in filter.items())]
        return rows[skip : skip + limit if limit else None]

    async def count_documents(self, collection: str, filter: dict | None = None) -> int:
        return len(await self.find_many(collection, filter=filter))

    async def create_index(
        self, collection: str, key_or_list: str | list, unique: bool = False
    ) -> None:
        return None

    @property
    def storage_detail(self) -> str:
        return "fake"

    async def update_one(self, collection: str, filter: dict, update: dict) -> int:
        rows = self.collections.get(collection, {})
        for key, row in rows.items():
            if all(row.get(k) == v for k, v in filter.items()):
                row.update(update)
                return 1
        return 0

    async def get_stats(self) -> dict:
        return {k: len(v) for k, v in self.collections.items()}


class TestTraceparentCodec(unittest.TestCase):
    def test_format_and_parse_roundtrip(self) -> None:
        ctx = SpanContext(trace_id="a" * 32, span_id="b" * 16, sampled=True)
        value = format_traceparent(ctx)
        self.assertEqual(value, "00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        parsed = parse_traceparent(value)
        self.assertEqual(parsed, ctx)

    def test_unsampled_flag(self) -> None:
        ctx = SpanContext(trace_id="1" * 32, span_id="2" * 16, sampled=False)
        self.assertTrue(format_traceparent(ctx).endswith("-00"))
        self.assertFalse(parse_traceparent(format_traceparent(ctx)).sampled)

    def test_invalid_values_rejected(self) -> None:
        invalid = [
            None,
            "",
            "00-" + "a" * 32 + "-" + "b" * 16,
            "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
            "00-" + "z" * 32 + "-" + "b" * 16 + "-01",
            "00-" + "a" * 32 + "-" + "b" * 16 + "-zz",
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(parse_traceparent(value))

    def test_format_rejects_bad_ids(self) -> None:
        with self.assertRaises(ValueError):
            format_traceparent(SpanContext(trace_id="short", span_id="b" * 16))
        with self.assertRaises(ValueError):
            format_traceparent(SpanContext(trace_id="a" * 32, span_id="short"))


class TestPropagationFlow(unittest.TestCase):
    def test_inject_and_extract_ignore_header_case(self) -> None:
        tracer = Tracer()
        trace = tracer.start_trace("root")
        span = tracer.start_span("child")
        headers: dict[str, str] = {}
        inject(headers)
        self.assertIn("traceparent", headers)
        parsed = extract({"TraceParent": headers["traceparent"]})
        self.assertEqual(parsed.trace_id, trace.trace_id)
        self.assertEqual(parsed.span_id, span.span_id)
        self.assertTrue(parsed.sampled)
        tracer.end_span(span)
        tracer.end_trace(trace)

    def test_start_trace_from_remote_context(self) -> None:
        remote = SpanContext(trace_id="c" * 32, span_id="d" * 16, sampled=True)
        tracer = Tracer()
        trace = tracer.start_trace("continued", parent_context=remote)
        self.assertEqual(trace.trace_id, remote.trace_id)
        span = tracer.start_span("work")
        self.assertEqual(span.trace_id, remote.trace_id)
        self.assertIsNone(span.parent_span_id)
        tracer.end_span(span)
        tracer.end_trace(trace)

    def test_start_span_links_remote_parent(self) -> None:
        remote = SpanContext(trace_id="e" * 32, span_id="f" * 16, sampled=True)
        tracer = Tracer()
        span = tracer.start_span("remote-child", parent_context=remote)
        self.assertEqual(span.trace_id, remote.trace_id)
        self.assertEqual(span.parent_span_id, remote.span_id)
        tracer.end_span(span)
        # The implicit remote trace is finalized once its last span ends.
        remote_trace = tracer.get_trace(remote.trace_id)
        self.assertIsNotNone(remote_trace)
        self.assertNotEqual(remote_trace.status, "running")
        self.assertIsNotNone(remote_trace.end_time)

    def test_continue_trace_helper(self) -> None:
        headers: dict[str, str] = {}
        upstream = Tracer()
        upstream_trace = upstream.start_trace("upstream")
        upstream_span = upstream.start_span("call")
        inject(headers)
        tracer = Tracer()
        trace = tracer.continue_trace(headers, name="downstream")
        self.assertEqual(trace.trace_id, upstream_trace.trace_id)
        self.assertTrue(trace.metadata["sampled"])
        span = tracer.start_span("work", parent_context=extract(headers))
        self.assertEqual(span.parent_span_id, upstream_span.span_id)
        tracer.end_span(span)
        tracer.end_trace(trace)
        # Ending the downstream trace restores the upstream context.
        upstream.end_span(upstream_span)
        upstream.end_trace(upstream_trace)

    def test_mismatched_parent_context_raises(self) -> None:
        tracer = Tracer()
        trace = tracer.start_trace("local")
        remote = SpanContext(trace_id="9" * 32, span_id="8" * 16)
        try:
            with self.assertRaises(ValueError):
                tracer.start_span("bad", parent_context=remote)
        finally:
            tracer.end_trace(trace)

    def test_inject_without_active_span_is_noop(self) -> None:
        headers = {"traceparent": "keep"}
        tracer = Tracer()
        trace = tracer.start_trace("no-span")
        inject(headers)
        self.assertEqual(headers, {"traceparent": "keep"})
        tracer.end_trace(trace)

    def test_remote_context_satisfies_require_trace(self) -> None:
        remote = SpanContext(trace_id="a" * 32, span_id="b" * 16, sampled=True)
        tracer = Tracer(require_trace=True)
        span = tracer.start_span("remote", parent_context=remote)
        self.assertEqual(span.trace_id, remote.trace_id)
        tracer.end_span(span)


class TestAutomaticEvents(unittest.TestCase):
    def test_llm_tool_and_retry_events(self) -> None:
        tracer = Tracer()
        trace = tracer.start_trace("events")
        llm = tracer.start_llm_span("llm", model="m")
        tracer.record_llm_result(
            llm, response="ok", prompt_tokens=3, completion_tokens=4, total_tokens=7
        )
        self.assertEqual(llm.events[-1].name, "llm.result")
        self.assertEqual(llm.events[-1].attributes["total_tokens"], 7)
        tool = tracer.start_tool_span("tool")
        tracer.record_tool_result(tool, error="boom")
        self.assertEqual(tool.events[-1].name, "tool.error")
        self.assertEqual(tool.events[-1].attributes["error"], "boom")
        tracer.record_retry(tool, attempt=2, reason="timeout")
        self.assertEqual(tool.events[-1].name, "retry")
        self.assertEqual(tool.events[-1].attributes["attempt"], 2)
        self.assertEqual(tool.attributes["retry_count"], 1)
        tracer.end_span(tool)
        tracer.end_span(llm)
        tracer.end_trace(trace)


class TestSampling(unittest.IsolatedAsyncioTestCase):
    def test_always_on_and_off_traces(self) -> None:
        on = Tracer(sampler=AlwaysOnSampler())
        tr = on.start_trace("on")
        self.assertTrue(tr.metadata["sampled"])
        self.assertIs(on.get_trace(tr.trace_id), tr)
        on.end_trace(tr)

        off = Tracer(sampler=AlwaysOffSampler())
        tr2 = off.start_trace("off")
        self.assertFalse(tr2.metadata["sampled"])
        self.assertIsNone(off.get_trace(tr2.trace_id))
        span = off.start_span("child")
        self.assertEqual(span.attributes.get("sampled"), False)
        self.assertNotIn(span.span_id, off._spans)
        off.end_span(span)
        off.end_trace(tr2)

    def test_rate_limit_extremes_and_determinism(self) -> None:
        self.assertFalse(RateLimitSampler(0.0).should_sample(trace_id="a" * 32))
        self.assertTrue(RateLimitSampler(1.0).should_sample(trace_id="a" * 32))
        sampler = RateLimitSampler(0.5, salt="test")
        decisions = {sampler.should_sample(trace_id=f"{i:032x}") for i in range(40)}
        self.assertEqual(decisions, {True, False})
        self.assertEqual(
            sampler.should_sample(trace_id="f" * 32),
            sampler.should_sample(trace_id="f" * 32),
        )
        with self.assertRaises(ValueError):
            RateLimitSampler(1.5)

    def test_parent_based_sampler(self) -> None:
        sampler = ParentBasedSampler(root=AlwaysOffSampler())
        self.assertFalse(sampler.should_sample(trace_id="a" * 32))
        self.assertTrue(
            sampler.should_sample(trace_id="a" * 32, parent_sampled=True)
        )
        self.assertFalse(
            sampler.should_sample(trace_id="a" * 32, parent_sampled=False)
        )

    def test_parent_sampling_propagates_to_children(self) -> None:
        remote = SpanContext(trace_id="b" * 32, span_id="c" * 16, sampled=False)
        tracer = Tracer(sampler=AlwaysOnSampler())
        trace = tracer.start_trace("remote", parent_context=remote)
        self.assertFalse(trace.metadata["sampled"])
        span = tracer.start_span("child", parent_context=remote)
        self.assertEqual(span.attributes.get("sampled"), False)
        self.assertNotIn(span.span_id, tracer._spans)
        tracer.end_span(span)
        tracer.end_trace(trace)

    async def test_auto_flush_on_trace_end(self) -> None:
        storage = FakeStorage()
        tracer = Tracer(storage=storage, auto_flush=True, flush_batch_size=1)
        with tracer.trace("auto"):
            tracer.start_span("child")
        await asyncio.sleep(0.05)
        self.assertEqual(len(storage.collections.get("traces", {})), 1)
        self.assertEqual(len(storage.collections.get("spans", {})), 1)
        await tracer.close()
        self.assertTrue(storage.closed)

    async def test_flush_interval_background_loop(self) -> None:
        storage = FakeStorage()
        tracer = Tracer(storage=storage, flush_interval=0.01)
        trace = tracer.start_trace("periodic")
        tracer.start_span("work")
        tracer.end_trace(trace)
        await tracer.start_flush_loop()
        await asyncio.sleep(0.05)
        await tracer.stop_flush_loop()
        self.assertGreaterEqual(len(storage.collections.get("traces", {})), 1)
        self.assertGreaterEqual(len(storage.collections.get("spans", {})), 1)

    async def test_flush_interval_autostarts_for_async_traces(self) -> None:
        storage = FakeStorage()
        tracer = Tracer(storage=storage, flush_interval=0.01)
        trace = tracer.start_trace("auto-loop")
        tracer.start_span("work")
        tracer.end_trace(trace)
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(len(storage.collections.get("traces", {})), 1)
        await tracer.close()

    async def test_unsampled_data_is_not_flushed(self) -> None:
        storage = FakeStorage()
        tracer = Tracer(storage=storage, sampler=AlwaysOffSampler(), auto_flush=True)
        trace = tracer.start_trace("off")
        tracer.start_span("child")
        tracer.end_trace(trace)
        await asyncio.sleep(0.02)
        await tracer.flush()
        self.assertEqual(storage.collections, {})


if __name__ == "__main__":
    unittest.main()
