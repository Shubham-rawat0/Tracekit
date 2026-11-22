import asyncio
import json
import logging
import sqlite3

import backend.apps.db.base  # noqa: F401
import pytest
import tracekit
import tracekit.cli as cli
import tracekit.context  # noqa: F401
from backend.apps.db.migrations import CURRENT_SCHEMA_VERSION
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.auth import validate_api_key
from tracekit.core.config import load_config
from tracekit.core.db import TraceStore
from tracekit.core.tracer import Tracer
from tracekit.logging import configure_logging, get_logger


async def _make_store(path, traces=None, spans=None):
    backend = SQLiteBackend(path)
    await backend.initialize()
    store = TraceStore(backend)
    for trace in traces or []:
        await store.save_trace(trace)
    for span in spans or []:
        await store.save_span(span)
    return backend, store


@pytest.mark.asyncio
async def test_schema_migration_preserves_existing_data(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
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

    backend, store = await _make_store(db_path)
    trace = await store.get_trace("legacy-trace")
    assert trace is not None
    assert trace["name"] == "legacy"
    await backend.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("SELECT MAX(version) FROM SCHEMA_VERSION").fetchone()[0]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(traces)")}
    assert version == CURRENT_SCHEMA_VERSION
    assert "error_type" in columns
    assert "latency_ms" in columns
    conn.close()


@pytest.mark.asyncio
async def test_collection_and_field_validation(tmp_path):
    backend, _ = await _make_store(tmp_path / "validated.db")
    with pytest.raises(ValueError, match="unsupported collection"):
        await backend.insert_one("traces; DROP TABLE traces", {"trace_id": "x"})
    with pytest.raises(ValueError, match="unsupported field"):
        await backend.find_many("traces", {"bad_field OR 1=1": "x"})
    with pytest.raises(ValueError, match="unsupported collection"):
        await backend.delete_many("unknown", {})
    await backend.close()


@pytest.mark.asyncio
async def test_batch_save_spans_uses_one_transaction_and_rolls_back(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.db"
    backend, store = await _make_store(db_path)
    spans = [
        {"span_id": f"s{i}", "trace_id": "t1", "name": f"span{i}", "span_type": "internal"}
        for i in range(3)
    ]
    await store.save_spans(spans)
    stored = await backend.find_many("spans", {})
    assert len(stored) == 3

    async def failing_executemany(*args, **kwargs):
        raise RuntimeError("batch failure")

    monkeypatch.setattr(backend._conn, "executemany", failing_executemany)
    with pytest.raises(RuntimeError, match="batch failure"):
        await store.save_spans(
            [{"span_id": "failed", "trace_id": "t1", "name": "failed", "span_type": "internal"}]
        )
    monkeypatch.undo()
    assert await backend.count_documents("spans", {"span_id": "failed"}) == 0
    await backend.close()


@pytest.mark.asyncio
async def test_delete_spans_older_than(tmp_path):
    backend, store = await _make_store(
        tmp_path / "older.db",
        spans=[
            {"span_id": "old", "trace_id": "t1", "start_time": "2020-01-01T00:00:00+00:00"},
            {"span_id": "new", "trace_id": "t1", "start_time": "2099-01-01T00:00:00+00:00"},
        ],
    )
    deleted = await store.delete_spans_older_than(7)
    assert deleted == 1
    assert [span["span_id"] for span in await backend.find_many("spans", {})] == ["new"]
    await backend.close()


@pytest.mark.asyncio
async def test_search_traces_extended_filters(tmp_path):
    backend, store = await _make_store(
        tmp_path / "search.db",
        traces=[
            {
                "trace_id": "t1",
                "name": "alpha",
                "status": "error",
                "error": '{"type": "ConnectionError"}',
                "error_type": "ConnectionError",
                "project_id": "p1",
                "environment": "production",
                "start_time": "2026-01-01T00:00:00+00:00",
                "duration_ms": 150.0,
                "latency_ms": 150.0,
            },
            {
                "trace_id": "t2",
                "name": "beta",
                "status": "ok",
                "project_id": "p2",
                "environment": "development",
                "start_time": "2026-01-02T00:00:00+00:00",
                "duration_ms": 10.0,
                "latency_ms": 10.0,
            },
        ],
    )
    result = await store.search_traces(
        status="error",
        error_type="ConnectionError",
        project_id="p1",
        environment="production",
        min_latency_ms=100,
        max_latency_ms=200,
        sort_by="latency_ms",
    )
    assert [trace["trace_id"] for trace in result] == ["t1"]
    await backend.close()


def test_cli_root_output_and_delete_older_than(tmp_path, capsys):
    db_path = tmp_path / "cli.db"

    async def setup():
        backend, _ = await _make_store(
            db_path,
            traces=[{"trace_id": "t1", "name": "alpha", "status": "error"}],
            spans=[
                {"span_id": "old", "trace_id": "t1", "start_time": "2020-01-01T00:00:00+00:00"}
            ],
        )
        await backend.close()

    asyncio.run(setup())

    assert cli.main(["--db", str(db_path), "--output", "json", "search", "--status", "error"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["trace_id"] == "t1"

    assert cli.main(["--db", str(db_path), "delete", "--older-than", "7d", "--yes"]) == 0
    assert "Deleted 1 spans" in capsys.readouterr().out

    args = cli.build_parser().parse_args(["watch", "--db", str(db_path)])
    assert args.command == "watch"
    assert args.watch is True
    assert args.db == str(db_path)


def test_config_environment(monkeypatch):
    monkeypatch.setenv("TRACEKIT_ENVIRONMENT", "staging")
    monkeypatch.setenv("TRACEKIT_AUTO_FLUSH", "true")
    monkeypatch.setenv("TRACEKIT_FLUSH_INTERVAL_SECONDS", "5")
    config = load_config(dotenv_path=None)
    assert config.environment == "staging"
    assert config.auto_flush is True
    assert config.flush_interval_seconds == 5.0


def test_cli_projects_and_api_keys(tmp_path, capsys):
    db_path = tmp_path / "admin.db"

    assert (
        cli.main(
            [
                "--db",
                str(db_path),
                "--output",
                "json",
                "projects",
                "create",
                "demo",
                "--project-id",
                "p1",
                "--description",
                "Demo project",
            ]
        )
        == 0
    )
    project_payload = json.loads(capsys.readouterr().out)
    assert project_payload[0]["project_id"] == "p1"
    assert project_payload[0]["name"] == "demo"

    assert (
        cli.main(
            [
                "--db",
                str(db_path),
                "--output",
                "json",
                "api-keys",
                "create",
                "--name",
                "ci",
                "--project-id",
                "p1",
                "--environment",
                "production",
            ]
        )
        == 0
    )
    key_payload = json.loads(capsys.readouterr().out)
    raw_key = key_payload[0]["raw_key"]
    assert raw_key.startswith("tk_")
    assert key_payload[0]["project_id"] == "p1"

    async def validate():
        backend = SQLiteBackend(db_path)
        await backend.initialize()
        try:
            return await validate_api_key(backend, raw_key)
        finally:
            await backend.close()

    record = asyncio.run(validate())
    assert record.project_id == "p1"
    assert record.environment == "production"


def test_tracer_health_check_and_public_exports():
    health = Tracer().health_check()
    assert health["status"] == "ok"
    assert health["storage"]["configured"] is True
    assert Tracer(storage=None).health_check()["storage"]["configured"] is False
    assert "sampler" in health
    assert "config" in health
    assert tracekit.Project is not None
    assert tracekit.APIKeyRecord is not None
    assert tracekit.TenantScopedStore is not None
    assert tracekit.StreamManager is not None
    assert tracekit.StreamServer is not None
    assert tracekit.WebhookTransport is not None


def test_logging_helpers(monkeypatch):
    monkeypatch.setenv("TRACEKIT_LOG_LEVEL", "debug")
    logger = configure_logging()
    assert logger.name == "tracekit"
    assert logger.level == logging.DEBUG
    assert get_logger("worker").name == "tracekit.worker"
