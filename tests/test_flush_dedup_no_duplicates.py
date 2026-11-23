import tempfile
import unittest
from pathlib import Path

from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.tracer import Tracer


class TestFlushDeduplication(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)
        self._temp_dir = tempfile.TemporaryDirectory()
        self.backend = SQLiteBackend(Path(self._temp_dir.name) / "flush.db")
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()
        self._temp_dir.cleanup()
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    async def _row_counts(self):
        return (
            await self.backend.count_documents("traces"),
            await self.backend.count_documents("spans"),
        )

    async def test_repeated_flush_does_not_duplicate_rows(self):
        tracer = Tracer(storage=self.backend)
        trace = tracer.start_trace("flush-once")
        span = tracer.start_span("one-span")
        tracer.end_span(span)
        tracer.end_trace(trace)

        await tracer.flush()
        self.assertEqual(await self._row_counts(), (1, 1))

        await tracer.flush()
        self.assertEqual(await self._row_counts(), (1, 1))

        await tracer.flush()
        self.assertEqual(await self._row_counts(), (1, 1))

    async def test_running_trace_is_flushed_only_after_it_ends(self):
        tracer = Tracer(storage=self.backend)
        trace = tracer.start_trace("long-request")
        span = tracer.start_span("tool-call")
        tracer.end_span(span)

        await tracer.flush()
        self.assertEqual(await self._row_counts(), (0, 1))

        tracer.end_trace(trace)
        await tracer.flush()
        self.assertEqual(await self._row_counts(), (1, 1))

    async def test_close_waits_for_scheduled_flush(self):
        import sqlite3

        db_path = Path(self._temp_dir.name) / "close.db"
        backend = SQLiteBackend(db_path)
        await backend.initialize()
        tracer = Tracer(storage=backend, auto_flush=True)
        trace = tracer.start_trace("closing-request")
        span = tracer.start_span("tool-call")
        tracer.end_span(span)
        tracer.end_trace(trace)

        await tracer.close()

        conn = sqlite3.connect(db_path)
        try:
            counts = tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("traces", "spans")
            )
        finally:
            conn.close()
        self.assertEqual(counts, (1, 1))


if __name__ == "__main__":
    unittest.main()
