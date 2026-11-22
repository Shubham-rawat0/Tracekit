import asyncio
import os
import tempfile
import unittest

try:
    import aiosqlite  # noqa: F401
    HAS_AIOSQLITE = True
except Exception:
    HAS_AIOSQLITE = False

from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.db import TraceStore
from tracekit.core.tracer import Tracer


@unittest.skipUnless(HAS_AIOSQLITE, "aiosqlite not installed")
class TestSQLiteBackend(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_persist_and_replay(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            backend = SQLiteBackend(db_path=path)
            await backend.initialize()
            store = TraceStore(backend)
            tracer = Tracer(storage=backend)
            tr = tracer.start_trace("dbt")
            with tracer.span("llm", span_type="llm", input="p") as s:
                tracer.record_llm_result(s, response="r", total_tokens=3)
            tracer.end_trace(tr)
            tid = tr.trace_id
            await tracer.flush()
            t = await store.get_trace(tid)
            replay = await store.get_trace_replay(tid)
            self.assertIsNotNone(t)
            self.assertEqual(t["name"], "dbt")
            self.assertIsNotNone(replay)
            self.assertEqual(len(replay["spans"]), 1)
            await backend.close()
        finally:
            for ext in ["", "-wal", "-shm"]:
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
