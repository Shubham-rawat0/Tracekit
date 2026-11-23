import pytest
import tracekit
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.db import TraceStore
from tracekit.core.tracer import Tracer
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


async def test_db_path_kwarg_reaches_sqlite_backend_via_trace_langchain(tmp_path):
    from langchain_core.language_models.fake import FakeListLLM

    db_path = tmp_path / "dashboard-foundation.db"
    tracer = tracekit.trace_langchain(flush=True, db_path=str(db_path))
    assert isinstance(tracer, Tracer)
    assert tracer._default_db_path == str(db_path)

    try:
        llm = FakeListLLM(responses=["foundation ok"])
        assert llm.invoke("hello") == "foundation ok"
        await tracer.flush()
    finally:
        await tracer.close()

    assert db_path.exists()

    backend = SQLiteBackend(db_path=db_path)
    await backend.initialize()
    try:
        store = TraceStore(backend)
        traces = await store.list_traces()
        assert len(traces) == 1
        stored = await store.get_trace(traces[0]["trace_id"])
        assert stored is not None
        assert stored["trace_id"] == traces[0]["trace_id"]
        assert stored["name"]
        assert await store.get_trace("missing-trace") is None
    finally:
        await backend.close()


def test_sqlite_backend_expands_user_in_explicit_db_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    backend = SQLiteBackend(db_path="~/.tracellm/custom.db")
    assert backend._db_path == tmp_path / ".tracellm" / "custom.db"


def test_default_sqlite_backend_resolves_environment_when_created(monkeypatch, tmp_path):
    db_path = tmp_path / "late-env.db"
    monkeypatch.setenv("TRACEKIT_DB_PATH", str(db_path))

    backend = SQLiteBackend()

    assert backend._db_path == db_path
