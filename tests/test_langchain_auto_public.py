import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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
