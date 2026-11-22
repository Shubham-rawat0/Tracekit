"""HTTP API handlers for the tracekit dashboard."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from tracekit._version import __version__
from tracekit.core.projects import (
    archive_project,
    count_project_traces,
    create_project,
    list_projects,
    unarchive_project,
)
from tracekit.dashboard.analytics import (
    build_context,
    build_replay_spec,
    export_row,
    extract_fields,
    slowest,
    timeseries,
)
from tracekit.dashboard.analytics import (
    errors as error_groups,
)
from tracekit.dashboard.analytics import (
    models as model_breakdown,
)
from tracekit.dashboard.analytics import (
    summary as summary_metrics,
)
from tracekit.replay import (
    ReplayRequest,
    execute_replay,
    list_replay_providers,
)


def _metadata_of(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata")
    return value if isinstance(value, dict) else {}


def _coerce_int(value: Any) -> int | None:
    """Return an integer for numeric values, or None when conversion fails."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_total_tokens(*sources: Any) -> int | None:
    """Find a total token count across nested API payload sources."""

    token_keys = {
        "total_tokens",
        "totaltokens",
        "total_token_count",
        "token_count",
        "tokencount",
    }
    input_keys = {
        "input_tokens",
        "prompt_tokens",
        "inputtokens",
        "prompttokens",
        "input_token_count",
        "prompt_token_count",
    }
    output_keys = {
        "output_tokens",
        "completion_tokens",
        "outputtokens",
        "completiontokens",
        "output_token_count",
        "completion_token_count",
    }
    total = None
    input_tokens = None
    output_tokens = None
    # Walk nested usage payloads depth-first; seen ids guard against cycles.
    stack = list(sources)
    seen: set[int] = set()
    while stack:
        source = stack.pop()
        if isinstance(source, dict):
            source_id = id(source)
            if source_id in seen:
                continue
            seen.add(source_id)
            for key, value in source.items():
                normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
                if normalized in token_keys and total is None:
                    total = _coerce_int(value)
                elif normalized in input_keys and input_tokens is None:
                    input_tokens = _coerce_int(value)
                elif normalized in output_keys and output_tokens is None:
                    output_tokens = _coerce_int(value)
                elif isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(source, (list, tuple)):
            source_id = id(source)
            if source_id in seen:
                continue
            seen.add(source_id)
            stack.extend(source)
    # An explicit total wins; otherwise derive it from input + output counts.
    if total is not None:
        return total
    if input_tokens is not None and output_tokens is not None:
        return input_tokens + output_tokens
    return None


def _hydrate_trace(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy metadata fallbacks onto a trace response payload."""

    # Copy common metadata fields to top level so downstream code reads one location.
    metadata = _metadata_of(payload)
    fallbacks = (
        ("prompt", ("prompt",)),
        ("response", ("response",)),
        ("model_name", ("model", "model_name")),
        ("error_type", ("error_type",)),
    )
    for target, keys in fallbacks:
        if payload.get(target) is not None:
            continue
        for key in keys:
            if metadata.get(key) is not None:
                payload[target] = metadata[key]
                break
    for field in ("input_tokens", "output_tokens", "total_tokens", "token_usage_source"):
        if payload.get(field) is None and metadata.get(field) is not None:
            payload[field] = metadata[field]
    if payload.get("token_count") is None:
        token_count = _extract_total_tokens(
            payload,
            payload.get("attributes"),
            metadata.get("total_tokens"),
            metadata.get("token_count"),
            metadata.get("token_usage"),
            metadata.get("usage"),
            metadata.get("usage_metadata"),
            metadata.get("llm.usage"),
        )
        if token_count is not None:
            payload["token_count"] = token_count
    return payload


def _normalize_trace(payload):
    """Normalize trace and span payload fields recursively."""

    # Fold latency aliases into duration_ms, then recurse into traces and spans.
    if isinstance(payload, dict):
        if payload.get("duration_ms") is None and (
            "latency_ms" in payload or "latency" in payload
        ):
            if payload.get("latency_ms") is not None:
                payload["duration_ms"] = payload.get("latency_ms")
            else:
                payload["duration_ms"] = payload.get("latency")
        # Presence of trace_id without span_id distinguishes a trace from a span.
        if "trace_id" in payload and "span_id" not in payload:
            _hydrate_trace(payload)
        if "trace" in payload and isinstance(payload["trace"], dict):
            _normalize_trace(payload["trace"])
        if "spans" in payload and isinstance(payload["spans"], list):
            _normalize_trace(payload["spans"])
        if "items" in payload and isinstance(payload["items"], list):
            _normalize_trace(payload["items"])
    elif isinstance(payload, list):
        for item in payload:
            _normalize_trace(item)
    return payload


def _to_text(value: Any) -> str | None:
    """Serialize export values into single text cells."""

    # Serialize structured outputs so exported rows stay single-valued.
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


async def _spans_for_traces(store, traces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        trace_id = trace.get("trace_id")
        if trace_id:
            result[str(trace_id)] = await store.get_spans_for_trace(str(trace_id))
    return result


async def _record_replay(store, body: dict[str, Any]) -> dict[str, Any] | None:
    trace_id = body.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError("trace_id is required")
    replay = await store.get_trace_replay(trace_id)
    if replay is None:
        return None
    original_trace = replay["trace"]
    spans = replay["spans"]
    spec = build_replay_spec(original_trace, spans)
    # Reject traces without a replayable LLM span before executing anything.
    if not spec["eligible"]:
        raise ValueError(spec["reason"])

    selected = spec["selected"]
    requested_span_id = body.get("span_id")
    if requested_span_id:
        selected = next(
            (item for item in spec["llm_spans"] if item["span_id"] == requested_span_id),
            None,
        )
        if selected is None:
            raise ValueError("span_id does not identify an eligible LLM span")
    assert selected is not None

    prompt = body.get("prompt") if "prompt" in body else selected.get("input")
    if prompt is None:
        raise ValueError("prompt is required")
    if not isinstance(prompt, str):
        prompt = json.dumps(prompt, default=str)
    if len(prompt) > 1024 * 1024:
        raise ValueError("prompt is too large")

    # Body values override the recorded span's input/parameters/model when supplied.
    parameters = body.get("parameters") if "parameters" in body else selected.get("parameters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")

    request = ReplayRequest(
        trace_id=trace_id,
        span_id=str(selected["span_id"]) if selected.get("span_id") else None,
        prompt=prompt,
        parameters=parameters,
        model=body.get("model") or selected.get("model"),
        provider=body.get("provider") or selected.get("provider"),
        metadata={
            "replay_of_trace_id": trace_id,
            "replay_of_span_id": selected.get("span_id"),
        },
    )
    result = await execute_replay(request)

    started = datetime.now(timezone.utc)
    latency = result.latency_ms
    if latency is None:
        ended = started
    else:
        ended = started + timedelta(milliseconds=max(0.0, float(latency)))
    new_trace_id = uuid.uuid4().hex
    token_usage = dict(result.token_usage or {})
    total_tokens = token_usage.get("total_tokens")
    if total_tokens is None and token_usage.get("prompt_tokens") is not None and token_usage.get("completion_tokens") is not None:
        total_tokens = int(token_usage["prompt_tokens"]) + int(token_usage["completion_tokens"])
    original_fields = extract_fields(original_trace, spans)
    provider = result.provider or request.provider
    model = result.model or request.model

    # Persist the replay as its own trace/span so it appears alongside the original in the UI.
    new_trace: dict[str, Any] = {
        "trace_id": new_trace_id,
        "name": f"replay:{original_trace.get('name') or trace_id}",
        "start_time": started.isoformat(timespec="microseconds"),
        "end_time": ended.isoformat(timespec="microseconds"),
        "duration_ms": float(latency) if latency is not None else 0.0,
        "status": "error" if result.error else result.status or "ok",
        "error": result.error,
        "error_type": type(result.error).__name__ if result.error else None,
        # Inherit project/environment so the replay stays in the original tenant scope.
        "project_id": original_trace.get("project_id"),
        "project_name": original_trace.get("project_name"),
        "environment": original_trace.get("environment"),
        "model_name": model,
        "token_count": total_tokens,
        "prompt": prompt,
        "response": _to_text(result.output),
        "metadata": {
            "replay_of_trace_id": trace_id,
            "replay_of_span_id": request.span_id,
            "replay_provider": provider,
            "replay_model": model,
            "replay_original_model": original_fields["model"],
            "replay_modified": prompt != selected.get("input")
            or parameters != selected.get("parameters")
            or model != selected.get("model"),
            "replay_parameters": parameters,
        },
        "attributes": {"replay": True},
    }
    new_span: dict[str, Any] = {
        "span_id": uuid.uuid4().hex[:16],
        "trace_id": new_trace_id,
        "parent_span_id": None,
        "name": f"replay:{selected.get('name') or 'llm'}",
        "span_type": "llm",
        "start_time": new_trace["start_time"],
        "end_time": new_trace["end_time"],
        "duration_ms": new_trace["duration_ms"],
        "status": new_trace["status"],
        "input": prompt,
        "output": _to_text(result.output),
        "error": result.error,
        "attributes": {
            "model": model,
            "provider": provider,
            "token_usage": token_usage,
            "replay_of_trace_id": trace_id,
            "replay_of_span_id": request.span_id,
            "replay_parameters": parameters,
        },
        "events": [],
    }
    for key, value in result.metadata.items():
        new_trace["metadata"][f"replay_result_{key}"] = value
    await store.save_trace(new_trace)
    await store.save_spans([new_span])

    return {
        "trace_id": new_trace_id,
        "span_id": new_span["span_id"],
        "replay_of_trace_id": trace_id,
        "replay_of_span_id": request.span_id,
        "status": new_trace["status"],
        "error": result.error,
        "warning": spec["warning"],
        # Return side-by-side original/replay data so the UI can diff the two runs.
        "comparison": {
            "original": {
                "trace_id": trace_id,
                "span_id": request.span_id,
                "input": selected.get("input"),
                "output": selected.get("output"),
                "model": selected.get("model"),
                "provider": selected.get("provider"),
                "latency_ms": selected.get("latency_ms"),
                "parameters": selected.get("parameters"),
                "token_usage": selected.get("token_usage"),
                "status": selected.get("status"),
            },
            "replay": {
                "trace_id": new_trace_id,
                "span_id": new_span["span_id"],
                "input": prompt,
                "output": result.output,
                "model": model,
                "provider": provider,
                "latency_ms": latency,
                "parameters": parameters,
                "token_usage": token_usage,
                "status": new_trace["status"],
            },
        },
    }


def qs_to_filters(qs):
    # Map UI parameter names onto store filter keys (e.g. start/end to time fields).
    filters = {}
    mapping = {
        "status": "status",
        "name": "name",
        "name_contains": "name_contains",
        "q": "q",
        "trace_id": "trace_id",
        "project_id": "project_id",
        "environment": "environment",
        "start": "start_time",
        "end": "end_time",
        "start_time": "start_time",
        "end_time": "end_time",
        "error_type": "error_type",
        "span_type": "span_type",
        "model_name": "model_name",
        "sort_by": "sort_by",
    }
    for k, fk in mapping.items():
        if k in qs and qs[k]:
            # Only the first value of a repeated query parameter is used.
            v = qs[k][0]
            if v != "":
                filters[fk] = v
    if "error" in qs and qs["error"]:
        v = qs["error"][0]
        if v != "":
            low = v.lower()
            if low in ("true", "1", "yes"):
                filters["error"] = True
            elif low in ("false", "0", "no"):
                filters["error"] = False
            else:
                raise ValueError(f"invalid boolean for error: {v}")
    if "sort_desc" in qs and qs["sort_desc"]:
        v = qs["sort_desc"][0]
        if v != "":
            low = v.lower()
            if low in ("true", "1", "yes"):
                filters["sort_desc"] = True
            elif low in ("false", "0", "no"):
                filters["sort_desc"] = False
            else:
                raise ValueError(f"invalid boolean for sort_desc: {v}")
    for key in ("min_latency_ms", "max_latency_ms"):
        if key in qs and qs[key]:
            v = qs[key][0]
            if v != "":
                filters[key] = float(v)
    # Legacy duration filter names alias the store's latency filters.
    if "min_duration_ms" in qs and qs["min_duration_ms"]:
        v = qs["min_duration_ms"][0]
        if v != "":
            filters["min_latency_ms"] = float(v)
    if "max_duration_ms" in qs and qs["max_duration_ms"]:
        v = qs["max_duration_ms"][0]
        if v != "":
            filters["max_latency_ms"] = float(v)
    if "limit" in qs and qs["limit"]:
        v = qs["limit"][0]
        if v != "":
            filters["limit"] = int(v)
    else:
        # Default page size keeps trace listings from loading unbounded rows.
        filters.setdefault("limit", 50)
    if "offset" in qs and qs["offset"]:
        v = qs["offset"][0]
        if v != "":
            filters["offset"] = int(v)
    else:
        filters.setdefault("offset", 0)
    return filters


def make_handler(loop, backend, store, token, static_dir, bind_host):
    static_dir = Path(static_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            self.loop = loop
            self.backend = backend
            self.store = store
            self.token = token
            self.static_dir = static_dir
            self.bind_host = bind_host
            super().__init__(*args, **kwargs)

        def _allowed_hosts(self):
            # Host-header check mitigates DNS rebinding when the server binds beyond loopback.
            address = cast(tuple[str, int], self.server.server_address)
            port = address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.bind_host not in ("127.0.0.1",):
                hosts.add(f"{self.bind_host}:{port}")
            return hosts

        def _send_json(self, status, payload):
            # Same restrictive CSP as static assets keeps one policy across all responses.
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status, content, media_type, filename=None):
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'",
            )
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def _read_json(self, limit=1024 * 1024):
            length = int(self.headers.get("Content-Length", "0"))
            if length > limit:
                # Drain the body before replying so the connection is left in sync.
                self.rfile.read(length)
                self._send_json(413, {"error": "payload too large"})
                return None
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send_json(400, {"error": "invalid json"})
                return None
            if not isinstance(parsed, dict):
                self._send_json(400, {"error": "json body must be an object"})
                return None
            return parsed

        def _check_host(self):
            host = self.headers.get("Host")
            if host not in self._allowed_hosts():
                self._send_json(403, {"error": "forbidden"})
                return False
            return True

        def _check_token(self):
            # Accept the token via bearer header or query string, using constant-time compare.
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                if secrets.compare_digest(auth[len("Bearer "):], self.token):
                    return True
            qs = parse_qs(urlparse(self.path).query)
            if "token" in qs and qs["token"]:
                if secrets.compare_digest(qs["token"][0], self.token):
                    return True
            self._send_json(403, {"error": "forbidden"})
            return False

        def do_GET(self):
            if not self._check_host():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path.startswith("/api/"):
                # Only API routes require the token; static assets stay publicly readable.
                if not self._check_token():
                    return
                try:
                    self._route_api(path, qs)
                # Bad input is a 400; unexpected failures become 500 without killing the UI.
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                except Exception as e:
                    self._send_json(500, {"error": str(e)})
                return
            self._serve_static(path)

        def do_POST(self):
            if not self._check_host():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                self._send_json(405, {"error": "method not allowed"})
                return
            if not self._check_token():
                return
            if path == "/api/replay":
                body = self._read_json()
                if body is None:
                    return
                try:
                    result = self._run(_record_replay(self.store, body))
                except LookupError as e:
                    self._send_json(422, {"error": str(e)})
                    return
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                if result is None:
                    self._send_json(404, {"error": "not found"})
                    return
                _normalize_trace(result)
                self._send_json(201, result)
                return
            if path == "/api/projects":
                length = int(self.headers.get("Content-Length", "0"))
                if length > 10 * 1024 * 1024:
                    self.rfile.read(length)
                    self._send_json(413, {"error": "payload too large"})
                    return
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    self._send_json(400, {"error": "invalid json"})
                    return
                try:
                    proj = self._run(create_project(self.backend, body.get("name", ""), project_id=body.get("project_id"), description=body.get("description", "")))
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                out = proj.__dict__ if hasattr(proj, "__dict__") else proj
                try:
                    out["trace_count"] = self._run(count_project_traces(self.backend, out.get("project_id")))
                except Exception:
                    pass
                self._send_json(201, out)
                return
            if path.startswith("/api/projects/") and path.endswith("/archive"):
                pid = path[len("/api/projects/"): -len("/archive")].strip("/")
                ok = self._run(archive_project(self.backend, pid))
                if not ok:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, {"project_id": pid, "archived": True})
                return
            if path.startswith("/api/projects/") and path.endswith("/unarchive"):
                pid = path[len("/api/projects/"): -len("/unarchive")].strip("/")
                ok = self._run(unarchive_project(self.backend, pid))
                if not ok:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, {"project_id": pid, "archived": False})
                return
            self._send_json(405, {"error": "method not allowed"})
        def do_PUT(self):
            self._method_not_allowed()
        def do_DELETE(self):
            self._method_not_allowed()
        def do_PATCH(self):
            self._method_not_allowed()

        def _method_not_allowed(self):
            if not self._check_host():
                return
            self._send_json(405, {"error": "method not allowed"})

        def _run(self, coro):
            # Sync HTTP handlers bridge to the async store on the server's dedicated loop.
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            return future.result(timeout=10)

        def _route_api(self, path, qs):
            if path == "/api/health":
                res = self._run(self.backend.health_check())
                self._send_json(200, res if res is not None else {})
                return
            if path == "/api/config":
                db_path = getattr(self.backend, "db_path", None)
                if db_path is None:
                    db_path = getattr(self.backend, "_db_path", "")
                self._send_json(200, {"db_path": str(db_path), "version": __version__})
                return
            if path == "/api/replay/providers":
                self._send_json(200, {"providers": list_replay_providers()})
                return
            if path == "/api/traces":
                filters = qs_to_filters(qs)
                res = self._run(self.store.search_traces(**filters))
                _normalize_trace(res)
                self._send_json(200, res)
                return
            if path == "/api/traces/query":
                filters = qs_to_filters(qs)
                res = self._run(self.store.search_traces_page(**filters))
                _normalize_trace(res)
                items = res.get("items") if isinstance(res, dict) else None
                # Enrich only the current page, preserving the store's pagination metadata.
                if isinstance(items, list):
                    trace_items = [item for item in items if isinstance(item, dict)]
                    spans_by_trace = self._run(_spans_for_traces(self.store, trace_items))
                    for item in trace_items:
                        spans = spans_by_trace.get(str(item.get("trace_id")), [])
                        fields = extract_fields(item, spans)
                        tokens = fields["tokens"]
                        item["span_count"] = len(spans)
                        item["llm_calls"] = fields["llm_count"]
                        item["input_tokens"] = tokens["input_tokens"] if tokens["input_tokens_available"] else None
                        item["output_tokens"] = tokens["output_tokens"] if tokens["output_tokens_available"] else None
                        item["total_tokens"] = tokens["total_tokens"] if tokens["total_tokens_available"] else None
                        if item.get("prompt") is None and fields.get("input") is not None:
                            item["prompt"] = _to_text(fields["input"])
                        if item.get("response") is None and fields.get("output") is not None:
                            item["response"] = _to_text(fields["output"])
                        if item.get("model_name") is None and fields.get("model") is not None:
                            item["model_name"] = str(fields["model"])
                self._send_json(200, res)
                return
            if path == "/api/analytics":
                filters = qs_to_filters(qs)
                # Aggregates need every matching trace, so bypass page-size limits.
                filters["limit"] = -1
                filters["offset"] = 0
                traces = self._run(self.store.search_traces(**filters))
                spans_by_trace = self._run(_spans_for_traces(self.store, traces))
                context = build_context(traces, spans_by_trace)
                bucket = qs.get("bucket", ["hour"])[0] or "hour"
                self._send_json(
                    200,
                    {
                        "summary": summary_metrics(context),
                        "timeseries": timeseries(context, bucket=bucket),
                        "models": model_breakdown(context),
                        "errors": error_groups(context),
                        "slowest": slowest(context),
                    },
                )
                return
            if path == "/api/export":
                filters = qs_to_filters(qs)
                # Cap export size so a single request cannot stream an unbounded table.
                filters["limit"] = min(max(filters.get("limit", 10000), 0), 10000)
                traces = self._run(self.store.search_traces(**filters))
                spans_by_trace = self._run(_spans_for_traces(self.store, traces))
                rows = [
                    export_row(trace, spans_by_trace.get(str(trace.get("trace_id")), []))
                    for trace in traces
                ]
                export_format = (qs.get("format", ["jsonl"])[0] or "jsonl").lower()
                if export_format == "csv":
                    # Use a canonical header when empty so the CSV schema stays stable.
                    fieldnames = list(rows[0]) if rows else list(export_row({}, []))
                    buffer = io.StringIO()
                    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({key: _to_text(row.get(key)) for key in fieldnames})
                    self._send_bytes(
                        200,
                        buffer.getvalue().encode("utf-8"),
                        "text/csv; charset=utf-8",
                        "tracekit-traces.csv",
                    )
                    return
                if export_format != "jsonl":
                    raise ValueError("format must be jsonl or csv")
                body = b"".join(
                    (json.dumps(row, default=str) + "\n").encode("utf-8") for row in rows
                )
                self._send_bytes(
                    200,
                    body,
                    "application/x-ndjson; charset=utf-8",
                    "tracekit-traces.jsonl",
                )
                return
            if path.startswith("/api/traces/"):
                rest = path[len("/api/traces/"):]
                if rest.endswith("/replay"):
                    tid = rest[:-len("/replay")].strip("/")
                    res = self._run(self.store.get_trace_replay(tid))
                    if res is None:
                        self._send_json(404, {"error": "not found"})
                        return
                    _normalize_trace(res)
                    res["replay_spec"] = build_replay_spec(res.get("trace", {}), res.get("spans", []))
                    self._send_json(200, res)
                    return
                tid = rest.strip("/")
                res = self._run(self.store.get_trace(tid))
                if res is None:
                    self._send_json(404, {"error": "not found"})
                    return
                _normalize_trace(res)
                self._send_json(200, res)
                return
            if path == "/api/projects":
                # Include archived projects so the UI can offer an unarchive action.
                res = self._run(list_projects(self.backend, include_archived=True))
                out: list[Any] = []
                for p in res:
                    project_item: dict[str, Any]
                    if hasattr(p, "__dict__"):
                        project_item = dict(p.__dict__)
                    elif isinstance(p, dict):
                        project_item = dict(p)
                    else:
                        project_item = {"name": str(p)}
                    project_item["trace_count"] = self._run(
                        count_project_traces(self.backend, str(project_item.get("project_id", "")))
                    )
                    out.append(project_item)
                self._send_json(200, out)
                return
            self._send_json(404, {"error": "not found"})

        def _serve_static(self, path):
            if path == "/":
                target = self.static_dir / "index.html"
            else:
                target = self.static_dir / Path(path.lstrip("/"))
            try:
                resolved = target.resolve()
            except Exception:
                self._send_json(404, {"error": "not found"})
                return
            base = self.static_dir.resolve()
            # Reject resolved paths outside static_dir to prevent traversal attacks.
            if resolved != base and base not in resolved.parents:
                self._send_json(403, {"error": "forbidden"})
                return
            if not resolved.exists() or not resolved.is_file():
                self._send_json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            data = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args, **kwargs):
            # Silence per-request logging to keep the dashboard console quiet.
            return

    return Handler
