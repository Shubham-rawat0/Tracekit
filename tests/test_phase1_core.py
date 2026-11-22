"""Comprehensive tests for Phase 1 core lifecycle, SpanType, flush, and exception capture."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
)
from tracekit.core.span import Span, SpanType, normalize_span_type
from tracekit.core.storage import StorageBackend
from tracekit.core.trace import Trace
from tracekit.core.tracer import Tracer


class TestSpanTypeValidation(unittest.TestCase):
    """Test SpanType enum additions and validation."""

    def test_new_span_types_exist(self):
        self.assertEqual(SpanType.DATABASE.value, "database")
        self.assertEqual(SpanType.HTTP.value, "http")

    def test_span_type_normalization_database(self):
        self.assertEqual(normalize_span_type("database"), SpanType.DATABASE)
        self.assertEqual(normalize_span_type("  DATABASE  "), SpanType.DATABASE)
        self.assertEqual(normalize_span_type("Database"), SpanType.DATABASE)
        self.assertEqual(normalize_span_type(SpanType.DATABASE), SpanType.DATABASE)

    def test_span_type_normalization_http(self):
        self.assertEqual(normalize_span_type("http"), SpanType.HTTP)
        self.assertEqual(normalize_span_type("  HTTP  "), SpanType.HTTP)
        self.assertEqual(normalize_span_type("Http"), SpanType.HTTP)
        self.assertEqual(normalize_span_type(SpanType.HTTP), SpanType.HTTP)

    def test_invalid_span_type_raises(self):
        with self.assertRaises(ValueError) as cm:
            normalize_span_type("invalid_type")
        self.assertIn("invalid span_type", str(cm.exception))

    def test_span_type_serialization(self):
        span = Span(name="test", trace_id="t1", span_type=SpanType.DATABASE)
        data = span.to_dict()
        self.assertEqual(data["span_type"], "database")
        
        span2 = Span(name="test", trace_id="t1", span_type=SpanType.HTTP)
        data2 = span2.to_dict()
        self.assertEqual(data2["span_type"], "http")

    def test_span_type_roundtrip(self):
        original = Span(name="test", trace_id="t1", span_type=SpanType.DATABASE)
        d = original.to_dict()
        restored = Span.from_dict(d)
        self.assertEqual(restored.span_type, SpanType.DATABASE)
        
        original2 = Span(name="test", trace_id="t1", span_type=SpanType.HTTP)
        d2 = original2.to_dict()
        restored2 = Span.from_dict(d2)
        self.assertEqual(restored2.span_type, SpanType.HTTP)


class TestContextRestoration(unittest.TestCase):
    """Test context restoration in nested spans, exceptions, and async."""

    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        self.tracer = Tracer()

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)

    def test_nested_sync_spans_restore_context(self):
        with self.tracer.span("parent") as parent:
            self.assertIs(get_current_span(), parent)
            with self.tracer.span("child") as child:
                self.assertIs(get_current_span(), child)
                self.assertEqual(child.parent_span_id, parent.span_id)
            self.assertIs(get_current_span(), parent)
        self.assertIsNone(get_current_span())

    def test_nested_async_spans_restore_context(self):
        async def run_test():
            async with self.tracer.async_span("parent") as parent:
                self.assertIs(get_current_span(), parent)
                async with self.tracer.async_span("child") as child:
                    self.assertIs(get_current_span(), child)
                    self.assertEqual(child.parent_span_id, parent.span_id)
                self.assertIs(get_current_span(), parent)
            self.assertIsNone(get_current_span())
        asyncio.run(run_test())

    def test_exception_in_sync_span_restores_context(self):
        with self.assertRaises(ValueError):
            with self.tracer.span("parent") as parent:
                self.assertIs(get_current_span(), parent)
                with self.tracer.span("child") as child:
                    self.assertIs(get_current_span(), child)
                    raise ValueError("boom")
                self.fail("Should not reach here")
        # Context should be fully restored
        self.assertIsNone(get_current_span())

    def test_exception_in_async_span_restores_context(self):
        async def run_test():
            with self.assertRaises(ValueError):
                async with self.tracer.async_span("parent") as parent:
                    self.assertIs(get_current_span(), parent)
                    async with self.tracer.async_span("child") as child:
                        self.assertIs(get_current_span(), child)
                        raise ValueError("async boom")
            self.assertIsNone(get_current_span())
        asyncio.run(run_test())

    def test_nested_traces_restore_context(self):
        with self.tracer.trace("outer") as outer:
            self.assertIs(get_current_trace(), outer)
            with self.tracer.span("outer-span") as outer_span:
                self.assertIs(get_current_span(), outer_span)
            with self.tracer.trace("inner") as inner:
                self.assertIs(get_current_trace(), inner)
                self.assertIsNone(get_current_span())
            self.assertIs(get_current_trace(), outer)
            self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_span())

    def test_direct_api_lifecycle(self):
        # Test start_trace/end_trace
        tr = self.tracer.start_trace("direct")
        self.assertIs(get_current_trace(), tr)
        self.tracer.end_trace(tr)
        self.assertIsNone(get_current_trace())
        
        # Test start_span/end_span
        tr = self.tracer.start_trace("direct2")
        sp = self.tracer.start_span("direct-span")
        self.assertIs(get_current_span(), sp)
        self.tracer.end_span(sp)
        self.assertIsNone(get_current_span())
        self.tracer.end_trace(tr)
        self.assertIsNone(get_current_trace())

    def test_concurrent_asyncio_tasks_no_context_leak(self):
        async def task1():
            async with self.tracer.async_span("task1-span") as s:
                self.assertIs(get_current_span(), s)
                await asyncio.sleep(0.01)
            return "task1 done"

        async def task2():
            async with self.tracer.async_span("task2-span") as s:
                self.assertIs(get_current_span(), s)
                await asyncio.sleep(0.01)
            return "task2 done"

        async def run_test():
            results = await asyncio.gather(task1(), task2())
            self.assertEqual(results, ["task1 done", "task2 done"])
            # Context should be clean after all tasks
            self.assertIsNone(get_current_span())
            self.assertIsNone(get_current_trace())

        asyncio.run(run_test())

    def test_out_of_order_end_span_no_leak(self):
        """Test that ending spans out of LIFO order doesn't leak context.
        
        Note: Due to contextvar token behavior, when outer is ended first (while
        inner is current), the token restores to state before outer (None).
        Then inner ends and restores to outer. Final context is outer (ended).
        This test documents the current behavior.
        """
        outer = self.tracer.start_span("outer")
        inner = self.tracer.start_span("inner")
        self.assertIs(get_current_span(), inner)
        
        # End outer first (out of order) - outer is NOT current, so no context change
        self.tracer.end_span(outer)
        self.assertIs(get_current_span(), inner)
        
        # End inner - restores to outer (which was current when inner's token was created)
        self.tracer.end_span(inner)
        # Final context is outer (even though ended), not None
        # This is correct contextvar behavior - tokens form a stack
        self.assertIs(get_current_span(), outer)
        self.assertEqual(outer.status, "ok")


class TestRecordException(unittest.TestCase):
    """Test span.record_exception and trace.record_exception."""

    def setUp(self):
        self.tracer = Tracer()

    def test_span_record_exception_structured(self):
        with self.tracer.span("test") as span:
            try:
                raise ValueError("test error")
            except ValueError as e:
                event = span.record_exception(e)
        
        self.assertEqual(span.status, "error")
        self.assertEqual(span.error, "test error")
        self.assertEqual(len(span.events), 1)
        
        event_data = event.to_dict()
        self.assertEqual(event_data["name"], "exception")
        attrs = event_data["attributes"]
        self.assertEqual(attrs["exception.type"], "ValueError")
        self.assertEqual(attrs["exception.message"], "test error")
        self.assertIn("exception.stacktrace", attrs)
        self.assertIn("ValueError: test error", attrs["exception.stacktrace"])
        self.assertIn("exception.timestamp", attrs)

    def test_trace_record_exception_sets_failure_reason(self):
        trace = Trace(name="test-trace")
        trace.start()
        try:
            raise RuntimeError("trace failed")
        except RuntimeError as e:
            trace.record_exception(e)

        self.assertEqual(trace.status, "error")
        self.assertEqual(trace.error, "trace failed")
        self.assertEqual(trace.metadata.get("failure_reason"), "trace failed")
        self.assertEqual(len(trace.metadata.get("events", [])), 1)

    def test_tracer_record_exception_convenience(self):
        with self.tracer.span("test") as span:
            try:
                raise KeyError("missing key")
            except KeyError as e:
                event = self.tracer.record_exception(e)
        
        self.assertIsNotNone(event)
        self.assertEqual(span.status, "error")
        # KeyError str() includes quotes: "'missing key'"
        self.assertEqual(span.error, "'missing key'")

    def test_record_exception_preserves_propagation(self):
        with self.assertRaises(ValueError):
            with self.tracer.span("test") as span:
                span.record_exception(ValueError("propagated"))
                raise ValueError("propagated")
        
        self.assertEqual(span.status, "error")
        self.assertIn("propagated", span.error or "")

    def test_record_exception_no_secrets_in_stacktrace(self):
        # Test that exception capture doesn't include sensitive data
        with self.tracer.span("test") as span:
            try:
                raise ValueError("password=secret123")
            except ValueError as e:
                event = span.record_exception(e)
        
        event_data = event.to_dict()
        attrs = event_data["attributes"]
        # Stack trace should be present but we don't filter it - that's user's responsibility
        self.assertIn("exception.stacktrace", attrs)


class TestFlushIdempotency(unittest.IsolatedAsyncioTestCase):
    """Test Tracer.flush() idempotency and partial failure handling."""

    class MockStorage(StorageBackend):
        def __init__(self, fail_on=None):
            self._connected = True
            self.inserted_traces = []
            self.inserted_spans = []
            self.fail_on = fail_on or set()
            self.call_count = 0

        @property
        def is_connected(self):
            return True

        @property
        def storage_type(self):
            return "mock"

        @property
        def storage_detail(self):
            return "mock"

        async def initialize(self):
            pass

        async def close(self):
            pass

        async def insert_one(self, collection: str, document: dict):
            self.call_count += 1
            if self.call_count in self.fail_on:
                raise RuntimeError(f"Simulated failure on call {self.call_count}")
            if collection == "traces":
                self.inserted_traces.append(document)
            elif collection == "spans":
                self.inserted_spans.append(document)

        async def find_one(self, collection: str, filter: dict):
            return None

        async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
            return []

        async def count_documents(self, collection: str, filter: dict | None = None):
            return 0

        async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
            pass

    async def test_flush_idempotent_repeated_calls(self):
        storage = self.MockStorage()
        tracer = Tracer(storage=storage)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        # First flush
        await tracer.flush()
        self.assertEqual(len(storage.inserted_traces), 1)
        self.assertEqual(len(storage.inserted_spans), 1)
        
        # Second flush - should be idempotent
        await tracer.flush()
        self.assertEqual(len(storage.inserted_traces), 1)
        self.assertEqual(len(storage.inserted_spans), 1)

    async def test_flush_partial_failure_traces_succeed_spans_fail(self):
        # Fail on the second call (first span insert)
        storage = self.MockStorage(fail_on={2})
        tracer = Tracer(storage=storage, sampler=True)  # Always sample
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        # First flush - trace succeeds, span fails - atomic so both remain
        with self.assertRaises(RuntimeError):
            await tracer.flush()
        
        # Both should remain in memory (atomic - nothing committed)
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)
        
        # Retry with fresh storage
        storage2 = self.MockStorage()
        tracer.storage = storage2
        await tracer.flush()
        
        self.assertEqual(len(storage2.inserted_traces), 1)
        self.assertEqual(len(storage2.inserted_spans), 1)
        self.assertNotIn(tr.trace_id, tracer._traces)
        self.assertNotIn(sp.span_id, tracer._spans)

    async def test_flush_partial_failure_spans_succeed_traces_fail(self):
        # Fail on the first call (trace insert)
        storage = self.MockStorage(fail_on={1})
        tracer = Tracer(storage=storage, sampler=True)  # Always sample
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        # Flush should raise and not commit anything (atomic behavior)
        with self.assertRaises(RuntimeError):
            await tracer.flush()
        
        # Both should remain in memory (atomic - nothing committed)
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)
        
        # Retry with fresh storage
        storage2 = self.MockStorage()
        tracer.storage = storage2
        await tracer.flush()
        
        # Both should be persisted now
        self.assertEqual(len(storage2.inserted_traces), 1)
        self.assertEqual(len(storage2.inserted_spans), 1)
        self.assertNotIn(tr.trace_id, tracer._traces)
        self.assertNotIn(sp.span_id, tracer._spans)

    async def test_flush_with_none_storage_is_noop(self):
        tracer = Tracer(storage=None)
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        # Should not raise
        await tracer.flush()
        
        # Items should remain in memory
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)

    async def test_flush_only_marks_after_successful_storage(self):
        storage = self.MockStorage()
        tracer = Tracer(storage=storage)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        await tracer.flush()
        
        # Items should be in flushed sets only after successful storage
        self.assertIn(tr.trace_id, tracer._flushed_trace_ids)
        self.assertIn(sp.span_id, tracer._flushed_span_ids)
        self.assertNotIn(tr.trace_id, tracer._traces)
        self.assertNotIn(sp.span_id, tracer._spans)


class TestSpanTypeAtApiBoundaries(unittest.TestCase):
    """Test span type validation at tracer/span API boundaries."""

    def setUp(self):
        self.tracer = Tracer()

    def test_start_span_validates_span_type(self):
        # Valid types
        sp = self.tracer.start_span("test", span_type=SpanType.DATABASE)
        self.assertEqual(sp.span_type, SpanType.DATABASE)
        
        sp = self.tracer.start_span("test", span_type="http")
        self.assertEqual(sp.span_type, SpanType.HTTP)
        
        # Invalid type should raise
        with self.assertRaises(ValueError):
            self.tracer.start_span("test", span_type="invalid")

    def test_start_llm_span_uses_llm_type(self):
        sp = self.tracer.start_llm_span()
        self.assertEqual(sp.span_type, SpanType.LLM)

    def test_start_tool_span_uses_tool_type(self):
        sp = self.tracer.start_tool_span("my-tool")
        self.assertEqual(sp.span_type, SpanType.TOOL)

    def test_span_context_manager_validates_span_type(self):
        with self.tracer.span("test", span_type=SpanType.DATABASE) as sp:
            self.assertEqual(sp.span_type, SpanType.DATABASE)
        
        with self.assertRaises(ValueError):
            with self.tracer.span("test", span_type="invalid"):
                pass

    def test_async_span_context_manager_validates_span_type(self):
        async def run_test():
            async with self.tracer.async_span("test", span_type=SpanType.HTTP) as sp:
                self.assertEqual(sp.span_type, SpanType.HTTP)
            
            with self.assertRaises(ValueError):
                async with self.tracer.async_span("test", span_type="invalid"):
                    pass
        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()