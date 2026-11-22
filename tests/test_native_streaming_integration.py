import asyncio
import json

import pytest
from backend.apps.db.sqllite import SQLiteBackend
from tracekit import StreamManager, Tracer


@pytest.mark.asyncio
async def test_default_sqlite_persists_trace_and_span_after_flush(tmp_path):
    db_path = tmp_path / "traces.db"
    tracer = Tracer(db_path=str(db_path))

    assert not db_path.exists()
    assert tracer.storage is None
    assert tracer.health_check()["storage"]["configured"] is True

    trace = tracer.start_trace("persisted_trace")
    span = tracer.start_llm_span(
        "persisted_span", model="test-model", prompt="INPUT_MARKER", trace=trace
    )
    tracer.record_llm_result(
        span, response="OUTPUT_MARKER", prompt_tokens=2, completion_tokens=3
    )
    tracer.end_span(span)
    tracer.end_trace(trace)

    assert not db_path.exists()
    await tracer.flush()
    assert db_path.exists()

    reader = SQLiteBackend(db_path=str(db_path))
    await reader.initialize()
    try:
        stored_trace = await reader.find_one("traces", {"trace_id": trace.trace_id})
        stored_span = await reader.find_one("spans", {"span_id": span.span_id})
        assert stored_trace is not None
        assert stored_span is not None
        assert stored_trace["name"] == "persisted_trace"
        assert stored_trace["prompt"] == "INPUT_MARKER"
        assert stored_trace["response"] == "OUTPUT_MARKER"
        assert stored_trace["token_count"] == 5
        assert stored_span["name"] == "persisted_span"
        assert stored_span["input"] == "INPUT_MARKER"
        assert stored_span["output"] == "OUTPUT_MARKER"
    finally:
        await reader.close()
        await tracer.close()


@pytest.mark.asyncio
async def test_explicit_no_storage_creates_no_database(tmp_path):
    db_path = tmp_path / "unused.db"
    tracer = Tracer(storage=None, db_path=str(db_path))

    trace = tracer.start_trace("ephemeral_trace")
    span = tracer.start_span("ephemeral_span", trace=trace)
    tracer.end_span(span)
    tracer.end_trace(trace)
    await tracer.flush()

    assert not db_path.exists()
    assert tracer.health_check()["storage"]["configured"] is False


@pytest.mark.asyncio
async def test_stream_manager_receives_lifecycle_events():
    events = []

    class Collector:
        def publish(self, event):
            events.append(event)

    collector = Collector()
    tracer = Tracer(storage=None, stream_manager=collector)

    trace = tracer.start_trace("streamed_trace")
    span = tracer.start_span("streamed_span", trace=trace)
    tracer.end_span(span)
    tracer.end_trace(trace)

    assert [event["type"] for event in events] == [
        "trace_start",
        "span_start",
        "span_end",
        "trace_end",
    ]
    assert all(event["trace_id"] == trace.trace_id for event in events)
    assert all(event["sampled"] is True for event in events)
    assert events[1]["span_id"] == span.span_id
    assert events[1]["span"]["name"] == "streamed_span"
    assert events[0]["trace"]["name"] == "streamed_trace"


@pytest.mark.asyncio
async def test_websocket_server_streams_lifecycle_events_to_connected_client():
    websockets = pytest.importorskip("websockets")
    manager = StreamManager()
    tracer = Tracer(
        storage=None,
        stream_enabled=True,
        stream_host="127.0.0.1",
        stream_port=0,
        stream_manager=manager,
    )
    server = await tracer.start_stream_server()
    assert server is not None

    try:
        async with websockets.connect(server.url) as websocket:
            for _ in range(100):
                if server.client_count == 1:
                    break
                await asyncio.sleep(0.01)
            assert server.client_count == 1

            trace = tracer.start_trace("ws_trace")
            span = tracer.start_span("ws_span", trace=trace)
            tracer.end_span(span)
            tracer.end_trace(trace)

            received = [json.loads(await asyncio.wait_for(websocket.recv(), 2)) for _ in range(4)]
            assert [event["type"] for event in received] == [
                "trace_start",
                "span_start",
                "span_end",
                "trace_end",
            ]
            assert all(event["trace_id"] == trace.trace_id for event in received)
            assert received[1]["span_id"] == span.span_id
    finally:
        await tracer.stop_stream_server()


@pytest.mark.asyncio
async def test_stream_server_failure_does_not_break_tracing():
    class BrokenManager:
        def publish(self, event):
            raise RuntimeError("publisher unavailable")

    tracer = Tracer(storage=None, stream_manager=BrokenManager())
    trace = tracer.start_trace("resilient_trace")
    span = tracer.start_span("resilient_span", trace=trace)
    tracer.end_span(span)
    tracer.end_trace(trace)

    assert trace.status == "ok"
    assert span.status == "ok"
