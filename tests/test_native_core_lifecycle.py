"""Tests for native persistence and streaming lifecycle in Tracer."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    set_current_span,
    set_current_trace,
)
from tracekit.core.span import SpanType
from tracekit.core.storage import StorageBackend
from tracekit.core.trace import Trace
from tracekit.core.tracer import Tracer


class MockStorage(StorageBackend):
    """Mock storage backend for testing."""
    
    def __init__(self, fail_on_traces=None, fail_on_spans=None):
        self._connected = True
        self.inserted_traces = []
        self.inserted_spans = []
        self.fail_on_traces = fail_on_traces or set()
        self.fail_on_spans = fail_on_spans or set()
        self.trace_call_count = 0
        self.span_call_count = 0

    @property
    def is_connected(self):
        return self._connected

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

    async def insert_many(self, collection: str, documents: list[dict]):
        if collection == "traces":
            self.trace_call_count += 1
            if self.trace_call_count in self.fail_on_traces:
                raise RuntimeError(f"Simulated trace failure on call {self.trace_call_count}")
            self.inserted_traces.extend(documents)
        elif collection == "spans":
            self.span_call_count += 1
            if self.span_call_count in self.fail_on_spans:
                raise RuntimeError(f"Simulated span failure on call {self.span_call_count}")
            self.inserted_spans.extend(documents)

    async def insert_one(self, collection: str, document: dict):
        # Not used anymore but required by interface
        await self.insert_many(collection, [document])

    async def find_one(self, collection: str, filter: dict):
        return None

    async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
        return []

    async def count_documents(self, collection: str, filter: dict | None = None):
        return 0

    async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
        pass


class TestDefaultSQLitePersistence(unittest.TestCase):
    """Test default SQLite persistence behavior."""

    def setUp(self):
        set_current_trace(None)
        set_current_span(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)

    def test_omitted_storage_constructs_sqlite_backend(self):
        """Omitted storage should construct SQLite backend on first flush."""
        with patch("backend.apps.db.sqllite.SQLiteBackend") as mock_sqlite:
            mock_instance = MagicMock()
            mock_instance.is_connected = True
            mock_instance.insert_many = AsyncMock()
            mock_sqlite.return_value = mock_instance

            tracer = Tracer()
            self.assertIsNone(tracer.storage)
            mock_sqlite.assert_not_called()

            tracer.start_trace("test")
            asyncio.run(tracer.flush())

            mock_sqlite.assert_called_once_with()
            self.assertIs(tracer.storage, mock_instance)

    def test_omitted_storage_with_db_path(self):
        """Omitted storage with db_path should pass to SQLiteBackend."""
        with patch("backend.apps.db.sqllite.SQLiteBackend") as mock_sqlite:
            mock_instance = MagicMock()
            mock_instance.is_connected = True
            mock_instance.insert_many = AsyncMock()
            mock_sqlite.return_value = mock_instance

            tracer = Tracer(db_path="/custom/path.db")
            mock_sqlite.assert_not_called()

            tracer.start_trace("test")
            asyncio.run(tracer.flush())

            mock_sqlite.assert_called_once_with(db_path="/custom/path.db")

    def test_explicit_storage_none_remains_storage_free(self):
        """Explicit storage=None should remain storage-free."""
        tracer = Tracer(storage=None)
        self.assertIsNone(tracer.storage)

    def test_persist_enabled_false_disables_default_persistence(self):
        """persist_enabled=False should disable default SQLite persistence."""
        tracer = Tracer(persist_enabled=False)
        self.assertIsNone(tracer.storage)

    def test_explicit_storage_preserved(self):
        """Explicit storage backend should be preserved."""
        mock_storage = MockStorage()
        tracer = Tracer(storage=mock_storage)
        self.assertIs(tracer.storage, mock_storage)


class TestTransactionalBatchFlush(unittest.IsolatedAsyncioTestCase):
    """Test transactional batch flush with insert_many."""

    async def test_flush_uses_insert_many_for_traces(self):
        """Flush should use insert_many for traces collection."""
        storage = MockStorage()
        tracer = Tracer(storage=storage, auto_flush=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        await tracer.flush()
        
        # Verify insert_many was called with traces collection
        self.assertEqual(storage.trace_call_count, 1)
        self.assertEqual(len(storage.inserted_traces), 1)
        self.assertEqual(storage.inserted_traces[0]["trace_id"], tr.trace_id)

    async def test_flush_uses_insert_many_for_spans(self):
        """Flush should use insert_many for spans collection."""
        storage = MockStorage()
        tracer = Tracer(storage=storage, auto_flush=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        await tracer.flush()
        
        # Verify insert_many was called with spans collection
        self.assertEqual(storage.span_call_count, 1)
        self.assertEqual(len(storage.inserted_spans), 1)
        self.assertEqual(storage.inserted_spans[0]["span_id"], sp.span_id)

    async def test_flush_atomic_on_trace_failure(self):
        """If trace insert_many fails, spans should not be persisted."""
        storage = MockStorage(fail_on_traces={1})
        tracer = Tracer(storage=storage, auto_flush=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        with self.assertRaises(RuntimeError):
            await tracer.flush()
        
        # Both should remain in memory (atomic - nothing committed)
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)
        self.assertEqual(len(storage.inserted_traces), 0)
        self.assertEqual(len(storage.inserted_spans), 0)

    async def test_flush_atomic_on_span_failure(self):
        """If span insert_many fails, traces should not be marked as flushed."""
        storage = MockStorage(fail_on_spans={1})
        tracer = Tracer(storage=storage, auto_flush=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        with self.assertRaises(RuntimeError):
            await tracer.flush()
        
        # Both should remain in memory (atomic - not marked as flushed)
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)
        # Items should not be in flushed sets
        self.assertNotIn(tr.trace_id, tracer._flushed_trace_ids)
        self.assertNotIn(sp.span_id, tracer._flushed_span_ids)

    async def test_flush_leaves_pending_on_failure(self):
        """Failed items should remain buffered for retry."""
        storage = MockStorage(fail_on_spans={1})
        tracer = Tracer(storage=storage, auto_flush=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        with self.assertRaises(RuntimeError):
            await tracer.flush()
        
        # Items remain in memory
        self.assertIn(tr.trace_id, tracer._traces)
        self.assertIn(sp.span_id, tracer._spans)
        
        # Retry with working storage
        storage2 = MockStorage()
        tracer.storage = storage2
        await tracer.flush()
        
        # Both should be persisted now
        self.assertEqual(len(storage2.inserted_traces), 1)
        self.assertEqual(len(storage2.inserted_spans), 1)
        self.assertNotIn(tr.trace_id, tracer._traces)
        self.assertNotIn(sp.span_id, tracer._spans)

    async def test_flush_idempotent_after_success(self):
        """Repeated flush after success should be idempotent."""
        storage = MockStorage()
        tracer = Tracer(storage=storage, auto_flush=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        await tracer.flush()
        await tracer.flush()
        
        # Should only have one set of records
        self.assertEqual(len(storage.inserted_traces), 1)
        self.assertEqual(len(storage.inserted_spans), 1)

    async def test_flush_with_none_storage_is_noop(self):
        """flush() with storage=None should be a no-op."""
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


class TestLifecycleEvents(unittest.IsolatedAsyncioTestCase):
    """Test lifecycle event publication."""

    def setUp(self):
        set_current_trace(None)
        set_current_span(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)

    async def test_trace_start_event_emitted(self):
        """trace_start event should be emitted for sampled traces."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        tr = tracer.start_trace("test")
        
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "trace_start")
        self.assertEqual(events[0]["trace_id"], tr.trace_id)
        self.assertTrue(events[0]["sampled"])
        self.assertIn("trace", events[0])

    async def test_trace_end_event_emitted(self):
        """trace_end event should be emitted for sampled traces."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        tr = tracer.start_trace("test")
        tracer.end_trace(tr)
        
        trace_events = [e for e in events if e["type"] in ("trace_start", "trace_end")]
        self.assertEqual(len(trace_events), 2)
        self.assertEqual(trace_events[0]["type"], "trace_start")
        self.assertEqual(trace_events[1]["type"], "trace_end")
        self.assertEqual(trace_events[1]["trace_id"], tr.trace_id)

    async def test_span_start_event_emitted(self):
        """span_start event should be emitted for sampled spans."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        
        span_events = [e for e in events if e["type"] in ("span_start", "span_end")]
        self.assertEqual(len(span_events), 1)
        self.assertEqual(span_events[0]["type"], "span_start")
        self.assertEqual(span_events[0]["trace_id"], tr.trace_id)
        self.assertEqual(span_events[0]["span_id"], sp.span_id)
        self.assertIn("span", span_events[0])

    async def test_span_end_event_emitted(self):
        """span_end event should be emitted for sampled spans."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        
        span_events = [e for e in events if e["type"] in ("span_start", "span_end")]
        self.assertEqual(len(span_events), 2)
        self.assertEqual(span_events[0]["type"], "span_start")
        self.assertEqual(span_events[1]["type"], "span_end")
        self.assertEqual(span_events[1]["span_id"], sp.span_id)

    async def test_unsampled_traces_do_not_emit_events(self):
        """Unsampled traces should not emit events."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager, sampler=False)
        
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        self.assertEqual(len(events), 0)

    async def test_publisher_exception_does_not_break_tracing(self):
        """Publisher exceptions should be caught and not break tracing."""
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=RuntimeError("publisher failed"))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        # Should not raise despite publisher error
        tr = tracer.start_trace("test")
        sp = tracer.start_span("span", trace=tr)
        tracer.end_span(sp)
        tracer.end_trace(tr)
        
        # Tracing should still work
        self.assertEqual(tr.status, "ok")
        self.assertEqual(sp.status, "ok")

    async def test_context_manager_trace_emits_events(self):
        """Context manager trace() should emit trace_start and trace_end."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        with tracer.trace("ctx-trace"):
            pass
        
        trace_events = [e for e in events if e["type"] in ("trace_start", "trace_end")]
        self.assertEqual(len(trace_events), 2)
        self.assertEqual(trace_events[0]["type"], "trace_start")
        self.assertEqual(trace_events[1]["type"], "trace_end")

    async def test_context_manager_span_emits_events(self):
        """Context manager span() should emit span_start and span_end."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        with tracer.trace("ctx-trace"):
            with tracer.span("ctx-span"):
                pass
        
        span_events = [e for e in events if e["type"] in ("span_start", "span_end")]
        self.assertEqual(len(span_events), 2)
        self.assertEqual(span_events[0]["type"], "span_start")
        self.assertEqual(span_events[1]["type"], "span_end")

    async def test_async_context_manager_emits_events(self):
        """Async context managers should emit events."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        async with tracer.async_trace("async-trace"):
            async with tracer.async_span("async-span"):
                pass
        
        trace_events = [e for e in events if e["type"] in ("trace_start", "trace_end")]
        span_events = [e for e in events if e["type"] in ("span_start", "span_end")]
        self.assertEqual(len(trace_events), 2)
        self.assertEqual(len(span_events), 2)

    async def test_start_llm_span_emits_span_start(self):
        """start_llm_span should emit span_start event."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        tracer.start_trace("test")
        tracer.start_llm_span("llm", model="gpt-4")
        
        span_events = [e for e in events if e["type"] in ("span_start", "span_end")]
        self.assertEqual(len(span_events), 1)
        self.assertEqual(span_events[0]["type"], "span_start")

    async def test_start_tool_span_emits_span_start(self):
        """start_tool_span should emit span_start event."""
        events = []
        mock_manager = MagicMock()
        mock_manager.publish = MagicMock(side_effect=lambda e: events.append(e))
        
        tracer = Tracer(stream_enabled=True, stream_manager=mock_manager)
        
        tracer.start_trace("test")
        tracer.start_tool_span("my-tool", tool_input="input")
        
        span_events = [e for e in events if e["type"] in ("span_start", "span_end")]
        self.assertEqual(len(span_events), 1)
        self.assertEqual(span_events[0]["type"], "span_start")


class TestStreamingDisabled(unittest.IsolatedAsyncioTestCase):
    """Test streaming server when disabled."""

    async def test_start_stream_server_noop_when_disabled(self):
        """start_stream_server() should be no-op when stream_enabled=False."""
        tracer = Tracer(stream_enabled=False)
        result = await tracer.start_stream_server()
        self.assertIsNone(result)

    async def test_stop_stream_server_noop_when_disabled(self):
        """stop_stream_server() should be no-op when stream_enabled=False."""
        tracer = Tracer(stream_enabled=False)
        # Should not raise
        await tracer.stop_stream_server()


class TestContextRestoration(unittest.TestCase):
    """Test context restoration remains correct."""

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
        tr = self.tracer.start_trace("direct")
        self.assertIs(get_current_trace(), tr)
        self.tracer.end_trace(tr)
        self.assertIsNone(get_current_trace())
        
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
            self.assertIsNone(get_current_span())
            self.assertIsNone(get_current_trace())

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()