import asyncio
import json
import threading
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.db import TraceStore
from tracekit.dashboard.server import start_dashboard_server, stop_dashboard_server


@pytest.fixture
def _server(tmp_path):
    db = tmp_path / "llm-detect.db"
    token = "detect-token"
    server, loop, loop_thread = start_dashboard_server(
        db_path=db, host="127.0.0.1", port=0, token=token
    )
    yield SimpleNamespace(server=server, loop=loop, loop_thread=loop_thread, db=db, token=token)
    stop_dashboard_server(server, loop, loop_thread)


def _json(_server, path):
    host, port = _server.server.server_address[:2]
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        headers={"Host": f"{host}:{port}", "Authorization": f"Bearer {_server.token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _seed(_server, traces, spans):
    async def run():
        backend = SQLiteBackend(db_path=_server.db)
        await backend.initialize()
        try:
            store = TraceStore(backend)
            for trace in traces:
                await store.save_trace(trace)
            for span in spans:
                await store.save_span(span)
        finally:
            await backend.close()

    asyncio.run(run())


def test_non_llm_generation_span_detected_and_tool_excluded(_server):
    traces = [{"trace_id": "t1", "name": "mixed", "status": "ok"}]
    spans = [
        {
            "span_id": "s-gen",
            "trace_id": "t1",
            "span_type": "generation",
            "name": "model-call",
            "status": "ok",
            "input": "prompt",
            "output": "answer",
            "attributes": {"model": "gpt-x"},
        },
        {
            "span_id": "s-tool",
            "trace_id": "t1",
            "span_type": "tool",
            "name": "search",
            "status": "ok",
            "input": "q",
            "output": "r",
        },
    ]
    _seed(_server, traces, spans)

    analytics = _json(_server, "/api/analytics")
    assert analytics["summary"]["total_llm_calls"] == 1

    replay = _json(_server, "/api/traces/t1/replay")
    assert replay["replay_spec"]["eligible"] is True
    assert [s["span_id"] for s in replay["replay_spec"]["llm_spans"]] == ["s-gen"]


def test_nested_alternate_token_usage_and_zero_vs_missing(_server):
    traces = [
        {"trace_id": "t-nested", "name": "nested", "status": "ok"},
        {"trace_id": "t-zero", "name": "zero", "status": "ok"},
        {"trace_id": "t-missing", "name": "missing", "status": "ok"},
    ]
    spans = [
        {
            "span_id": "s-nested",
            "trace_id": "t-nested",
            "span_type": "chat",
            "name": "chat.completions.create",
            "status": "ok",
            "input": "hi",
            "attributes": {
                "gen_ai.usage.input_tokens": 3,
                "usage": {"completionTokens": 4},
            },
        },
        {
            "span_id": "s-zero",
            "trace_id": "t-zero",
            "span_type": "generation",
            "name": "generate",
            "status": "ok",
            "input": "hi",
            "attributes": {"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}},
        },
        {
            "span_id": "s-missing",
            "trace_id": "t-missing",
            "span_type": "generation",
            "name": "generate-missing",
            "status": "ok",
            "input": "hi",
            "attributes": {"model": "x"},
        },
    ]
    _seed(_server, traces, spans)

    nested = _json(_server, "/api/analytics?" + urllib.parse.urlencode({"name": "nested"}))["summary"]
    assert nested["input_tokens"] == 3
    assert nested["output_tokens"] == 4
    assert nested["total_tokens"] == 7
    assert nested["tokens_available"] is True

    zero = _json(_server, "/api/analytics?" + urllib.parse.urlencode({"name": "zero"}))["summary"]
    assert zero["input_tokens"] == 0
    assert zero["output_tokens"] == 0
    assert zero["total_tokens"] == 0
    assert zero["tokens_available"] is True

    missing = _json(_server, "/api/analytics?" + urllib.parse.urlencode({"name": "missing"}))["summary"]
    assert missing["input_tokens"] is None
    assert missing["output_tokens"] is None
    assert missing["total_tokens"] is None
    assert missing["tokens_available"] is False
