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


if __name__ == "__main__":
    unittest.main()
