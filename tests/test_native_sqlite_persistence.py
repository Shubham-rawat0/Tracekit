import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from backend.apps.db.migrations import CURRENT_SCHEMA_VERSION
from backend.apps.db.sqllite import SQLiteBackend


def _legacy_database(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT UNIQUE NOT NULL, name TEXT, status TEXT, "
        "start_time TEXT, end_time TEXT, duration_ms REAL, metadata TEXT)"
    )
    conn.execute(
        "CREATE TABLE spans (span_id TEXT UNIQUE NOT NULL, trace_id TEXT, name TEXT, "
        "span_type TEXT, start_time TEXT, end_time TEXT, duration_ms REAL)"
    )
    conn.execute("CREATE TABLE projects (project_id TEXT UNIQUE NOT NULL, name TEXT UNIQUE NOT NULL)")
    conn.execute("CREATE TABLE api_keys (key TEXT UNIQUE NOT NULL, project_id TEXT)")
    conn.execute(
        "INSERT INTO traces (trace_id, name, status, start_time) VALUES (?, ?, ?, ?)",
        ("legacy-trace", "legacy", "ok", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_initialize_runs_migrations_and_preserves_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)

    backend = SQLiteBackend(db_path=db_path)
    await backend.initialize()
    try:
        trace = await backend.find_one("traces", {"trace_id": "legacy-trace"})
        assert trace is not None
        assert trace["name"] == "legacy"
        assert backend.schema_version == CURRENT_SCHEMA_VERSION

        cursor = await backend._conn.execute("PRAGMA table_info(traces)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert {"error_type", "latency_ms", "created_at", "updated_at"} <= columns
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_insert_many_commits_all_rows_on_success(tmp_path):
    backend = SQLiteBackend(db_path=tmp_path / "success.db")
    await backend.initialize()
    try:
        await backend.insert_many(
            "traces",
            [
                {"trace_id": "batch-1", "name": "one"},
                {"trace_id": "batch-2", "name": "two"},
            ],
        )
        rows = await backend.find_many("traces", sort=[("trace_id", 1)])
        assert [row["trace_id"] for row in rows] == ["batch-1", "batch-2"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_insert_many_rolls_back_all_rows_on_failure(tmp_path):
    backend = SQLiteBackend(db_path=tmp_path / "rollback.db")
    await backend.initialize()
    try:
        with pytest.raises(Exception):
            await backend.insert_many(
                "traces",
                [
                    {"trace_id": "valid-row", "name": "valid"},
                    {"name": "missing-required-trace-id"},
                ],
            )
        assert await backend.count_documents("traces") == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_metadata_timestamps_duration_and_errors_round_trip(tmp_path):
    backend = SQLiteBackend(db_path=tmp_path / "metadata.db")
    await backend.initialize()
    start = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=2)))
    end = start + timedelta(milliseconds=1500, microseconds=456)
    try:
        await backend.insert_many(
            "traces",
            [
                {
                    "trace_id": "trace-metadata",
                    "name": "metadata",
                    "start_time": start,
                    "end_time": end,
                    "error_type": "ValueError",
                    "failure_reason": "bad input",
                    "metadata": {"sampled": True},
                }
            ],
        )
        await backend.insert_many(
            "spans",
            [
                {
                    "span_id": "span-metadata",
                    "trace_id": "trace-metadata",
                    "name": "work",
                    "start_time": start,
                    "end_time": end,
                    "error_type": "TimeoutError",
                    "error": "timed out",
                    "attributes": {"retry": 1},
                }
            ],
        )

        trace = await backend.find_one("traces", {"trace_id": "trace-metadata"})
        span = await backend.find_one("spans", {"span_id": "span-metadata"})

        assert trace["start_time"] == start.astimezone(timezone.utc)
        assert trace["end_time"] == end.astimezone(timezone.utc)
        assert trace["duration_ms"] == pytest.approx(1500.456, abs=0.001)
        assert trace["created_at"].tzinfo == timezone.utc
        assert trace["updated_at"].tzinfo == timezone.utc
        assert trace["error_type"] == "ValueError"
        assert trace["failure_reason"] == "bad input"
        assert trace["metadata"]["sampled"] is True

        assert span["start_time"] == start.astimezone(timezone.utc)
        assert span["end_time"] == end.astimezone(timezone.utc)
        assert span["duration_ms"] == pytest.approx(1500.456, abs=0.001)
        assert span["error_type"] == "TimeoutError"
        assert span["error"] == "timed out"
        assert span["attributes"]["retry"] == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_duplicate_insert_is_idempotent(tmp_path):
    backend = SQLiteBackend(db_path=tmp_path / "idempotent.db")
    await backend.initialize()
    try:
        await backend.insert_one("traces", {"trace_id": "same", "name": "first"})
        await backend.insert_one("traces", {"trace_id": "same", "name": "second"})
        await backend.insert_many("traces", [{"trace_id": "same", "name": "third"}])

        assert await backend.count_documents("traces") == 1
        stored = await backend.find_one("traces", {"trace_id": "same"})
        assert stored["name"] == "third"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_existing_database_stays_compatible_across_reopen(tmp_path):
    db_path = tmp_path / "reopen.db"
    first = SQLiteBackend(db_path=db_path)
    await first.initialize()
    try:
        await first.insert_one("traces", {"trace_id": "persisted", "name": "across reopen"})
    finally:
        await first.close()

    second = SQLiteBackend(db_path=db_path)
    await second.initialize()
    try:
        trace = await second.find_one("traces", {"trace_id": "persisted"})
        assert trace is not None
        assert trace["name"] == "across reopen"
        assert second.schema_version == CURRENT_SCHEMA_VERSION
    finally:
        await second.close()
