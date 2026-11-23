"""Adversarial edge-case tests for Phase 2 fixes."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tracekit.core.context import (
    get_current_trace,
    get_trace_context,
    reset_trace_context,
    set_current_span,
    set_current_trace,
    set_trace_context,
)
from tracekit.core.span import Span, SpanType, normalize_span_type
from tracekit.core.storage import StorageBackend
from tracekit.core.trace import Trace
from tracekit.core.tracer import Tracer


def clear_all_context():
    """Clear all contextvars."""
    set_current_trace(None)
    set_current_span(None)
    set_trace_context(None)


class TestSpanTypeEnum(unittest.TestCase):
    """2.1: SpanType enum - validation, normalization, round-trip."""

    def test_valid_span_types_pass(self):
        for st in SpanType:
            self.assertEqual(normalize_span_type(st), st)
            self.assertEqual(normalize_span_type(st.value), st)
            self.assertEqual(normalize_span_type(st.value.upper()), st)

    def test_invalid_span_type_raises_value_error(self):
        with self.assertRaises(ValueError) as cm:
            normalize_span_type("invalid_type")
        self.assertIn("invalid span_type", str(cm.exception))

    def test_case_and_whitespace_normalization(self):
        self.assertEqual(normalize_span_type("  LLM  "), SpanType.LLM)
        self.assertEqual(normalize_span_type("Tool"), SpanType.TOOL)
        self.assertEqual(normalize_span_type("cHaIn"), SpanType.CHAIN)

    def test_none_returns_internal(self):
        self.assertEqual(normalize_span_type(None), SpanType.INTERNAL)

    def test_type_property_getter_returns_enum(self):
        span = Span(name="test", trace_id="t1", span_type=SpanType.LLM)
        self.assertIs(span.type, SpanType.LLM)
        self.assertEqual(span.type, SpanType.LLM)

    def test_type_property_setter_normalizes_string(self):
        span = Span(name="test", trace_id="t1")
        span.type = "  TOOL  "
        self.assertIs(span.span_type, SpanType.TOOL)
        self.assertIs(span.type, SpanType.TOOL)

    def test_type_property_setter_accepts_enum(self):
        span = Span(name="test", trace_id="t1")
        span.type = SpanType.AGENT
        self.assertIs(span.span_type, SpanType.AGENT)

    def test_to_dict_serializes_enum_value(self):
        span = Span(name="test", trace_id="t1", span_type=SpanType.EMBEDDING)
        d = span.to_dict()
        self.assertEqual(d["span_type"], "embedding")
        self.assertIsInstance(d["span_type"], str)

    def test_from_dict_deserializes_legacy_type_key(self):
        data = {
            "name": "test",
            "trace_id": "t1",
            "span_id": "s1",
            "type": "llm",  # legacy key
        }
        span = Span.from_dict(data)
        self.assertIs(span.span_type, SpanType.LLM)

    def test_from_dict_prefers_span_type_over_type(self):
        data = {
            "name": "test",
            "trace_id": "t1",
            "span_id": "s1",
            "span_type": "tool",
            "type": "llm",  # should be ignored
        }
        span = Span.from_dict(data)
        self.assertIs(span.span_type, SpanType.TOOL)

    def test_from_dict_round_trip_preserves_enum(self):
        original = Span(name="test", trace_id="t1", span_type=SpanType.RETRIEVER)
        d = original.to_dict()
        restored = Span.from_dict(d)
        self.assertIs(restored.span_type, SpanType.RETRIEVER)

    def test_equality_with_plain_string_still_works(self):
        span = Span(name="test", trace_id="t1", span_type=SpanType.LLM)
        self.assertEqual(span.span_type, "llm")  # StrEnum equality
        self.assertEqual(span.type, "llm")


class TestFlushIdempotencyRetry(unittest.IsolatedAsyncioTestCase):
    """2.2: flush idempotency and retry behavior."""

    async def test_flush_skips_already_flushed_items(self):
        """Repeated flush must not duplicate rows - use fake storage that tracks calls."""
        insert_calls = []

        class FakeStorage(StorageBackend):
            def __init__(self):
                self._connected = True
                self.inserted = []

            @property
            def is_connected(self):
                return True

            @property
            def storage_type(self):
                return "fake"

            @property
            def storage_detail(self):
                return "fake"

            async def initialize(self):
                pass

            async def close(self):
                pass

            async def insert_one(self, collection: str, document: dict) -> None:
                insert_calls.append((collection, document["trace_id"] if collection == "traces" else document["span_id"]))
                self.inserted.append((collection, document))

            async def find_one(self, collection: str, filter: dict):
                return None

            async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
                return []

            async def count_documents(self, collection: str, filter: dict | None = None):
                return 0

            async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
                pass

        storage = FakeStorage()
        tracer = Tracer(storage=storage)

        # Create trace and spans
        tr = tracer.start_trace("test")
        sp1 = tracer.start_span("span1", trace=tr)
        sp2 = tracer.start_span("span2", trace=tr)
        tracer.end_span(sp1)
        tracer.end_span(sp2)
        tracer.end_trace(tr)

        # First flush
        await tracer.flush()
        first_call_count = len(insert_calls)

        # Second flush - should skip already flushed
        await tracer.flush()
        second_call_count = len(insert_calls)

        self.assertEqual(second_call_count, first_call_count, "flush should be idempotent - no duplicate inserts")

    async def test_flush_retries_failed_items(self):
        """Failed items must remain buffered and be retried on next flush.
        
        Fake storage fails on first flush globally, succeeds on second.
        """
        fail_once = {"done": False}

        class FailOnceStorage(StorageBackend):
            def __init__(self):
                self._connected = True

            @property
            def is_connected(self):
                return True

            @property
            def storage_type(self):
                return "fake"

            @property
            def storage_detail(self):
                return "fake"

            async def initialize(self):
                pass

            async def close(self):
                pass

            async def insert_one(self, collection: str, document: dict) -> None:
                if not fail_once["done"]:
                    fail_once["done"] = True
                    raise RuntimeError("first flush fails")
                # Second attempt succeeds

            async def find_one(self, collection: str, filter: dict):
                return None

            async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
                return []

            async def count_documents(self, collection: str, filter: dict | None = None):
                return 0

            async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
                pass

        storage = FailOnceStorage()
        tracer = Tracer(storage=storage)

        tr = tracer.start_trace("retry-test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)

        # First flush - should fail but keep items buffered
        with self.assertRaises(RuntimeError):
            await tracer.flush()

        # Items should still be in memory (not flushed)
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)

        # Second flush - should succeed
        await tracer.flush()

        # Items should now be removed from memory
        self.assertNotIn(tr.trace_id, tracer._traces)
        self.assertNotIn(sp.span_id, tracer._spans)

    async def test_flush_with_none_storage_is_noop(self):
        """flush() with storage=None should be a no-op and not crash."""
        tracer = Tracer(storage=None)  # No storage
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)

        # Should not raise
        await tracer.flush()

    async def test_sqlite_backend_row_counts_after_two_flushes(self):
        """Verify real SQLiteBackend row counts after two flushes."""
        try:
            import aiosqlite
        except ImportError:
            self.skipTest("aiosqlite not installed")

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            from backend.apps.db.sqllite import SQLiteBackend

            backend = SQLiteBackend(db_path=path)
            await backend.initialize()

            tracer = Tracer(storage=backend)
            tr = tracer.start_trace("sqlite-test")
            sp = tracer.start_span("span", trace=tr)
            tracer.end_span(sp)
            tracer.end_trace(tr)

            # First flush
            await tracer.flush()

            # Count rows
            trace_count = await backend.count_documents("traces", {})
            span_count = await backend.count_documents("spans", {})
            self.assertEqual(trace_count, 1)
            self.assertEqual(span_count, 1)

            # Second flush - should not duplicate
            await tracer.flush()

            trace_count2 = await backend.count_documents("traces", {})
            span_count2 = await backend.count_documents("spans", {})
            self.assertEqual(trace_count2, 1, "Second flush should not duplicate trace rows")
            self.assertEqual(span_count2, 1, "Second flush should not duplicate span rows")

            await backend.close()
        finally:
            for ext in ["", "-wal", "-shm"]:
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    async def test_new_trace_after_flush_persists(self):
        """Adding a new trace after a flush still persists it."""
        insert_calls = []

        class FakeStorage(StorageBackend):
            def __init__(self):
                self._connected = True

            @property
            def is_connected(self):
                return True

            @property
            def storage_type(self):
                return "fake"

            @property
            def storage_detail(self):
                return "fake"

            async def initialize(self):
                pass

            async def close(self):
                pass

            async def insert_one(self, collection: str, document: dict) -> None:
                insert_calls.append((collection, document))

            async def find_one(self, collection: str, filter: dict):
                return None

            async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
                return []

            async def count_documents(self, collection: str, filter: dict | None = None):
                return 0

            async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
                pass

        storage = FakeStorage()
        tracer = Tracer(storage=storage)

        # First trace
        tr1 = tracer.start_trace("trace1")
        sp1 = tracer.start_span("span1", trace=tr1)
        tracer.end_span(sp1)
        tracer.end_trace(tr1)
        await tracer.flush()

        # Add new trace after flush
        tr2 = tracer.start_trace("trace2")
        sp2 = tracer.start_span("span2", trace=tr2)
        tracer.end_span(sp2)
        tracer.end_trace(tr2)
        await tracer.flush()

        # Should have 2 traces and 2 spans total
        trace_inserts = [c for c in insert_calls if c[0] == "traces"]
        span_inserts = [c for c in insert_calls if c[0] == "spans"]
        self.assertEqual(len(trace_inserts), 2)
        self.assertEqual(len(span_inserts), 2)


class TestRequireTrace(unittest.TestCase):
    """2.3: require_trace - constructor default, per-call override, context managers."""

    def setUp(self):
        clear_all_context()

    def tearDown(self):
        clear_all_context()

    def test_constructor_default_require_trace_false(self):
        tracer = Tracer()
        self.assertFalse(tracer.require_trace)

    def test_constructor_require_trace_true(self):
        tracer = Tracer(require_trace=True)
        self.assertTrue(tracer.require_trace)

    def test_start_span_raises_when_require_trace_true_and_no_trace(self):
        tracer = Tracer(require_trace=True)
        with self.assertRaises(RuntimeError) as cm:
            tracer.start_span("test")
        self.assertIn("no active trace", str(cm.exception))

    def test_start_span_with_require_trace_false_creates_auto_trace(self):
        tracer = Tracer(require_trace=True)
        span = tracer.start_span("test", require_trace=False)
        self.assertIsNotNone(span)
        # Auto-trace should be created
        self.assertTrue(getattr(tracer._traces.get(span.trace_id), "_tracekit_auto_trace", False))

    def test_start_span_per_call_override_require_trace_false(self):
        tracer = Tracer(require_trace=True)
        span = tracer.start_span("test", require_trace=False)
        self.assertIsNotNone(span)

    def test_start_span_per_call_override_require_trace_true_with_trace(self):
        tracer = Tracer(require_trace=False)
        tracer.start_trace("outer")
        span = tracer.start_span("test", require_trace=True)
        self.assertIsNotNone(span)

    def test_start_llm_span_respects_require_trace(self):
        tracer = Tracer(require_trace=True)
        with self.assertRaises(RuntimeError):
            tracer.start_llm_span(require_trace=True)
        # With override
        span = tracer.start_llm_span(require_trace=False)
        self.assertIsNotNone(span)

    def test_start_tool_span_respects_require_trace(self):
        tracer = Tracer(require_trace=True)
        with self.assertRaises(RuntimeError):
            tracer.start_tool_span("tool", require_trace=True)
        span = tracer.start_tool_span("tool", require_trace=False)
        self.assertIsNotNone(span)

    def test_span_context_manager_require_trace_override(self):
        """Context manager with require_trace=False: auto-trace ends with status ok, context cleared."""
        tracer = Tracer(require_trace=True)
        with self.assertRaises(RuntimeError):
            with tracer.span("test", require_trace=True):
                pass
        # With override - auto-trace should be ended and context cleared
        with tracer.span("test", require_trace=False) as span:
            self.assertIsNotNone(span)
            auto_trace = tracer._traces.get(span.trace_id)
            self.assertTrue(getattr(auto_trace, "_tracekit_auto_trace", False))
        # After exit: context cleared, auto-trace ended (status ok), remains in _traces for replay
        self.assertIsNone(get_current_trace())
        auto_trace = tracer._traces.get(span.trace_id)
        self.assertIsNotNone(auto_trace, "Auto-trace should remain in _traces for replay/flush")
        self.assertEqual(auto_trace.status, "ok", "Auto-trace should be ended with status ok")

    def test_async_span_context_manager_require_trace_override(self):
        """Async context manager with require_trace=False: auto-trace ends with status ok, context cleared."""
        async def run_test():
            tracer = Tracer(require_trace=True)
            with self.assertRaises(RuntimeError):
                async with tracer.async_span("test", require_trace=True):
                    pass
            async with tracer.async_span("test", require_trace=False) as span:
                self.assertIsNotNone(span)
                auto_trace = tracer._traces.get(span.trace_id)
                self.assertTrue(getattr(auto_trace, "_tracekit_auto_trace", False))
            # After exit: context cleared, auto-trace ended (status ok), remains in _traces for replay
            self.assertIsNone(get_current_trace())
            auto_trace = tracer._traces.get(span.trace_id)
            self.assertIsNotNone(auto_trace, "Auto-trace should remain in _traces for replay/flush")
            self.assertEqual(auto_trace.status, "ok", "Auto-trace should be ended with status ok")

        asyncio.run(run_test())

    def test_nested_traces_with_require_trace(self):
        tracer = Tracer(require_trace=True)
        # Start outer trace first
        tracer.start_trace("outer")
        # Now nested span with require_trace=True should work
        with tracer.span("inner", require_trace=True) as span:
            self.assertIsNotNone(span)
        # Nested trace context manager
        with tracer.trace("nested", require_trace=True) as inner_trace:
            self.assertIsNotNone(inner_trace)

    def test_no_temp_auto_trace_left_when_require_trace_true_raises(self):
        """When require_trace=True raises, no temp auto-trace should be left behind."""
        tracer = Tracer(require_trace=True)
        initial_trace_count = len(tracer._traces)

        with self.assertRaises(RuntimeError):
            tracer.start_span("test", require_trace=True)

        self.assertEqual(len(tracer._traces), initial_trace_count, "No auto-trace should be created when require_trace=True raises")

    def test_span_context_manager_exception_clears_context(self):
        """Exception in span() with require_trace=False: context cleared, auto-trace mirrors the failed span."""
        tracer = Tracer(require_trace=True)
        try:
            with tracer.span("test", require_trace=False) as span:
                raise ValueError("boom")
        except ValueError:
            pass

        # After exception: context cleared, auto-trace ends with the last span's status
        self.assertIsNone(get_current_trace())
        auto_trace = tracer._traces.get(span.trace_id)
        self.assertIsNotNone(auto_trace, "Auto-trace should remain in _traces for replay/flush")
        self.assertEqual(auto_trace.status, "error", "Auto-trace should reflect the failed span")


class TestBaggage(unittest.TestCase):
    """2.4: baggage - trace context set on start, restored on end, nested traces."""

    def setUp(self):
        clear_all_context()

    def tearDown(self):
        clear_all_context()

    def test_trace_start_sets_baggage(self):
        tracer = Tracer()
        tr = tracer.start_trace("test", attributes={"key": "value"})
        baggage = get_trace_context()
        self.assertIsNotNone(baggage)
        self.assertEqual(baggage["trace_id"], tr.trace_id)
        self.assertEqual(baggage["name"], "test")
        self.assertEqual(baggage["attributes"], {"key": "value"})

    def test_trace_end_restores_prior_baggage(self):
        tracer = Tracer()
        tr1 = tracer.start_trace("outer", attributes={"outer": True})
        baggage1 = get_trace_context()
        self.assertEqual(baggage1["name"], "outer")

        tr2 = tracer.start_trace("inner", attributes={"inner": True})
        baggage2 = get_trace_context()
        self.assertEqual(baggage2["name"], "inner")

        tracer.end_trace(tr2)
        baggage_after = get_trace_context()
        self.assertEqual(baggage_after["name"], "outer")

        tracer.end_trace(tr1)
        baggage_final = get_trace_context()
        self.assertIsNone(baggage_final)

    def test_nested_trace_context_manager_restores_outer_baggage(self):
        tracer = Tracer()
        with tracer.trace("outer", attributes={"outer": True}):
            baggage_outer = get_trace_context()
            self.assertEqual(baggage_outer["name"], "outer")

            with tracer.trace("inner", attributes={"inner": True}):
                baggage_inner = get_trace_context()
                self.assertEqual(baggage_inner["name"], "inner")

            # After inner exits, outer baggage restored
            baggage_restored = get_trace_context()
            self.assertEqual(baggage_restored["name"], "outer")

        # After outer exits, None
        baggage_final = get_trace_context()
        self.assertIsNone(baggage_final)

    def test_exception_in_trace_restores_prior_baggage(self):
        tracer = Tracer()
        with tracer.trace("outer"):
            baggage_outer = get_trace_context()
            self.assertEqual(baggage_outer["name"], "outer")

            try:
                with tracer.trace("inner"):
                    baggage_inner = get_trace_context()
                    self.assertEqual(baggage_inner["name"], "inner")
                    raise ValueError("boom")
            except ValueError:
                pass

            # After exception, outer baggage restored
            baggage_restored = get_trace_context()
            self.assertEqual(baggage_restored["name"], "outer")

    def test_trace_model_start_sets_context_token(self):
        tr = Trace(name="model-test", attributes={"a": 1})
        tr.start()
        baggage = get_trace_context()
        self.assertIsNotNone(baggage)
        self.assertEqual(baggage["trace_id"], tr.trace_id)

    def test_trace_model_end_resets_context_token(self):
        tr = Trace(name="model-test")
        tr.start()
        tr.end()
        baggage = get_trace_context()
        self.assertIsNone(baggage)

    def test_end_trace_without_start_does_not_crash(self):
        """end_trace on a trace that was never started should not crash."""
        tracer = Tracer()
        tr = Trace(name="unstarted")
        # Don't call tr.start()
        result = tracer.end_trace(tr)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "ok")

    def test_baggage_includes_attributes_metadata(self):
        tracer = Tracer()
        tracer.start_trace("test", attributes={"custom": "attr"}, metadata={"meta": "data"})
        baggage = get_trace_context()
        self.assertEqual(baggage["attributes"], {"custom": "attr"})
        # metadata is not in baggage by default - that's fine


class TestResidualRisks(unittest.TestCase):
    """Review for residual risks: enum serialization, legacy from_dict, token mutation, unbounded sets."""

    def setUp(self):
        clear_all_context()

    def tearDown(self):
        clear_all_context()

    def test_enum_serialization_in_sqlite_json(self):
        """SpanType enum should serialize to string value in to_dict (for JSON storage)."""
        span = Span(name="test", trace_id="t1", span_type=SpanType.LLM)
        d = span.to_dict()
        self.assertIsInstance(d["span_type"], str)
        self.assertEqual(d["span_type"], "llm")

    def test_from_dict_handles_unknown_span_type_gracefully(self):
        """from_dict should handle unknown span_type from legacy data."""
        data = {
            "name": "test",
            "trace_id": "t1",
            "span_id": "s1",
            "span_type": "unknown_legacy_type",
        }
        # Should not crash, should fall back to INTERNAL or raise clear error
        # Current implementation raises ValueError in __post_init__
        with self.assertRaises(ValueError):
            Span.from_dict(data)

    def test_token_attribute_mutation_safety(self):
        """Direct mutation of _tracekit_*_token attributes should not crash tracer."""
        tracer = Tracer()
        tr = tracer.start_trace("test")
        sp = tracer.start_span("test", trace=tr)

        # Mutate token attributes
        tr._tracekit_trace_token = "not a real token"
        sp._tracekit_span_token = "not a real token"

        # end_trace/end_span should handle gracefully (wrapped in try/except)
        tracer.end_trace(tr)
        tracer.end_span(sp)

    def test_unbounded_flushed_id_sets_growth(self):
        """_flushed_trace_ids and _flushed_span_ids grow unbounded - check behavior."""
        tracer = Tracer()
        # Simulate many flushes
        for i in range(100):
            tr = tracer.start_trace(f"trace-{i}")
            sp = tracer.start_span(f"span-{i}", trace=tr)
            tracer.end_span(sp)
            tracer.end_trace(tr)
            # Manually add to flushed sets to simulate growth
            tracer._flushed_trace_ids.add(tr.trace_id)
            tracer._flushed_span_ids.add(sp.span_id)

        # Sets should have 100 entries each
        self.assertEqual(len(tracer._flushed_trace_ids), 100)
        self.assertEqual(len(tracer._flushed_span_ids), 100)

    def test_flush_with_none_storage_returns_early(self):
        """flush() with storage=None should return early without touching buffers."""
        tracer = Tracer(storage=None)
        tr = tracer.start_trace("test")
        sp = tracer.start_span("test", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)

        # Should not raise
        asyncio.run(tracer.flush())

        # Buffers should still have items (since no storage to write to)
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)

    def test_thread_task_context_restoration(self):
        """Context restoration should work correctly across async boundaries."""
        async def run_test():
            tracer = Tracer()
            with tracer.trace("outer"):
                outer_baggage = get_trace_context()
                self.assertEqual(outer_baggage["name"], "outer")

                async def inner_task():
                    with tracer.trace("inner"):
                        inner_baggage = get_trace_context()
                        self.assertEqual(inner_baggage["name"], "inner")
                        await asyncio.sleep(0)  # Yield to event loop
                    # After inner trace ends, outer baggage should be restored
                    restored = get_trace_context()
                    self.assertEqual(restored["name"], "outer")

                await inner_task()

                # Still in outer
                final_baggage = get_trace_context()
                self.assertEqual(final_baggage["name"], "outer")

        asyncio.run(run_test())


class TestCancellationLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_async_trace_ends_when_cancelled(self):
        tracer = Tracer()

        async def run():
            async with tracer.async_trace("cancelled-request"):
                await asyncio.sleep(0)
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await run()

        self.assertEqual(len(tracer._traces), 1)
        trace = next(iter(tracer._traces.values()))
        self.assertEqual(trace.status, "error")
        self.assertIsNotNone(trace.end_time)
        self.assertIsNone(get_current_trace())


if __name__ == "__main__":
    unittest.main()