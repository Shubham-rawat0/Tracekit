import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import aiosqlite  # noqa: F401

    HAS_AIOSQLITE = True
except Exception:
    HAS_AIOSQLITE = False

from backend.apps.db.memory import MemoryBackend
from backend.apps.db.sqllite import SQLiteBackend, _format_timestamp, _parse_timestamp
from tracekit.core.db import TraceStore
from tracekit.core.tracer import Tracer


class TestTimestampHelpers(unittest.TestCase):
    def test_timestamp_normalization_and_round_trip(self):
        aware = datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=2)))
        text = _format_timestamp(aware)
        self.assertEqual(text, "2024-01-02T01:04:05.123456+00:00")
        self.assertEqual(
            _parse_timestamp(text),
            datetime(2024, 1, 2, 1, 4, 5, 123456, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _parse_timestamp("2024-01-02T01:04:05Z"),
            datetime(2024, 1, 2, 1, 4, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _parse_timestamp("2024-01-02T01:04:05"),
            datetime(2024, 1, 2, 1, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIsNone(_parse_timestamp(""))
        self.assertIsNone(_parse_timestamp(None))


@unittest.skipUnless(HAS_AIOSQLITE, "aiosqlite not installed")
class TestSQLiteMetadata(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.backend = SQLiteBackend(Path(self._temp_dir.name) / "phase1.db")
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()
        self._temp_dir.cleanup()

    async def test_trace_metadata_populated_and_timestamps_deserialized(self):
        start = datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=-5)))
        end = start + timedelta(milliseconds=1500, microseconds=456)
        await self.backend.insert_one(
            "traces",
            {
                "trace_id": "trace-meta",
                "name": "meta",
                "start_time": start,
                "end_time": end,
                "attributes": {"when": start},
            },
        )

        trace = await self.backend.find_one("traces", {"trace_id": "trace-meta"})
        self.assertIsInstance(trace["start_time"], datetime)
        self.assertIsInstance(trace["end_time"], datetime)
        self.assertIsInstance(trace["created_at"], datetime)
        self.assertIsInstance(trace["updated_at"], datetime)
        self.assertEqual(trace["start_time"], _parse_timestamp(start))
        self.assertEqual(trace["end_time"], _parse_timestamp(end))
        self.assertAlmostEqual(trace["duration_ms"], 1500.456, places=3)
        self.assertEqual(trace["attributes"]["when"], "2024-01-02T08:04:05.123456+00:00")

        cursor = await self.backend._conn.execute(
            "SELECT start_time, created_at, updated_at FROM traces WHERE trace_id = ?",
            ("trace-meta",),
        )
        raw = await cursor.fetchone()
        self.assertEqual(raw["start_time"], "2024-01-02T08:04:05.123456+00:00")
        self.assertTrue(raw["created_at"].endswith("+00:00"))
        self.assertTrue(raw["updated_at"].endswith("+00:00"))

    async def test_duration_precision_with_mixed_timezone_accuracy(self):
        start = datetime(2024, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 5, 1, 0, 0, 1, 234567, tzinfo=timezone.utc)
        await self.backend.insert_one(
            "spans",
            {
                "span_id": "duration-span",
                "trace_id": "trace-duration",
                "start_time": start,
                "end_time": end,
            },
        )
        span = await self.backend.find_one("spans", {"span_id": "duration-span"})
        self.assertAlmostEqual(span["duration_ms"], 1234.567, places=3)

        tiny_end = start + timedelta(microseconds=1)
        await self.backend.insert_one(
            "traces",
            {
                "trace_id": "tiny-duration",
                "start_time": start,
                "end_time": tiny_end,
            },
        )
        tiny = await self.backend.find_one("traces", {"trace_id": "tiny-duration"})
        self.assertAlmostEqual(tiny["duration_ms"], 0.001, places=3)

    async def test_created_at_preserved_and_updated_at_bumped_on_rewrite(self):
        created = datetime(2020, 1, 1, tzinfo=timezone.utc)
        document = {"trace_id": "trace-update", "name": "first", "created_at": created}
        await self.backend.insert_one("traces", document)
        first = await self.backend.find_one("traces", {"trace_id": "trace-update"})
        self.assertEqual(first["created_at"], created)

        await asyncio.sleep(0.002)
        document["name"] = "second"
        await self.backend.insert_one("traces", document)
        second = await self.backend.find_one("traces", {"trace_id": "trace-update"})
        self.assertEqual(second["name"], "second")
        self.assertEqual(second["created_at"], created)
        self.assertGreater(second["updated_at"], first["updated_at"])

    async def test_span_schema_is_not_extended_with_trace_only_columns(self):
        await self.backend.insert_one(
            "spans", {"span_id": "span-schema", "trace_id": "trace-schema", "name": "work"}
        )
        cursor = await self.backend._conn.execute("PRAGMA table_info(spans)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertNotIn("created_at", columns)
        self.assertNotIn("updated_at", columns)

        span = await self.backend.find_one("spans", {"span_id": "span-schema"})
        self.assertIsInstance(span["start_time"], datetime)
        self.assertIsNone(span["end_time"])
        self.assertIsNone(span["duration_ms"])

    async def test_timezone_aware_filtering_and_sorting(self):
        base = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
        await self.backend.insert_one(
            "traces", {"trace_id": "early", "start_time": base - timedelta(hours=1)}
        )
        await self.backend.insert_one(
            "traces", {"trace_id": "late", "start_time": base + timedelta(hours=1)}
        )
        offset_start = (base + timedelta(hours=2)).astimezone(timezone(timedelta(hours=2)))
        await self.backend.insert_one(
            "traces", {"trace_id": "offset", "start_time": offset_start}
        )

        ordered = await self.backend.find_many("traces", sort=[("start_time", 1)])
        self.assertEqual([trace["trace_id"] for trace in ordered], ["early", "late", "offset"])

        aware_filtered = await self.backend.find_many(
            "traces", filter={"start_time": {"$gte": base}}
        )
        self.assertEqual({trace["trace_id"] for trace in aware_filtered}, {"late", "offset"})

        string_filtered = await self.backend.find_many(
            "traces", filter={"start_time": (base + timedelta(hours=1)).isoformat()}
        )
        self.assertEqual([trace["trace_id"] for trace in string_filtered], ["late"])

    async def test_trace_and_span_persistence_through_store(self):
        tracer = Tracer(storage=self.backend)
        store = TraceStore(self.backend)
        trace = tracer.start_trace("persisted")
        with tracer.span("work", span_type="llm", input="prompt") as span:
            span.output = "reply"
        tracer.end_trace(trace)
        await tracer.flush()

        stored_trace = await store.get_trace(trace.trace_id)
        self.assertIsNotNone(stored_trace)
        self.assertIsInstance(stored_trace["start_time"], datetime)
        self.assertIsInstance(stored_trace["end_time"], datetime)
        self.assertIsInstance(stored_trace["created_at"], datetime)
        self.assertIsInstance(stored_trace["updated_at"], datetime)
        self.assertIsInstance(stored_trace["duration_ms"], float)

        replay = await store.get_trace_replay(trace.trace_id)
        self.assertEqual(len(replay["spans"]), 1)
        stored_span = replay["spans"][0]
        self.assertIsInstance(stored_span["start_time"], datetime)
        self.assertIsInstance(stored_span["end_time"], datetime)
        self.assertIsInstance(stored_span["duration_ms"], float)
        self.assertEqual(json.loads(json.dumps(stored_span["events"])), [])


class TestUnstartedSpanSorting(unittest.IsolatedAsyncioTestCase):
    async def test_unstarted_spans_sort_last_ascending_and_first_descending(self):
        backend = MemoryBackend()
        await backend.initialize()
        started = datetime(2024, 1, 1, tzinfo=timezone.utc)
        await backend.insert_one(
            "spans", {"span_id": "started", "start_time": started}
        )
        await backend.insert_one(
            "spans", {"span_id": "unstarted", "start_time": None}
        )
        await backend.insert_one("spans", {"span_id": "missing"})

        ascending = await backend.find_many("spans", sort=[("start_time", 1)])
        self.assertEqual(ascending[0]["span_id"], "started")
        self.assertEqual(
            {span["span_id"] for span in ascending[1:]}, {"unstarted", "missing"}
        )

        descending = await backend.find_many("spans", sort=[("start_time", -1)])
        self.assertEqual(
            {span["span_id"] for span in descending[:2]}, {"unstarted", "missing"}
        )
        self.assertEqual(descending[-1]["span_id"], "started")

    async def test_timezone_aware_memory_sorting(self):
        backend = MemoryBackend()
        await backend.initialize()
        earlier = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        later = datetime(2024, 1, 1, 15, 0, tzinfo=timezone(timedelta(hours=2)))
        await backend.insert_one("spans", {"span_id": "later", "start_time": later})
        await backend.insert_one("spans", {"span_id": "earlier", "start_time": earlier})
        await backend.insert_one(
            "spans", {"span_id": "string-z", "start_time": "2024-01-01T11:30:00Z"}
        )

        ascending = await backend.find_many("spans", sort=[("start_time", 1)])
        self.assertEqual(
            [span["span_id"] for span in ascending],
            ["string-z", "earlier", "later"],
        )


if __name__ == "__main__":
    unittest.main()
