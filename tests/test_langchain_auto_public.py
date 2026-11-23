import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import tracekit
from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.tracer import Tracer
from tracekit.integrations import langchain_auto
from tracekit.integrations.langchain import TracekitCallbackHandler


class FakeBaseLLM:
    def __init__(self, callbacks=None, **kwargs):
        self.callbacks = list(callbacks or [])


def _clear_context():
    set_current_trace(None)
    set_current_span(None)
    set_trace_context(None)


@pytest.fixture(autouse=True)
def clean_langchain_state():
    langchain_auto.untrace_langchain()
    langchain_auto._ORIGINAL_INITS.clear()
    langchain_auto._PATCHED = False
    langchain_auto._TRACER = None
    _clear_context()
    yield
    langchain_auto.untrace_langchain()
    langchain_auto._ORIGINAL_INITS.clear()
    langchain_auto._PATCHED = False
    langchain_auto._TRACER = None
    _clear_context()


def _patch_fake_classes():
    return (
        patch.object(langchain_auto, "_BaseLLM", FakeBaseLLM),
        patch.object(langchain_auto, "_BaseChain", None),
        patch.object(langchain_auto, "_BaseTool", None),
        patch.object(langchain_auto, "_LANGCHAIN_IMPORT_ERROR", None),
    )


def _handlers(instance):
    return [
        callback
        for callback in instance.callbacks
        if isinstance(callback, TracekitCallbackHandler)
    ]


def test_trace_langchain_creates_one_managed_trace_without_explicit_context(tmp_path):
    tracer = Tracer(storage=None)
    p1, p2, p3, p4 = _patch_fake_classes()
    with p1, p2, p3, p4:
        tracekit.trace_langchain(tracer=tracer)
        handler = _handlers(FakeBaseLLM())[0]
    chain_id, llm_id, tool_id = uuid4(), uuid4(), uuid4()

    handler.on_chain_start({"name": "agent"}, {"q": "hi"}, run_id=chain_id)
    handler.on_llm_start({"name": "model"}, [{"role": "user", "content": "hi"}], run_id=llm_id, parent_run_id=chain_id)
    handler.on_tool_start({"name": "search"}, "x", run_id=tool_id, parent_run_id=chain_id)
    handler.on_tool_end("ok", run_id=tool_id, parent_run_id=chain_id)
    handler.on_llm_end(SimpleNamespace(generations=[], llm_output={}), run_id=llm_id, parent_run_id=chain_id)
    handler.on_chain_end({"a": "b"}, run_id=chain_id)

    assert len(tracer._traces) == 1
    trace = next(iter(tracer._traces.values()))
    assert trace.status == "ok"
    assert len(tracer._spans) == 3
    assert all(span.trace_id == trace.trace_id for span in tracer._spans.values())


def test_top_level_trace_langchain_flush_alias(tmp_path):
    p1, p2, p3, p4 = _patch_fake_classes()
    with p1, p2, p3, p4:
        tracer = tracekit.trace_langchain(
            flush=True,
            db_path=str(tmp_path / "traces.db"),
        )

    assert isinstance(tracer, Tracer)
    assert tracer.auto_flush is True
    instance = FakeBaseLLM(callbacks=["existing"])
    assert instance.callbacks[0] == "existing"
    assert len(_handlers(instance)) == 1


def test_existing_callbacks_are_preserved_and_handler_is_added_once(tmp_path):
    p1, p2, p3, p4 = _patch_fake_classes()
    with p1, p2, p3, p4:
        tracekit.trace_langchain(tracer=Tracer(storage=None))
        instance = FakeBaseLLM(callbacks=["first", "second"])

    assert instance.callbacks[:2] == ["first", "second"]
    assert len(_handlers(instance)) == 1


def test_instrumentation_is_idempotent_and_restorable(tmp_path):
    p1, p2, p3, p4 = _patch_fake_classes()
    original_init = FakeBaseLLM.__init__
    with p1, p2, p3, p4:
        tracer = tracekit.trace_langchain(tracer=Tracer(storage=None))
        patched_init = FakeBaseLLM.__init__
        again = tracekit.trace_langchain()
        instance = FakeBaseLLM()
        assert len(_handlers(instance)) == 1

        langchain_auto.untrace_langchain()
        assert FakeBaseLLM.__init__ is original_init
        assert tracer is again

        untouched = FakeBaseLLM(callbacks=["kept"])
        assert _handlers(untouched) == []
        assert untouched.callbacks == ["kept"]

        tracekit.trace_langchain(tracer=Tracer(storage=None))
        assert FakeBaseLLM.__init__ is not patched_init


def test_flush_and_auto_flush_compatibility(tmp_path):
    p1, p2, p3, p4 = _patch_fake_classes()
    with p1, p2, p3, p4:
        tracer = tracekit.trace_langchain(flush=True, auto_flush=True)
        assert tracer.auto_flush is True
        langchain_auto.untrace_langchain()

        tracer = tracekit.trace_langchain(auto_flush=True)
        assert tracer.auto_flush is True
        langchain_auto.untrace_langchain()

        tracer = tracekit.trace_langchain(flush=False)
        assert tracer.auto_flush is False


def test_conflicting_flush_options_raise(tmp_path):
    p1, p2, p3, p4 = _patch_fake_classes()
    with p1, p2, p3, p4:
        with pytest.raises(ValueError, match="disagree"):
            tracekit.trace_langchain(flush=True, auto_flush=False)
        with pytest.raises(ValueError, match="disagree"):
            tracekit.trace_langchain(flush=False, auto_flush=True)


def test_supplied_tracer_owns_configuration(tmp_path):
    p1, p2, p3, p4 = _patch_fake_classes()
    with p1, p2, p3, p4:
        with pytest.raises(ValueError, match="owns its configuration"):
            tracekit.trace_langchain(tracer=Tracer(storage=None), flush=True)
        with pytest.raises(ValueError, match="owns its configuration"):
            tracekit.trace_langchain(tracer=Tracer(storage=None), auto_flush=True)
        with pytest.raises(ValueError, match="owns its configuration"):
            tracekit.trace_langchain(
                tracer=Tracer(storage=None),
                db_path=str(tmp_path / "traces.db"),
            )


def test_missing_langchain_has_actionable_error():
    with (
        patch.object(
            langchain_auto,
            "_LANGCHAIN_IMPORT_ERROR",
            ImportError("No module named 'langchain_core'"),
        ),
        patch.object(langchain_auto, "_BaseLLM", None),
        patch.object(langchain_auto, "_BaseChain", None),
        patch.object(langchain_auto, "_BaseTool", None),
    ):
        with pytest.raises(ImportError, match=r'pip install tracekit\[langchain\]'):
            tracekit.trace_langchain(flush=True)


@pytest.mark.asyncio
async def test_langchain_invocation_produces_and_persists_trace(tmp_path):
    from langchain_core.language_models.fake import FakeListLLM

    db_path = tmp_path / "invocation.db"
    tracer = tracekit.trace_langchain(
        flush=True,
        db_path=str(db_path),
        project_id="proj-public",
        environment="test",
    )
    try:
        llm = FakeListLLM(responses=["hello from tracekit"])
        assert len(_handlers(llm)) == 1
        assert llm.invoke("hello") == "hello from tracekit"

        await tracer.flush()
    finally:
        try:
            await tracer.close()
        finally:
            langchain_auto.untrace_langchain()

    conn = sqlite3.connect(db_path)
    try:
        trace_count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        span_count = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        trace_row = conn.execute(
            "SELECT project_id, environment FROM traces"
        ).fetchone()
        span_row = conn.execute(
            "SELECT span_type, project_id, environment FROM spans"
        ).fetchone()
    finally:
        conn.close()

    assert trace_count == 1
    assert span_count == 1
    assert trace_row == ("proj-public", "test")
    assert span_row == ("llm", "proj-public", "test")


@pytest.mark.asyncio
async def test_untrace_closes_completed_top_level_tool_trace_before_shutdown(tmp_path):
    from langchain_core.tools import tool

    db_path = tmp_path / "tool-trace.db"
    tracer = tracekit.trace_langchain(db_path=str(db_path))

    @tool
    def root_tool(value: str) -> str:
        """Return the input value."""
        return value

    try:
        assert root_tool.invoke("persist me") == "persist me"
        assert any(trace.status == "running" for trace in tracer._traces.values())
        langchain_auto.untrace_langchain()
        await tracer.close()
    finally:
        langchain_auto.untrace_langchain()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_chat_model_invocation_produces_and_persists_span(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    db_path = tmp_path / "chat-invocation.db"
    tracer = tracekit.trace_langchain(db_path=str(db_path))
    try:
        chat_model = FakeListChatModel(responses=["hello from tracekit"])
        assert chat_model.invoke("hello").content == "hello from tracekit"

        await tracer.flush()
    finally:
        try:
            await tracer.close()
        finally:
            langchain_auto.untrace_langchain()

    conn = sqlite3.connect(db_path)
    try:
        trace_count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        span_row = conn.execute(
            "SELECT span_type, input FROM spans"
        ).fetchone()
    finally:
        conn.close()

    assert trace_count == 1
    assert span_row is not None
    assert span_row[0] == "llm"
    assert json.loads(span_row[1])[0][0]["content"] == "hello"


def test_existing_callback_manager_is_augmented_once():
    from langchain_core.callbacks import BaseCallbackHandler, CallbackManager
    from langchain_core.language_models.fake import FakeListLLM

    existing = BaseCallbackHandler()
    manager = CallbackManager(handlers=[existing], inheritable_handlers=[existing])
    tracekit.trace_langchain(tracer=Tracer(storage=None))
    try:
        llm = FakeListLLM(responses=["hello"], callbacks=manager)
        assert llm.callbacks is manager
        assert existing in manager.handlers
        assert existing in manager.inheritable_handlers
        assert sum(isinstance(h, TracekitCallbackHandler) for h in manager.handlers) == 1
        assert (
            sum(
                isinstance(h, TracekitCallbackHandler)
                for h in manager.inheritable_handlers
            )
            == 1
        )
    finally:
        langchain_auto.untrace_langchain()


@pytest.mark.asyncio
async def test_async_langchain_invocation_auto_flushes(tmp_path):
    from langchain_core.language_models.fake import FakeListLLM

    db_path = tmp_path / "async-auto.db"
    tracer = tracekit.trace_langchain(flush=True, db_path=str(db_path))
    try:
        llm = FakeListLLM(responses=["hello from async tracekit"])
        assert await llm.ainvoke("hello") == "hello from async tracekit"

        for _ in range(100):
            if not tracer._traces and not tracer._spans:
                break
            await asyncio.sleep(0.01)

        assert not tracer._traces
        assert not tracer._spans
    finally:
        try:
            await tracer.close()
        finally:
            langchain_auto.untrace_langchain()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 1
    finally:
        conn.close()


def test_auto_flush_process_exits_without_explicit_close(tmp_path):
    db_path = tmp_path / "subprocess-auto.db"
    script = f"""
import asyncio
import sqlite3

import tracekit
from langchain_core.language_models.fake import FakeListLLM
from tracekit.integrations import langchain_auto

DB_PATH = {str(db_path)!r}

async def main():
    tracer = tracekit.trace_langchain(flush=True, db_path=DB_PATH)
    llm = FakeListLLM(responses=["hello"])
    assert await llm.ainvoke("hi") == "hello"
    for _ in range(100):
        if not tracer._traces and not tracer._spans:
            break
        await asyncio.sleep(0.01)
    assert not tracer._traces
    assert not tracer._spans

asyncio.run(main())
langchain_auto.untrace_langchain()

conn = sqlite3.connect(DB_PATH)
try:
    assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 1
finally:
    conn.close()
print("ok")
"""
    env = dict(os.environ)
    sdk_path = str(Path(__file__).resolve().parents[1] / "sdk")
    existing_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        sdk_path if not existing_path else sdk_path + os.pathsep + existing_path
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_tool_created_before_trace_langchain_emits_tool_span():
    from langchain_core.tools import tool

    @tool
    def echo(value: str) -> str:
        """Echo the provided value."""
        return f"echo:{value}"

    assert getattr(echo, "callbacks", None) is None
    tracer = Tracer(storage=None)
    tracekit.trace_langchain(tracer=tracer)
    try:
        assert echo.run("abc") == "echo:abc"
    finally:
        langchain_auto.untrace_langchain()

    tool_spans = [
        span for span in tracer._spans.values() if span.span_type == "tool"
    ]
    assert len(tool_spans) == 1
    assert tool_spans[0].name == "echo"
    assert tool_spans[0].input == "abc"
    assert tool_spans[0].output == "echo:abc"


@pytest.mark.asyncio
async def test_async_tool_created_before_trace_langchain_emits_tool_span():
    from langchain_core.tools import tool

    @tool
    async def async_echo(value: str) -> str:
        """Echo the provided value asynchronously."""
        return f"async:{value}"

    assert getattr(async_echo, "callbacks", None) is None
    tracer = Tracer(storage=None)
    tracekit.trace_langchain(tracer=tracer)
    try:
        assert await async_echo.arun("xyz") == "async:xyz"
    finally:
        langchain_auto.untrace_langchain()

    tool_spans = [
        span for span in tracer._spans.values() if span.span_type == "tool"
    ]
    assert len(tool_spans) == 1
    assert tool_spans[0].name == "async_echo"
    assert tool_spans[0].input == "xyz"
    assert tool_spans[0].output == "async:xyz"


def test_untrace_langchain_restores_tool_run_methods():
    from langchain_core.tools.base import BaseTool

    original_run = BaseTool.run
    original_arun = BaseTool.arun
    tracekit.trace_langchain(tracer=Tracer(storage=None))
    assert BaseTool.run is not original_run
    assert BaseTool.arun is not original_arun

    langchain_auto.untrace_langchain()
    assert BaseTool.run is original_run
    assert BaseTool.arun is original_arun
