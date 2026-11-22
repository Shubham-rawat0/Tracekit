import asyncio
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.db import TraceStore
from tracekit.core.tracer import Tracer
from tracekit.dashboard.server import start_dashboard_server, stop_dashboard_server
from tracekit.replay import clear_replay_handlers, register_replay_handler


@pytest.fixture
def _server(tmp_path):
    db = tmp_path / "advanced.db"
    token = "advanced-token"
    server, loop, loop_thread = start_dashboard_server(
        db_path=db, host="127.0.0.1", port=0, token=token
    )
    yield SimpleNamespace(server=server, loop=loop, loop_thread=loop_thread, db=db, token=token)
    stop_dashboard_server(server, loop, loop_thread)


@pytest.fixture(autouse=True)
def _replay_handlers():
    clear_replay_handlers()
    yield
    clear_replay_handlers()


def _request(_server, path, token=None, method="GET", body=None):
    host, port = _server.server.server_address[:2]
    headers = {"Host": f"{host}:{port}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://{host}:{port}{path}", headers=headers, data=data, method=method
    )
    return urllib.request.urlopen(request, timeout=10)


def _json(_server, path, token=None, method="GET", body=None):
    with _request(_server, path, token=token, method=method, body=body) as response:
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


def _fixture_data():
    start = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    traces = [
        {
            "trace_id": "trace-llm",
            "name": "alpha",
            "status": "ok",
            "project_id": "proj-a",
            "environment": "test",
            "start_time": start,
            "end_time": start + timedelta(milliseconds=100),
            "model_name": "gpt-4",
            "token_count": 12,
            "metadata": {"provider": "openai", "total_cost_usd": 0.01},
        },
        {
            "trace_id": "trace-error",
            "name": "beta",
            "status": "error",
            "project_id": "proj-b",
            "environment": "test",
            "start_time": start + timedelta(minutes=1),
            "end_time": start + timedelta(minutes=1, milliseconds=200),
            "error": "boom",
            "error_type": "ValueError",
        },
    ]
    spans = [
        {
            "span_id": "span-llm",
            "trace_id": "trace-llm",
            "span_type": "llm",
            "name": "llm-call",
            "status": "ok",
            "start_time": start,
            "end_time": start + timedelta(milliseconds=50),
            "input": "hi",
            "output": "hello",
            "attributes": {
                "model": "gpt-4",
                "provider": "openai",
                "token_usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "total_tokens": 12,
                },
                "cost_usd": 0.01,
                "invocation_params": {"temperature": 0.2},
            },
        },
        {
            "span_id": "span-tool",
            "trace_id": "trace-error",
            "span_type": "tool",
            "name": "tool-call",
            "status": "error",
            "start_time": start + timedelta(minutes=1),
            "end_time": start + timedelta(minutes=1, milliseconds=100),
            "error": "boom",
            "events": [
                {
                    "name": "exception",
                    "attributes": {
                        "exception.type": "ValueError",
                        "exception.message": "boom",
                        "exception.stacktrace": "Traceback...",
                    },
                }
            ],
        },
    ]
    return traces, spans


def test_trace_query_supports_search_sort_and_pagination(_server):
    traces, spans = _fixture_data()
    _seed(_server, traces, spans)

    payload = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"q": "alpha", "limit": 1}),
        token=_server.token,
    )
    assert payload["total"] == 1
    assert [item["trace_id"] for item in payload["items"]] == ["trace-llm"]
    assert payload["limit"] == 1

    sorted_payload = _json(
        _server,
        "/api/traces/query?"
        + urllib.parse.urlencode({"sort_by": "duration_ms", "sort_desc": "false", "limit": 10}),
        token=_server.token,
    )
    assert sorted_payload["total"] == 2

    legacy = _json(_server, "/api/traces", token=_server.token)
    assert isinstance(legacy, list)
    assert len(legacy) == 2


def test_trace_query_exposes_prompt_and_response(_server):
    start = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    traces = [
        {
            "trace_id": "trace-meta",
            "name": "meta",
            "status": "ok",
            "start_time": start,
            "end_time": start + timedelta(milliseconds=100),
            "metadata": {"prompt": "metadata prompt", "response": "metadata response"},
        },
        {
            "trace_id": "trace-span",
            "name": "span",
            "status": "ok",
            "start_time": start + timedelta(minutes=1),
            "end_time": start + timedelta(minutes=1, milliseconds=100),
        },
    ]
    spans = [
        {
            "span_id": "span-io",
            "trace_id": "trace-span",
            "span_type": "llm",
            "name": "llm",
            "status": "ok",
            "start_time": start + timedelta(minutes=1),
            "end_time": start + timedelta(minutes=1, milliseconds=50),
            "input": "span input",
            "output": "span output",
            "attributes": {
                "model": "span-model",
                "token_usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        }
    ]
    _seed(_server, traces, spans)

    meta_payload = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"q": "meta"}),
        token=_server.token,
    )
    meta_item = meta_payload["items"][0]
    assert meta_item["prompt"] == "metadata prompt"
    assert meta_item["response"] == "metadata response"

    span_payload = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"q": "span"}),
        token=_server.token,
    )
    span_item = span_payload["items"][0]
    assert span_item["prompt"] == "span input"
    assert span_item["response"] == "span output"
    assert span_item["model_name"] == "span-model"
    assert span_item["total_tokens"] == 5


def test_trace_query_does_not_treat_missing_data_as_zero(_server):
    traces, spans = _fixture_data()
    _seed(_server, [traces[0]], [spans[0]])
    payload = _json(_server, "/api/analytics", token=_server.token)
    summary = payload["summary"]
    assert summary["total_tokens"] == 12
    assert summary["total_cost_usd"] == pytest.approx(0.01)
    assert summary["tokens_available"] is True
    assert summary["cost_available"] is True

    _seed(_server, [{"trace_id": "sparse", "name": "sparse", "status": "ok"}], [])
    sparse = _json(
        _server,
        "/api/analytics?" + urllib.parse.urlencode({"name": "sparse"}),
        token=_server.token,
    )
    assert sparse["summary"]["total_tokens"] is None
    assert sparse["summary"]["total_cost_usd"] is None
    assert sparse["summary"]["tokens_available"] is False
    assert sparse["summary"]["cost_available"] is False


def test_analytics_contains_real_models_errors_and_timeseries(_server):
    traces, spans = _fixture_data()
    _seed(_server, traces, spans)
    payload = _json(
        _server,
        "/api/analytics?" + urllib.parse.urlencode({"bucket": "hour"}),
        token=_server.token,
    )
    assert payload["summary"]["total_llm_calls"] == 1
    assert payload["summary"]["error_rate"] == pytest.approx(0.5)
    assert payload["models"][0]["model"] == "gpt-4"
    assert payload["errors"][0]["error_type"] == "ValueError"
    assert payload["errors"][0]["stacktrace"] == "Traceback..."
    assert sum(point["traces"] for point in payload["timeseries"]) == 2
    assert payload["slowest"]["traces"][0]["trace_id"] == "trace-error"


def test_export_supports_jsonl_and_csv(_server):
    traces, spans = _fixture_data()
    _seed(_server, traces, spans)
    with _request(
        _server,
        "/api/export?" + urllib.parse.urlencode({"format": "jsonl"}),
        token=_server.token,
    ) as response:
        rows = [json.loads(line) for line in response.read().decode("utf-8").splitlines()]
    assert {row["trace_id"] for row in rows} == {"trace-llm", "trace-error"}
    assert next(row for row in rows if row["trace_id"] == "trace-llm")["total_tokens"] == 12

    with _request(
        _server,
        "/api/export?" + urllib.parse.urlencode({"format": "csv"}),
        token=_server.token,
    ) as response:
        csv_body = response.read().decode("utf-8")
    assert "trace_id,name,status" in csv_body
    assert "trace-llm" in csv_body


def test_replay_requires_handler_then_records_linked_trace(_server):
    traces, spans = _fixture_data()
    _seed(_server, traces, spans)

    assert _json(_server, "/api/replay/providers", token=_server.token) == {"providers": []}
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(
            _server,
            "/api/replay",
            token=_server.token,
            method="POST",
            body={"trace_id": "trace-llm", "span_id": "span-llm", "prompt": "edited"},
        )
    assert exc.value.code == 422

    register_replay_handler(
        "openai",
        lambda request: {
            "output": "replayed",
            "model": request.model,
            "provider": request.provider,
            "token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "latency_ms": 10.0,
        },
    )
    result = _json(
        _server,
        "/api/replay",
        token=_server.token,
        method="POST",
        body={"trace_id": "trace-llm", "span_id": "span-llm", "prompt": "edited"},
    )
    assert result["replay_of_trace_id"] == "trace-llm"
    assert result["comparison"]["original"]["input"] == "hi"
    assert result["comparison"]["replay"]["input"] == "edited"
    assert "deterministically" in result["warning"]

    replayed = _json(
        _server,
        "/api/traces/" + result["trace_id"],
        token=_server.token,
    )
    assert replayed["prompt"] == "edited"
    assert replayed["response"] == "replayed"
    assert replayed["metadata"]["replay_of_trace_id"] == "trace-llm"


def test_sdk_derives_top_level_trace_fields(tmp_path):
    async def run():
        backend = SQLiteBackend(db_path=tmp_path / "sdk.db")
        await backend.initialize()
        tracer = Tracer(storage=backend, auto_flush=False, project_id="p", environment="test")
        with tracer.trace("sdk-trace"):
            span = tracer.start_llm_span("call", model="model-x", prompt="question")
            tracer.record_llm_result(
                span,
                response="answer",
                model="model-x",
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
            )
            tracer.end_span(span)
        await tracer.flush()
        doc = await backend.find_one("traces", {"name": "sdk-trace"})
        await backend.close()
        return doc

    doc = asyncio.run(run())
    assert doc is not None
    assert doc["prompt"] == "question"
    assert doc["response"] == "answer"
    assert doc["token_count"] == 5
    assert doc["model_name"] == "model-x"


def test_trace_query_uses_ls_model_name_fallback(_server):
    start = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    _seed(
        _server,
        [
            {
                "trace_id": "ls-model",
                "name": "ls-model",
                "status": "ok",
                "model_name": "",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=100),
            }
        ],
        [
            {
                "span_id": "span-ls-model",
                "trace_id": "ls-model",
                "span_type": "llm",
                "name": "llm-call",
                "status": "ok",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=50),
                "attributes": {"ls_model_name": "gemini-2.0-flash"},
            }
        ],
    )

    query = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"trace_id": "ls-model"}),
        token=_server.token,
    )
    assert query["items"][0]["model_name"] == "gemini-2.0-flash"

    analytics = _json(
        _server,
        "/api/analytics?" + urllib.parse.urlencode({"trace_id": "ls-model"}),
        token=_server.token,
    )
    assert analytics["models"][0]["model"] == "gemini-2.0-flash"


def test_error_events_mark_trace_status_as_error(_server):
    start = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    _seed(
        _server,
        [
            {
                "trace_id": "event-error",
                "name": "event-error",
                "status": "ok",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=100),
            }
        ],
        [
            {
                "span_id": "span-event-error",
                "trace_id": "event-error",
                "span_type": "tool",
                "name": "tool-call",
                "status": "error",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=50),
                "events": [
                    {
                        "name": "exception",
                        "attributes": {
                            "exception.type": "ValueError",
                            "exception.message": "boom",
                        },
                    }
                ],
            }
        ],
    )

    query = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"trace_id": "event-error"}),
        token=_server.token,
    )
    assert query["items"][0]["status"] == "error"

    analytics = _json(
        _server,
        "/api/analytics?" + urllib.parse.urlencode({"trace_id": "event-error"}),
        token=_server.token,
    )
    assert analytics["summary"]["failed_traces"] == 1


def test_span_error_status_marks_trace_status_as_error(_server):
    start = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    _seed(
        _server,
        [
            {
                "trace_id": "span-status-error",
                "name": "span-status-error",
                "status": "ok",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=100),
            }
        ],
        [
            {
                "span_id": "span-status-error-child",
                "trace_id": "span-status-error",
                "span_type": "tool",
                "name": "tool-call",
                "status": "error",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=50),
            }
        ],
    )

    query = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"trace_id": "span-status-error"}),
        token=_server.token,
    )
    assert query["items"][0]["status"] == "error"

    detail = _json(
        _server,
        "/api/traces/span-status-error",
        token=_server.token,
    )
    assert detail["status"] == "error"


def test_span_error_attribute_marks_trace_status_as_error(_server):
    start = datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc)
    _seed(
        _server,
        [
            {
                "trace_id": "span-attribute-error",
                "name": "span-attribute-error",
                "status": "ok",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=100),
            }
        ],
        [
            {
                "span_id": "span-attribute-error-child",
                "trace_id": "span-attribute-error",
                "span_type": "tool",
                "name": "tool-call",
                "status": "ok",
                "start_time": start,
                "end_time": start + timedelta(milliseconds=50),
                "attributes": {"error.type": "ValueError", "error.message": "boom"},
            }
        ],
    )

    query = _json(
        _server,
        "/api/traces/query?" + urllib.parse.urlencode({"trace_id": "span-attribute-error"}),
        token=_server.token,
    )
    assert query["items"][0]["status"] == "error"
