import sqlite3

import pytest
from backend.apps.db.migrations import CURRENT_SCHEMA_VERSION
from backend.apps.db.sqllite import SQLiteBackend
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.tools import tool
from tracekit import Tracer, trace_langchain
from tracekit.core.span import SpanType
from tracekit.integrations import langchain_auto


@pytest.fixture(autouse=True)
def clean_langchain_state():
    langchain_auto.untrace_langchain()
    langchain_auto._ORIGINAL_INITS.clear()
    langchain_auto._PATCHED = False
    langchain_auto._TRACER = None
    yield
    langchain_auto.untrace_langchain()
    langchain_auto._ORIGINAL_INITS.clear()
    langchain_auto._PATCHED = False
    langchain_auto._TRACER = None


def _failing_tool():
    raise RuntimeError("service unavailable")


def _successful_llm():
    return "report"


def _spans_by_name(tracer):
    return {span.name: span for span in tracer._spans.values()}


def test_handled_failure_then_final_llm_makes_trace_ok():
    tracer = Tracer()
    trace = tracer.start_trace("workflow")

    assert tracer.run_span("flaky-tool", _failing_tool, allow_failure=True) is None
    tracer.run_span(
        "gemini-final-summary",
        _successful_llm,
        span_type=SpanType.LLM,
        mark_final=True,
    )
    tracer.end_trace(trace)

    spans = _spans_by_name(tracer)
    assert spans["flaky-tool"].status == "error"
    assert spans["flaky-tool"].error == "service unavailable"
    assert spans["flaky-tool"].handled is True
    assert spans["gemini-final-summary"].is_final is True
    assert trace.status == "ok"


def test_unhandled_failure_is_not_ignored():
    tracer = Tracer()
    trace = tracer.start_trace("workflow")

    with pytest.raises(RuntimeError):
        tracer.run_span("fatal-tool", _failing_tool)

    tracer.end_trace(trace)
    assert trace.status == "error"


def test_status_falls_back_to_last_finished_span_without_final_marker():
    tracer = Tracer()
    trace = tracer.start_trace("workflow")

    tracer.run_span("handled-tool", _failing_tool, allow_failure=True)
    assert tracer.run_span("last-step", lambda: "done") == "done"
    tracer.end_trace(trace)

    assert not any(span.is_final for span in tracer._spans.values())
    assert trace.status == "ok"


@pytest.mark.asyncio
async def test_final_and_handled_fields_round_trip_through_sqlite(tmp_path):
    db_path = tmp_path / "final-status.db"
    tracer = Tracer(db_path=str(db_path))

    with tracer.trace("workflow"):
        tracer.run_span("flaky-tool", _failing_tool, allow_failure=True)
        tracer.run_span("final-llm", _successful_llm, mark_final=True)

    await tracer.flush()
    await tracer.close()

    conn = sqlite3.connect(db_path)
    try:
        trace_status = conn.execute("SELECT status FROM traces").fetchone()[0]
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT name, status, error, is_final, handled FROM spans"
            ).fetchall()
        }
    finally:
        conn.close()

    assert trace_status == "ok"
    assert rows["flaky-tool"] == ("error", "service unavailable", 0, 1)
    assert rows["final-llm"] == ("ok", None, 1, 0)


@pytest.mark.asyncio
async def test_migration_adds_final_columns_to_legacy_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
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
        conn.commit()
    finally:
        conn.close()

    backend = SQLiteBackend(db_path=db_path)
    await backend.initialize()
    try:
        cursor = await backend._conn.execute("PRAGMA table_info(spans)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert {"is_final", "handled"} <= columns
        assert backend.schema_version == CURRENT_SCHEMA_VERSION
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_langchain_handles_tool_failure_and_marks_final_llm(tmp_path):
    db_path = tmp_path / "langchain-final-status.db"
    tracer = trace_langchain(db_path=str(db_path), mark_final=True)

    @tool
    def flaky_tool(value: str) -> str:
        """Fail on purpose."""
        raise RuntimeError("inventory service unavailable")

    try:
        with pytest.raises(RuntimeError):
            flaky_tool.invoke("USER-101")

        llm = FakeListLLM(responses=["Workflow completed."])
        assert llm.invoke("Summarize the results") == "Workflow completed."

        spans = _spans_by_name(tracer)
        assert spans["flaky_tool"].handled is True
        assert spans["flaky_tool"].error == "inventory service unavailable"
        assert spans["FakeListLLM"].is_final is True

        langchain_auto.untrace_langchain()
        await tracer.close()
    finally:
        langchain_auto.untrace_langchain()

    conn = sqlite3.connect(db_path)
    try:
        trace_status = conn.execute("SELECT status FROM traces").fetchone()[0]
        tool_row = conn.execute(
            "SELECT status, error, handled FROM spans WHERE name = ?", ("flaky_tool",)
        ).fetchone()
        final_row = conn.execute(
            "SELECT status, is_final FROM spans WHERE name = ?", ("FakeListLLM",)
        ).fetchone()
    finally:
        conn.close()

    assert trace_status == "ok"
    assert tool_row == ("error", "inventory service unavailable", 1)
    assert final_row == ("ok", 1)


@pytest.mark.asyncio
async def test_langchain_per_invocation_override_is_honored(tmp_path):
    db_path = tmp_path / "langchain-override.db"
    tracer = trace_langchain(db_path=str(db_path))

    @tool
    def strict_tool(value: str) -> str:
        """Fail with per-invocation override."""
        raise RuntimeError("strict failure")

    try:
        with pytest.raises(RuntimeError, match="strict failure"):
            strict_tool.invoke("USER-101", allow_failure=False)

        spans = _spans_by_name(tracer)
        assert spans["strict_tool"].handled is False

        langchain_auto.untrace_langchain()
        await tracer.close()
    finally:
        langchain_auto.untrace_langchain()

    conn = sqlite3.connect(db_path)
    try:
        trace_status = conn.execute("SELECT status FROM traces").fetchone()[0]
    finally:
        conn.close()

    assert trace_status == "error"
