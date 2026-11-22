"""Adversarial edge-case tests for Phase 3 search and deletion."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

try:
    import aiosqlite  # noqa: F401
    HAS_AIOSQLITE = True
except Exception:
    HAS_AIOSQLITE = False

from tracekit.api import delete_trace, search_traces
from tracekit.core.db import TraceStore
from tracekit.core.span import SpanType
from tracekit.core.storage import StorageBackend
from tracekit.core.tracer import Tracer

# Check for MemoryBackend
try:
    from backend.apps.db.memory import MemoryBackend
    HAS_MEMORY = True
except Exception:
    HAS_MEMORY = False


class FakeStorage(StorageBackend):
    """Fake storage backend for testing without SQLite."""
    
    def __init__(self):
        self._connected = True
        self.traces = {}
        self.spans = {}
    
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
        if collection == "traces":
            self.traces[document["trace_id"]] = document
        elif collection == "spans":
            self.spans[document["span_id"]] = document
    
    def _match_filter(self, doc: dict, filter: dict) -> bool:
        """Check if document matches filter, supporting $gte/$lte operators."""
        for key, value in filter.items():
            if isinstance(value, dict):
                # Handle operators like $gte, $lte, $gt, $lt
                doc_value = doc.get(key)
                if doc_value is None:
                    return False
                for op, op_val in value.items():
                    if op == "$gte":
                        if not (doc_value >= op_val):
                            return False
                    elif op == "$lte":
                        if not (doc_value <= op_val):
                            return False
                    elif op == "$gt":
                        if not (doc_value > op_val):
                            return False
                    elif op == "$lt":
                        if not (doc_value < op_val):
                            return False
            else:
                if doc.get(key) != value:
                    return False
        return True
    
    async def find_one(self, collection: str, filter: dict):
        if collection == "traces":
            for trace in self.traces.values():
                if self._match_filter(trace, filter):
                    return trace
        elif collection == "spans":
            for span in self.spans.values():
                if self._match_filter(span, filter):
                    return span
        return None
    
    async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
        if collection == "traces":
            results = list(self.traces.values())
        elif collection == "spans":
            results = list(self.spans.values())
        else:
            results = []
        
        if filter:
            results = [r for r in results if self._match_filter(r, filter)]
        
        if sort:
            for field, direction in reversed(sort):
                reverse = direction == -1
                results.sort(key=lambda x: x.get(field, ""), reverse=reverse)
        
        if limit is not None:
            results = results[:limit]
        
        return results
    
    async def count_documents(self, collection: str, filter: dict | None = None) -> int:
        if collection == "traces":
            results = list(self.traces.values())
        elif collection == "spans":
            results = list(self.spans.values())
        else:
            return 0
        
        if filter:
            results = [r for r in results if self._match_filter(r, filter)]
        
        return len(results)
    
    async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
        pass
    
    async def delete_one(self, collection: str, filter: dict) -> int:
        if collection == "traces":
            to_delete = [tid for tid, t in self.traces.items() if self._match_filter(t, filter)]
            for tid in to_delete:
                del self.traces[tid]
            return len(to_delete)
        elif collection == "spans":
            to_delete = [sid for sid, s in self.spans.items() if self._match_filter(s, filter)]
            for sid in to_delete:
                del self.spans[sid]
            return len(to_delete)
        return 0


class TestSearchTracesStatusName(unittest.IsolatedAsyncioTestCase):
    """Test status and name filters."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_status_exact_match(self):
        # Insert traces with different statuses
        await self.store.save_trace({"trace_id": "t1", "name": "test1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "test2", "status": "error", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "test3", "status": "running", "start_time": "2024-01-01T02:00:00"})
        
        results = await self.store.search_traces(status="ok")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trace_id"], "t1")
        
        results = await self.store.search_traces(status="error")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trace_id"], "t2")
    
    async def test_name_exact_match(self):
        await self.store.save_trace({"trace_id": "t1", "name": "my-trace", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "other-trace", "status": "ok", "start_time": "2024-01-01T01:00:00"})
        
        results = await self.store.search_traces(name="my-trace")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trace_id"], "t1")
        
        results = await self.store.search_traces(name="nonexistent")
        self.assertEqual(len(results), 0)


class TestSearchTracesTimeRange(unittest.IsolatedAsyncioTestCase):
    """Test start_time/end_time range filters."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_start_time_filter(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "ok", "start_time": "2024-01-02T00:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "ok", "start_time": "2024-01-03T00:00:00"})
        
        # start_time >= 2024-01-02
        results = await self.store.search_traces(start_time="2024-01-02T00:00:00")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["trace_id"], "t3")  # newest first
        self.assertEqual(results[1]["trace_id"], "t2")
        
        # With datetime object
        results = await self.store.search_traces(start_time=datetime(2024, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(len(results), 2)
    
    async def test_end_time_filter(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "ok", "start_time": "2024-01-02T00:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "ok", "start_time": "2024-01-03T00:00:00"})
        
        # end_time <= 2024-01-02
        results = await self.store.search_traces(end_time="2024-01-02T00:00:00")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["trace_id"], "t2")
        self.assertEqual(results[1]["trace_id"], "t1")
    
    async def test_time_range_both(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "ok", "start_time": "2024-01-02T00:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "ok", "start_time": "2024-01-03T00:00:00"})
        await self.store.save_trace({"trace_id": "t4", "name": "t4", "status": "ok", "start_time": "2024-01-04T00:00:00"})
        
        results = await self.store.search_traces(
            start_time="2024-01-02T00:00:00",
            end_time="2024-01-03T23:59:59"
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["trace_id"], "t3")
        self.assertEqual(results[1]["trace_id"], "t2")


class TestSearchTracesError(unittest.IsolatedAsyncioTestCase):
    """Test error filter: True/False/string."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_error_true(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "error": None, "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "error", "error": "connection failed", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "error", "error": "  ", "start_time": "2024-01-01T02:00:00"})
        
        results = await self.store.search_traces(error=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trace_id"], "t2")
    
    async def test_error_false(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "error": None, "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "error", "error": "connection failed", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "error", "error": "  ", "start_time": "2024-01-01T02:00:00"})
        
        results = await self.store.search_traces(error=False)
        self.assertEqual(len(results), 2)
        trace_ids = {r["trace_id"] for r in results}
        self.assertEqual(trace_ids, {"t1", "t3"})
    
    async def test_error_string_substring(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "error": "Connection timeout", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "error", "error": "Database connection failed", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "error", "error": "Auth error", "start_time": "2024-01-01T02:00:00"})
        
        results = await self.store.search_traces(error="connection")
        self.assertEqual(len(results), 2)
        trace_ids = {r["trace_id"] for r in results}
        self.assertEqual(trace_ids, {"t1", "t2"})
        
        # Case insensitive
        results = await self.store.search_traces(error="CONNECTION")
        self.assertEqual(len(results), 2)


class TestSearchTracesSpanType(unittest.IsolatedAsyncioTestCase):
    """Test span_type filter with post-filtering."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_span_type_filter_matching(self):
        # Trace with LLM span
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s1", "trace_id": "t1", "span_type": "llm", "start_time": "2024-01-01T00:00:00"})
        
        # Trace with TOOL span
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "ok", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_span({"span_id": "s2", "trace_id": "t2", "span_type": "tool", "start_time": "2024-01-01T01:00:00"})
        
        # Trace with multiple spans including LLM
        await self.store.save_trace({"trace_id": "t3", "name": "t3", "status": "ok", "start_time": "2024-01-01T02:00:00"})
        await self.store.save_span({"span_id": "s3", "trace_id": "t3", "span_type": "internal", "start_time": "2024-01-01T02:00:00"})
        await self.store.save_span({"span_id": "s4", "trace_id": "t3", "span_type": "llm", "start_time": "2024-01-01T02:00:01"})
        
        results = await self.store.search_traces(span_type="llm")
        self.assertEqual(len(results), 2)
        trace_ids = {r["trace_id"] for r in results}
        self.assertEqual(trace_ids, {"t1", "t3"})
    
    async def test_span_type_filter_case_insensitive(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s1", "trace_id": "t1", "span_type": "llm", "start_time": "2024-01-01T00:00:00"})
        
        results = await self.store.search_traces(span_type="llm")
        self.assertEqual(len(results), 1)
        
        results = await self.store.search_traces(span_type="  LLM  ")
        self.assertEqual(len(results), 1)
    
    async def test_span_type_filter_no_matching_span(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s1", "trace_id": "t1", "span_type": "tool", "start_time": "2024-01-01T00:00:00"})
        
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "ok", "start_time": "2024-01-01T01:00:00"})
        # No spans for t2
        
        results = await self.store.search_traces(span_type="llm")
        self.assertEqual(len(results), 0)
    
    async def test_span_type_filter_with_other_filters(self):
        await self.store.save_trace({"trace_id": "t1", "name": "trace-a", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s1", "trace_id": "t1", "span_type": "llm", "start_time": "2024-01-01T00:00:00"})
        
        await self.store.save_trace({"trace_id": "t2", "name": "trace-b", "status": "error", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_span({"span_id": "s2", "trace_id": "t2", "span_type": "llm", "start_time": "2024-01-01T01:00:00"})
        
        results = await self.store.search_traces(span_type="llm", status="ok")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trace_id"], "t1")


class TestSearchTracesLimitOffsetOrdering(unittest.IsolatedAsyncioTestCase):
    """Test limit, offset, and ordering."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_limit(self):
        for i in range(5):
            await self.store.save_trace({
                "trace_id": f"t{i}",
                "name": f"t{i}",
                "status": "ok",
                "start_time": f"2024-01-0{i+1}T00:00:00"
            })
        
        results = await self.store.search_traces(limit=3)
        self.assertEqual(len(results), 3)
        # Newest first
        self.assertEqual(results[0]["trace_id"], "t4")
        self.assertEqual(results[1]["trace_id"], "t3")
        self.assertEqual(results[2]["trace_id"], "t2")
    
    async def test_offset(self):
        for i in range(5):
            await self.store.save_trace({
                "trace_id": f"t{i}",
                "name": f"t{i}",
                "status": "ok",
                "start_time": f"2024-01-0{i+1}T00:00:00"
            })
        
        results = await self.store.search_traces(offset=2, limit=10)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["trace_id"], "t2")
        self.assertEqual(results[1]["trace_id"], "t1")
        self.assertEqual(results[2]["trace_id"], "t0")
    
    async def test_limit_offset_combined(self):
        for i in range(10):
            start_time = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
            await self.store.save_trace({
                "trace_id": f"t{i}",
                "name": f"t{i}",
                "status": "ok",
                "start_time": start_time.isoformat()
            })
        
        results = await self.store.search_traces(offset=3, limit=4)
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]["trace_id"], "t6")
        self.assertEqual(results[1]["trace_id"], "t5")
        self.assertEqual(results[2]["trace_id"], "t4")
        self.assertEqual(results[3]["trace_id"], "t3")
    
    async def test_ordering_newest_first(self):
        await self.store.save_trace({"trace_id": "old", "name": "old", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_trace({"trace_id": "new", "name": "new", "status": "ok", "start_time": "2024-01-03T00:00:00"})
        await self.store.save_trace({"trace_id": "mid", "name": "mid", "status": "ok", "start_time": "2024-01-02T00:00:00"})
        
        results = await self.store.search_traces()
        self.assertEqual(results[0]["trace_id"], "new")
        self.assertEqual(results[1]["trace_id"], "mid")
        self.assertEqual(results[2]["trace_id"], "old")


class TestSearchTracesMissingTraceEmptyDB(unittest.IsolatedAsyncioTestCase):
    """Test missing trace and empty database."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_empty_db(self):
        results = await self.store.search_traces()
        self.assertEqual(results, [])
        
        results = await self.store.search_traces(status="ok")
        self.assertEqual(results, [])
    
    async def test_no_matching_traces(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        
        results = await self.store.search_traces(name="nonexistent")
        self.assertEqual(results, [])


class TestDeleteTrace(unittest.IsolatedAsyncioTestCase):
    """Test delete_trace functionality."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_delete_trace_removes_trace_and_spans(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s1", "trace_id": "t1", "span_type": "llm", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s2", "trace_id": "t1", "span_type": "tool", "start_time": "2024-01-01T00:00:01"})
        
        await self.store.save_trace({"trace_id": "t2", "name": "t2", "status": "ok", "start_time": "2024-01-01T01:00:00"})
        await self.store.save_span({"span_id": "s3", "trace_id": "t2", "span_type": "llm", "start_time": "2024-01-01T01:00:00"})
        
        # Delete t1
        result = await self.store.delete_trace("t1")
        self.assertTrue(result)
        
        # Verify trace is gone
        trace = await self.store.get_trace("t1")
        self.assertIsNone(trace)
        
        # Verify spans are gone
        spans = await self.store.get_spans_for_trace("t1")
        self.assertEqual(spans, [])
        
        # Verify other trace remains
        trace2 = await self.store.get_trace("t2")
        self.assertIsNotNone(trace2)
        spans2 = await self.store.get_spans_for_trace("t2")
        self.assertEqual(len(spans2), 1)
    
    async def test_delete_trace_returns_false_for_missing(self):
        result = await self.store.delete_trace("nonexistent")
        self.assertFalse(result)
    
    async def test_delete_trace_returns_true_on_success(self):
        await self.store.save_trace({"trace_id": "t1", "name": "t1", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        
        result = await self.store.delete_trace("t1")
        self.assertTrue(result)


class TestDeleteTraceBackendWithoutDeleteOne(unittest.IsolatedAsyncioTestCase):
    """Test delete_trace raises NotImplementedError for backends without delete_one."""
    
    class BackendWithoutDelete(StorageBackend):
        def __init__(self):
            self._connected = True
        
        @property
        def is_connected(self):
            return True
        
        @property
        def storage_type(self):
            return "no-delete"
        
        @property
        def storage_detail(self):
            return "no-delete"
        
        async def initialize(self):
            pass
        
        async def close(self):
            pass
        
        async def insert_one(self, collection: str, document: dict) -> None:
            pass
        
        async def find_one(self, collection: str, filter: dict):
            return None
        
        async def find_many(self, collection: str, filter: dict | None = None, sort: list | None = None, limit: int | None = None):
            return []
        
        async def count_documents(self, collection: str, filter: dict | None = None) -> int:
            return 0
        
        async def create_index(self, collection: str, key_or_list: str | list, unique: bool = False):
            pass
    
    async def asyncSetUp(self):
        self.storage = self.BackendWithoutDelete()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_delete_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError) as cm:
            await self.store.delete_trace("t1")
        self.assertIn("does not support delete_trace", str(cm.exception))


class TestAPIHelpers(unittest.IsolatedAsyncioTestCase):
    """Test api.py helper functions."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_search_traces_helper(self):
        await self.store.save_trace({"trace_id": "t1", "name": "test", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        
        results = await search_traces(self.store, status="ok")
        self.assertEqual(len(results), 1)
    
    async def test_delete_trace_helper(self):
        await self.store.save_trace({"trace_id": "t1", "name": "test", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        
        result = await delete_trace(self.store, "t1")
        self.assertTrue(result)
        
        trace = await self.store.get_trace("t1")
        self.assertIsNone(trace)


class TestListTracesBackwardCompat(unittest.IsolatedAsyncioTestCase):
    """Test list_traces remains backward compatible."""
    
    async def asyncSetUp(self):
        self.storage = FakeStorage()
        self.store = TraceStore(self.storage)
        await self.store.initialize()
    
    async def test_list_traces_still_works(self):
        for i in range(5):
            await self.store.save_trace({
                "trace_id": f"t{i}",
                "name": f"t{i}",
                "status": "ok",
                "start_time": f"2024-01-0{i+1}T00:00:00"
            })
        
        results = await self.store.list_traces(limit=3)
        self.assertEqual(len(results), 3)
        # Should still be newest first
        self.assertEqual(results[0]["trace_id"], "t4")
    
    async def test_list_traces_default_limit(self):
        for i in range(150):
            await self.store.save_trace({
                "trace_id": f"t{i}",
                "name": f"t{i}",
                "status": "ok",
                "start_time": f"2024-01-01T{i:02d}:00:00"
            })
        
        results = await self.store.list_traces()  # default limit 100
        self.assertEqual(len(results), 100)


@unittest.skipUnless(HAS_AIOSQLITE, "aiosqlite not installed")
class TestSearchTracesSQLiteBackend(unittest.IsolatedAsyncioTestCase):
    """Integration tests with real SQLiteBackend."""
    
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        
        from backend.apps.db.sqllite import SQLiteBackend
        self.backend = SQLiteBackend(db_path=self.path)
        await self.backend.initialize()
        self.store = TraceStore(self.backend)
        await self.store.initialize()
    
    async def asyncTearDown(self):
        await self.backend.close()
        for ext in ["", "-wal", "-shm"]:
            try:
                os.unlink(self.path + ext)
            except FileNotFoundError:
                pass
    
    async def test_search_traces_status(self):
        tracer = Tracer(storage=self.backend)
        tr = tracer.start_trace("test", status="ok")
        tr.end()
        await tracer.flush()
        
        tr2 = tracer.start_trace("test2", status="error")
        tr2.end(error="failed")
        await tracer.flush()
        
        results = await self.store.search_traces(status="ok")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "test")
    
    async def test_search_traces_span_type(self):
        tracer = Tracer(storage=self.backend)
        tr = tracer.start_trace("trace-with-llm")
        with tracer.span("llm-span", span_type="llm"):
            pass
        tracer.end_trace(tr)
        await tracer.flush()
        
        tr2 = tracer.start_trace("trace-with-tool")
        with tracer.span("tool-span", span_type="tool"):
            pass
        tracer.end_trace(tr2)
        await tracer.flush()
        
        results = await self.store.search_traces(span_type="llm")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "trace-with-llm")
    
    async def test_delete_trace(self):
        tracer = Tracer(storage=self.backend)
        tr = tracer.start_trace("to-delete")
        with tracer.span("span1"):
            pass
        tracer.end_trace(tr)
        await tracer.flush()
        
        tr2 = tracer.start_trace("to-keep")
        tracer.end_trace(tr2)
        await tracer.flush()
        
        result = await self.store.delete_trace(tr.trace_id)
        self.assertTrue(result)
        
        deleted = await self.store.get_trace(tr.trace_id)
        self.assertIsNone(deleted)
        
        kept = await self.store.get_trace(tr2.trace_id)
        self.assertIsNotNone(kept)


@unittest.skipUnless(HAS_MEMORY, "MemoryBackend not available")
class TestSearchTracesMemoryBackend(unittest.IsolatedAsyncioTestCase):
    """Test with MemoryBackend if available."""
    
    async def asyncSetUp(self):
        self.backend = MemoryBackend()
        await self.backend.initialize()
        self.store = TraceStore(self.backend)
        await self.store.initialize()
    
    async def asyncTearDown(self):
        await self.backend.close()
    
    async def test_memory_backend_search(self):
        await self.store.save_trace({"trace_id": "t1", "name": "test", "status": "ok", "start_time": "2024-01-01T00:00:00"})
        await self.store.save_span({"span_id": "s1", "trace_id": "t1", "span_type": "llm", "start_time": "2024-01-01T00:00:00"})
        
        results = await self.store.search_traces(span_type="llm")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trace_id"], "t1")


if __name__ == "__main__":
    unittest.main()