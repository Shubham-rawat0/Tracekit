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
from tracekit.dashboard.server import start_dashboard_server, stop_dashboard_server


@pytest.fixture
def _server(tmp_path):
    db = tmp_path / "test.db"
    token = "test-token-abc"
    server, loop, loop_thread = start_dashboard_server(
        db_path=db, host="127.0.0.1", port=0, token=token
    )
    yield SimpleNamespace(server=server, loop=loop, loop_thread=loop_thread, db=db, token=token)
    stop_dashboard_server(server, loop, loop_thread)


@pytest.fixture
def dashboard_server(_server):
    host, port = _server.server.server_address[:2]
    yield host, port, _server.token


def _get(_server, path, token=None, headers=None, opener=urllib.request.urlopen):
    host, port = _server.server.server_address[:2]
    request_headers = {"Host": f"{host}:{port}"}
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    if headers:
        request_headers.update(headers)
    url = f"http://{host}:{port}{path}"
    return opener(urllib.request.Request(url, headers=request_headers), timeout=10)


def _json(_server, path, token=None, headers=None):
    with _get(_server, path, token=token, headers=headers) as response:
        return json.loads(response.read().decode("utf-8"))


async def _seed(db_path, traces=(), spans=()):
    backend = SQLiteBackend(db_path=db_path)
    await backend.initialize()
    try:
        store = TraceStore(backend)
        for trace in traces:
            await store.save_trace(trace)
        for span in spans:
            await store.save_span(span)
    finally:
        await backend.close()


def _seed_sync(_server, traces=(), spans=()):
    asyncio.run(_seed(_server.db, traces, spans))


def _trace(trace_id="trace-1", name="alpha", status="ok", project_id="proj-a", **extra):
    start = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    row = {
        "trace_id": trace_id,
        "name": name,
        "status": status,
        "project_id": project_id,
        "environment": "test",
        "start_time": start,
        "end_time": start + timedelta(milliseconds=125),
    }
    row.update(extra)
    return row


def test_missing_token_returns_403(dashboard_server):
    host, port, _ = dashboard_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=10)
    assert exc.value.code == 403


def test_wrong_token_returns_403(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(_server, "/api/health", token="wrong-token")
    assert exc.value.code == 403


def test_correct_token_returns_200(_server):
    with _get(_server, "/api/health", token=_server.token) as response:
        assert response.status == 200
        assert json.loads(response.read().decode("utf-8"))["status"] == "ok"


def test_query_string_token_works(_server):
    payload = _json(_server, f"/api/health?token={_server.token}")
    assert payload["status"] == "ok"


def test_bad_host_header_returns_403(_server):
    host, port = _server.server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/api/health",
        headers={"Host": "evil.com", "Authorization": f"Bearer {_server.token}"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 403


def test_bad_host_header_on_static_returns_403(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(_server, "/", headers={"Host": "evil.com"})
    assert exc.value.code == 403


def test_index_served_without_token(dashboard_server):
    host, port, _ = dashboard_server
    with urllib.request.urlopen(f"http://{host}:{port}/", timeout=10) as response:
        assert response.status == 200
        body = response.read().decode("utf-8")
        assert "<html" in body.lower()
        assert "app.js" in body


def test_static_assets_served_without_token(_server):
    for path, expected in (("/app.js", "Authorization"), ("/styles.css", "body")):
        with _get(_server, path) as response:
            assert response.status == 200
            assert expected in response.read().decode("utf-8")


def test_static_content_type(_server):
    with _get(_server, "/app.js") as response:
        assert "javascript" in response.headers.get("Content-Type", "")


def test_path_traversal_blocked(_server):
    host, port = _server.server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/../../etc/passwd",
        headers={
            "Host": f"{host}:{port}",
            "Authorization": f"Bearer {_server.token}",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 403


def test_unknown_static_path_is_404(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(_server, "/does-not-exist.js")
    assert exc.value.code == 404


def test_no_cors_headers(_server):
    with _get(_server, "/api/health", token=_server.token) as response:
        assert "Access-Control-Allow-Origin" not in response.headers


def test_get_health(dashboard_server):
    host, port, token = dashboard_server
    request = urllib.request.Request(
        f"http://{host}:{port}/api/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["status"] == "ok"
    assert payload["storage_type"] == "sqlite"


def test_get_config(_server):
    payload = _json(_server, "/api/config", token=_server.token)
    assert payload["db_path"].endswith("test.db")
    assert payload["version"] == "0.2.0"


def test_get_traces_returns_list(_server):
    _seed_sync(_server, traces=[_trace(), _trace("trace-2", "beta", "error", "proj-b")])
    payload = _json(_server, "/api/traces", token=_server.token)
    assert isinstance(payload, list)
    assert {row["trace_id"] for row in payload} == {"trace-1", "trace-2"}
    assert set(payload[0]) >= {
        "trace_id",
        "name",
        "status",
        "project_id",
        "environment",
        "start_time",
        "duration_ms",
    }


def test_trace_list_normalizes_duration_ms(_server):
    _seed_sync(
        _server,
        traces=[_trace(duration_ms=None, latency_ms=77.5, end_time=None)],
    )
    payload = _json(_server, "/api/traces", token=_server.token)
    assert payload[0]["duration_ms"] == pytest.approx(77.5)


def test_get_trace_detail(_server):
    _seed_sync(
        _server,
        traces=[
            _trace(
                prompt="tell me a joke",
                response="why did the chicken...",
                attributes={"model": "fake"},
            )
        ],
    )
    payload = _json(_server, "/api/traces/trace-1", token=_server.token)
    assert payload["name"] == "alpha"
    assert payload["prompt"] == "tell me a joke"
    assert payload["response"] == "why did the chicken..."
    assert payload["attributes"]["model"] == "fake"


def test_get_trace_detail_not_found(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(_server, "/api/traces/missing", token=_server.token)
    assert exc.value.code == 404


def test_get_trace_replay_tree_shape(_server):
    base = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    _seed_sync(
        _server,
        traces=[_trace()],
        spans=[
            {
                "span_id": "root-span",
                "trace_id": "trace-1",
                "name": "root",
                "span_type": "chain",
                "start_time": base,
                "end_time": base + timedelta(milliseconds=100),
            },
            {
                "span_id": "child-span",
                "trace_id": "trace-1",
                "parent_span_id": "root-span",
                "name": "child",
                "span_type": "llm",
                "start_time": base + timedelta(milliseconds=10),
                "end_time": base + timedelta(milliseconds=50),
            },
        ],
    )
    payload = _json(_server, "/api/traces/trace-1/replay", token=_server.token)
    assert payload["trace"]["trace_id"] == "trace-1"
    assert len(payload["spans"]) == 2
    tree = payload["tree"]
    assert len(tree) == 1
    assert tree[0]["span_id"] == "root-span"
    assert tree[0]["children"][0]["span_id"] == "child-span"


def test_filter_by_status(_server):
    _seed_sync(
        _server,
        traces=[_trace("t-ok", status="ok"), _trace("t-error", status="error")],
    )
    payload = _json(_server, "/api/traces?status=error", token=_server.token)
    assert [row["trace_id"] for row in payload] == ["t-error"]


def test_filter_by_project(_server):
    _seed_sync(
        _server,
        traces=[_trace("t-a", project_id="proj-a"), _trace("t-b", project_id="proj-b")],
    )
    payload = _json(_server, "/api/traces?project_id=proj-b", token=_server.token)
    assert [row["trace_id"] for row in payload] == ["t-b"]


def test_filter_by_error_and_latency(_server):
    _seed_sync(
        _server,
        traces=[
            _trace("t-ok", error=None, latency_ms=10, end_time=None),
            _trace("t-bad", error="boom", latency_ms=200, end_time=None),
        ],
    )
    errors = _json(_server, "/api/traces?error=true", token=_server.token)
    assert [row["trace_id"] for row in errors] == ["t-bad"]
    slow = _json(_server, "/api/traces?min_latency_ms=100", token=_server.token)
    assert [row["trace_id"] for row in slow] == ["t-bad"]


def test_invalid_filter_returns_400(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(_server, "/api/traces?limit=not-a-number", token=_server.token)
    assert exc.value.code == 400


def test_get_projects(_server):
    asyncio.run(_seed(_server.db))
    payload = _json(_server, "/api/projects", token=_server.token)
    assert isinstance(payload, list)


def test_unknown_api_route_is_404(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(_server, "/api/nope", token=_server.token)
    assert exc.value.code == 404


def test_post_is_method_not_allowed(_server):
    host, port = _server.server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/api/traces",
        data=b"{}",
        method="POST",
        headers={"Host": f"{host}:{port}", "Authorization": f"Bearer {_server.token}"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 405


def test_concurrent_read_write(_server):
    errors = []

    def writer():
        async def write():
            backend = SQLiteBackend(db_path=_server.db)
            await backend.initialize()
            try:
                store = TraceStore(backend)
                for index in range(20):
                    await store.save_trace(
                        _trace(f"concurrent-{index}", name=f"writer-{index}")
                    )
            finally:
                await backend.close()

        try:
            asyncio.run(write())
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(20):
            payload = _json(_server, "/api/traces?limit=100", token=_server.token)
            assert isinstance(payload, list)
    finally:
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert not errors, errors
    final = _json(_server, "/api/traces?limit=100", token=_server.token)
    assert len(final) == 20


def test_csp_header_on_static_and_api(_server):
    with _get(_server, "/") as response:
        csp = response.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "Access-Control-Allow-Origin" not in response.headers
    with _get(_server, "/api/health", token=_server.token) as response:
        assert "default-src 'self'" in response.headers.get("Content-Security-Policy", "")


def test_filter_aliases_start_time_and_duration(_server):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _seed_sync(
        _server,
        traces=[
            _trace("t-old", start_time=start - timedelta(days=10), latency_ms=10, end_time=None),
            _trace("t-new", start_time=start, latency_ms=500, end_time=None),
        ],
    )
    query = urllib.parse.urlencode(
        {
            "start_time": start.isoformat(),
            "min_duration_ms": "100",
            "limit": "50",
        }
    )
    payload = _json(_server, f"/api/traces?{query}", token=_server.token)
    assert [row["trace_id"] for row in payload] == ["t-new"]


def _post(_server, path, payload, token=None):
    host, port = _server.server.server_address[:2]
    headers = {"Host": f"{host}:{port}", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{port}{path}", data=body, method="POST", headers=headers
    )
    return urllib.request.urlopen(request, timeout=10)


def test_project_create_archive_unarchive(_server):
    with _post(
        _server,
        "/api/projects",
        {"name": "React Project", "project_id": "react-proj", "description": "demo"},
        token=_server.token,
    ) as response:
        assert response.status == 201
        created = json.loads(response.read().decode("utf-8"))
    assert created["project_id"] == "react-proj"

    with _post(
        _server, "/api/projects/react-proj/archive", {}, token=_server.token
    ) as response:
        assert response.status == 200

    projects = _json(_server, "/api/projects", token=_server.token)
    match = [p for p in projects if p["project_id"] == "react-proj"]
    assert len(match) == 1
    assert match[0]["archived"] is True
    assert match[0]["trace_count"] == 0

    with _post(
        _server, "/api/projects/react-proj/unarchive", {}, token=_server.token
    ) as response:
        assert response.status == 200
    projects = _json(_server, "/api/projects", token=_server.token)
    assert [p for p in projects if p["project_id"] == "react-proj"][0]["archived"] is False


def test_project_write_requires_token(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(_server, "/api/projects", {"name": "nope"})
    assert exc.value.code == 403


def test_project_archive_missing_is_404(_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(_server, "/api/projects/does-not-exist/archive", {}, token=_server.token)
    assert exc.value.code == 404


def test_oversized_request_body_is_rejected(_server):
    host, port = _server.server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/api/projects",
        data=b"x" * (10 * 1024 * 1024 + 1),
        method="POST",
        headers={
            "Host": f"{host}:{port}",
            "Authorization": f"Bearer {_server.token}",
            "Content-Type": "application/json",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=15)
    assert exc.value.code == 413
