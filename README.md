# Tracekit

Developer-focused LLM observability and tracing platform. TraceKit persists telemetry directly from the SDK into SQLite and can optionally stream live trace/span events to dashboards over WebSocket. No separate backend server or mandatory collector is required.

## Features (current)
- Trace/span/event models with context propagation
- Tracer with sync/async spans, LLM/tool helpers, replay tree
- W3C `traceparent`/`tracestate` extraction and injection for distributed traces
- Sampling strategies: always-on, always-off, deterministic rate, parent-based
- Span events/structured logging, exception helpers, LLM/tool/retry auto-events
- SDK-native SQLite persistence (`aiosqlite`) with idempotent, transactional batch writes
- Automatic schema migrations and backward-compatible existing database upgrades
- Optional in-process `StreamManager` and WebSocket `StreamServer` for live dashboards
- Idempotent flushing, auto-flush on trace/span end, periodic flush loop
- Trace search filters (status, span type, name, error type, project, environment, time range, latency) and deletion
- API key hashing/validation and project-scoped multi-tenancy helpers
- OTLP/HTTP and optional OTLP/gRPC exporters with bounded reliability helpers
- LangChain callback handler integration
- Automatic input/output token tracking for LLM spans: provider usage is preferred, with bounded text-length estimation as fallback
- Trace-level token aggregation persisted alongside spans for dashboard analytics
- Local dashboard with interactive SVG charts, global filters, light/dark theme, and trace/span inspection
- CLI (`tracekit list/search/show/replay/delete`) with JSON/table output and watch mode

## Installation

Create and activate a virtual environment, then install Tracekit in editable mode:

```bash
python -m venv venv
source venv/bin/activate
pip install -e .
```

To install optional extras for development and LangChain integration:

```bash
pip install -e ".[dev,langchain]"
```

Optional runtime extras:

```bash
pip install -e ".[streaming]"   # WebSocket dashboard streaming (websockets)
pip install -e ".[webhook]"     # Webhook delivery transport (httpx)
pip install -e ".[grpc]"        # OTLP/gRPC exporter
pip install -e ".[postgres]"    # PostgreSQL storage backend
pip install -e ".[clickhouse]"  # ClickHouse storage backend
```

SQLite persistence works with the base install. WebSocket streaming and webhook delivery are disabled unless their extras are installed and the corresponding feature is enabled.

## Version

Check the installed Tracekit version:

```bash
python -c "import tracekit; print(tracekit.__version__)"
```

## Quickstart

### Context Manager (Automatic Cleanup)

Using context managers is recommended for automatic cleanup. When exiting the block, spans and traces are automatically ended, durations and timestamps are recorded, and execution contexts are restored:

```python
from tracekit import Tracer

tracer = Tracer()
with tracer.trace("example"):
    with tracer.span("work", span_type="internal", input={"x": 1}) as s:
        s.add_event("checkpoint", {"step": 1})
        s.output = {"y": 2}
print(tracer.build_trace_replay(next(iter(tracer._traces))))
```

### Direct API vs Context Manager

Tracekit supports both explicit manual management and Python context managers:

- **Context manager**: Use `with tracer.trace(...):` and `with tracer.span(...):`. Cleanup is automatic upon block exit (including recording status/error when exceptions occur).
- **Direct API**: Use `trace = tracer.start_trace(...)` and `span = tracer.start_span(...)`. Explicit cleanup is required: you must explicitly call `tracer.end_span(span)` and `tracer.end_trace(trace)` to finalize the lifecycle, close tokens, and schedule flushes.

```python
from tracekit import Tracer

tracer = Tracer()

# Direct API: explicit lifecycle management
trace = tracer.start_trace("manual-trace")
span = tracer.start_span("manual-span", input={"task": "computation"})
try:
    # perform work...
    span.output = {"result": 42}
finally:
    tracer.end_span(span)
    tracer.end_trace(trace)
```

## SQLite Path Configuration

Tracekit uses SQLite for local trace and span storage:

- **Default location**: `~/.tracellm/traces.db`
- **`TRACELLM_HOME`**: Setting this environment variable changes the base directory (e.g. `export TRACELLM_HOME=/path/to/dir` sets the database path to `/path/to/dir/traces.db`).
- **`TRACEKIT_DB_PATH`**: Setting this environment variable directly overrides the full database file path (e.g. `export TRACEKIT_DB_PATH=/var/log/custom_traces.db`).
- **CLI flag**: Pass `--db /path/to/traces.db` to any `tracekit` CLI command to override the path per invocation:
  ```bash
  tracekit --db /path/to/traces.db list
  ```

## Local Dashboard

Install Tracekit with `pip install tracekit`, persist traces to a local SQLite
file, then open the dashboard in your browser. The compiled UI assets ship in
the wheel, so no npm installation is needed:

```python
from tracekit import trace_langchain
trace_langchain(flush=True, db_path="~/.tracellm/traces.db")
```

```bash
tracekit dashboard --db ~/.tracellm/traces.db --open
```

The dashboard binds to `127.0.0.1` only and is protected by a one-time token
printed to stdout. It is not a hosted backend — your traces never leave your
machine. Optional `--host`, `--port`, and `--token` flags are also available.

Default db path: `~/.tracellm/traces.db` (override with `TRACEKIT_DB_PATH`).

The dashboard shows request volume, error rate, latency percentiles, span composition,
token usage, and cost. Its custom SVG charts include hover/focus tooltips, active
series highlights, and responsive layouts. Global filters, theme switching, and the collapsible
sidebar apply across all dashboard pages; the TraceKit logo and sidebar toggle stay
pinned while the main content scrolls.

### Developing the dashboard UI

The React 18 + TypeScript + Vite source lives in `sdk/frontend/`. Build the
compiled `index.html`, `app.js`, and `styles.css` assets into
`sdk/tracekit/dashboard/static/` with:

```bash
cd sdk/frontend && npm install && npm run build
```

Use `npm run dev` from `sdk/frontend/` to run the local Vite development server.

## SDK-Native Persistence and Live Streaming

`Tracer` persists directly to SQLite by default. No backend server is involved. Use `storage=None` or `persist_enabled=False` when persistence is not wanted:

```python
from tracekit import Tracer

# Default: direct SQLite persistence at TRACEKIT_DB_PATH or ~/.tracellm/traces.db
tracer = Tracer()

# No persistence (legacy in-memory-only tracing)
ephemeral = Tracer(storage=None)

# Custom default SQLite path
local = Tracer(db_path="./traces.db")
```

Persistence and streaming are independent. Storage failures leave pending records buffered for retry; streaming failures never interrupt application execution.

To stream completed trace/span lifecycle events to a dashboard, install the streaming extra and enable it:

```bash
pip install -e ".[streaming]"
```

```python
import asyncio
from tracekit import Tracer

tracer = Tracer(
    stream_enabled=True,
    stream_host="127.0.0.1",
    stream_port=8765,
)

async def main():
    await tracer.start_stream_server()
    with tracer.trace("user_request"):
        with tracer.span("llm_call"):
            call_llm()
    await tracer.flush()
    await tracer.stop_stream_server()

asyncio.run(main())
```

Dashboard clients connect to `ws://127.0.0.1:8765`. Events are JSON objects:

```json
{"type": "span_start", "span": {...}, "trace_id": "...", "span_id": "...", "timestamp": "...", "sampled": true}
```

Supported types are `trace_start`, `trace_end`, `span_start`, and `span_end`. Only sampled telemetry is published, matching persisted data. Streaming binds to localhost by default, uses bounded per-client queues, drops oldest events when a client is too slow, and unsubscribes cleanly on shutdown.

## LangChain Integration

Tracekit includes a `TracekitCallbackHandler` for seamless tracing of LangChain chains, LLM calls, and tools.

> **Note**: Install the optional `langchain` extra first:
> `pip install "tracekit[langchain]"`. For a repository checkout, use
> `pip install -e ".[dev,langchain]"`.

Minimal example:

```python
from langchain_openai import ChatOpenAI
from tracekit import Tracer, TracekitCallbackHandler

tracer = Tracer()
handler = TracekitCallbackHandler(tracer)

# Pass handler in callbacks
llm = ChatOpenAI(model="gpt-4o-mini", callbacks=[handler])
response = llm.invoke("Hello, Tracekit!", config={"callbacks": [handler]})
```

### Automatic LangChain instrumentation

For zero-change tracing of existing LangChain code, instrument once at startup
with the top-level API. This patches `BaseLLM`, `BaseChatModel`, `BaseChain`,
and `BaseTool` so new instances automatically receive exactly one
`TracekitCallbackHandler` without replacing any callbacks you already configured.

```python
import asyncio
import tracekit

tracer = tracekit.trace_langchain(
    flush=True,
    db_path="./traces.db",
    # Any other Tracer construction options also work, for example:
    # stream_enabled=True,
    # sampler=...,
)

# Existing LangChain code continues unchanged.
chain.invoke(...)

# Shutdown: flush pending data and release storage resources.
asyncio.run(tracer.close())
```

`flush=True` is a friendly alias for `Tracer(auto_flush=True, ...)`. Completed
LangChain spans are persisted as lifecycle events end without making a network
call or blocking application work; the explicit `tracer.close()` above is the
recommended deterministic shutdown step. If you only need a mid-process flush,
`await tracer.flush()` remains available. Contradictory values such as
`flush=True, auto_flush=False` raise `ValueError`. The older `auto_flush=True`
spelling continues to work.

If you already own a `Tracer`, pass it as `trace_langchain(tracer=...)` instead.
The supplied tracer owns its configuration, so `flush`, `auto_flush`, and other
tracer construction options are rejected in that form.

Instrumentation is idempotent even when called more than once. Call
`tracekit.untrace_langchain()` to restore the original constructors. The helper
also supports async callbacks, streaming token events, and retry events.

For opt-in wrapping of a single component instead of global patching:

```python
from tracekit import TracekitCallbackHandler

handler = TracekitCallbackHandler()
llm = handler.wrap(existing_llm)  # injects handler into callbacks
```

### Decisive spans and handled failures

A trace's final status follows one decisive span instead of every intermediate
error. Spans carry `is_final` and `handled` flags; both are persisted in SQLite
(migration 6, `is_final INTEGER NOT NULL DEFAULT 0` and
`handled INTEGER NOT NULL DEFAULT 0`).

`Tracer.run_span()` (and the `tracer.span(...)` context manager) accept:

- `allow_failure=True` — record the error on the span, set `handled=True`, and
  return `None` instead of re-raising. Handled spans are ignored when the trace
  status is computed, so a recovered tool outage does not fail the workflow.
- `mark_final=True` — set `is_final=True` on the span. If any span is final, its
  status decides the trace status (the last final span wins). Without a final
  span, the last finished, unhandled span decides it.

```python
tracer.run_span("inventory-service", call_inventory, allow_failure=True)
tracer.run_span("final-summary", summarize, mark_final=True)
```

For auto-instrumented LangChain, `trace_langchain()` enables
`allow_failure=True` for tool runs by default and accepts `mark_final=True` to
mark LLM spans as decisive. Per-invocation overrides can be passed to a tool's
`run()`/`invoke()` wrapper as `allow_failure=` or `mark_final=`; they are
forwarded to the underlying span runner and never reach LangChain.

```python
tracer = tracekit.trace_langchain(db_path="./traces.db", mark_final=True)
# Tool failures keep their error details but do not fail the trace;
# the last LLM span decides the overall status.
```

## Token accounting

LLM spans record `input_tokens`, `output_tokens`, `total_tokens`, and
`token_usage_source`. Provider-reported usage from LangChain callbacks or
`Tracer.record_llm_result()` is used unchanged when present. Missing counts are
estimated from span input/output text with configurable jitter (default ±10%),
and estimates never overwrite provider-reported values.

Trace completion aggregates token counts from its spans once, so dashboard KPIs
and analytics reflect the whole trace without double-counting nested LLM spans.
These fields are persisted on both traces and spans, and existing SQLite
databases are upgraded automatically.

Set `TRACEKIT_TOKEN_ESTIMATION_JITTER` between `0` and `1` to tune the fallback
variation (default `0.10`).

## Distributed tracing
```python
from tracekit import Tracer, extract, inject

# Upstream service
headers = {}
with tracer.trace("upstream"):
    with tracer.span("call"):
        inject(headers)          # sets traceparent + optional tracestate

# Downstream service
remote = extract(request.headers)
with tracer.trace("downstream", parent_context=remote):
    ...                          # child spans keep the upstream trace id
```

## Sampling and auto-flush
```python
from tracekit import RateLimitSampler, Tracer
from backend.apps.db.sqllite import SQLiteBackend

tracer = Tracer(
    storage=SQLiteBackend("traces.db"),
    sampler=RateLimitSampler(0.1),   # sample 10% of root traces
    auto_flush=True,                 # flush ended traces automatically
    flush_batch_size=50,             # also flush every 50 ended spans
    flush_interval=None,             # optional periodic loop (seconds)
)
```

## Storage and export
```python
from backend.apps.db.memory import MemoryBackend
from tracekit import OTLPExporter

memory_tracer = Tracer(storage=MemoryBackend())

exporter = OTLPExporter("http://localhost:4318/v1/traces")
exporter.export_trace(trace_dict, span_dicts)
```

## Configuration and health

Tracekit reads configuration from environment variables and an optional `.env` file. See `.env.example` for supported settings such as `TRACEKIT_ENVIRONMENT`, `TRACEKIT_DB_PATH`, `TRACEKIT_AUTO_FLUSH`, `TRACEKIT_SAMPLING_RATE`, and `TRACEKIT_TOKEN_ESTIMATION_JITTER`.

```python
from tracekit import Tracer
from tracekit.core.config import load_config

config = load_config()
print(config.public_dict())
print(Tracer().health_check())
```

SQLite schema upgrades are applied automatically when a `SQLiteBackend` initializes. Migrations are sequential, preserve existing rows, and track the current version in the `SCHEMA_VERSION` table.

## Projects, API keys, and tenant isolation
```python
from backend.apps.db.sqllite import SQLiteBackend
from tracekit import generate_api_key, list_projects, validate_api_key

backend = SQLiteBackend("traces.db")
projects = await list_projects(backend)

raw_key, record = await generate_api_key(
    backend,
    name="ci",
    project_id=projects[0].project_id,
    environment="production",
)
authenticated = await validate_api_key(backend, raw_key)
```

Only salted PBKDF2 hashes are stored; the raw key is returned once at creation time. `TenantScopedStore` enforces project-level read/write isolation.

## CLI
```bash
tracekit --db path/to/traces.db list
tracekit --output json search --status error --span-type llm
tracekit search --project-id X --environment production --min-latency-ms 100
tracekit show <trace_id> --output table
tracekit delete --trace-id <trace_id> --yes
tracekit delete --older-than 7d --yes
tracekit watch --db path/to/traces.db --interval 2
tracekit projects create demo --project-id my-project
tracekit projects list --include-archived
tracekit api-keys create --project-id my-project --environment production
tracekit api-keys list --project-id my-project
tracekit api-keys validate <raw_key>
tracekit api-keys revoke tk_<key_id>
```

Or via module invocation:
```bash
python -m tracekit --db path/to/traces.db list
```

## Scaling and distributed deployment

Core tracing remains dependency-light. Install only the backend or transport you deploy:

```bash
python -m pip install -e ".[postgres]"
python -m pip install -e ".[clickhouse]"
python -m pip install -e ".[grpc]"
python -m pip install -e ".[distributed]"
```

### Storage backends

`SQLiteBackend` and `MemoryBackend` remain valid `TraceStorage` implementations and keep the original `StorageBackend` collection API. For multi-process deployments, use `PostgreSQLBackend`; for append-heavy analytics, use `ClickHouseBackend`:

```python
from backend.apps.db.clickhouse import ClickHouseBackend
from backend.apps.db.postgres import PostgreSQLBackend
from tracekit import Tracer

postgres = PostgreSQLBackend(
    "postgresql://tracekit:secret@localhost:5432/tracekit",
    min_size=2,
    max_size=20,
)
await postgres.initialize()

clickhouse = ClickHouseBackend(url="http://localhost:8123")
await clickhouse.initialize()

tracer = Tracer(storage=postgres)
```

Drivers are imported lazily. Without optional extras, initialization raises a clear `RuntimeError`. Do not share a connection pool across processes; create one pool per worker process.

### Bounded async persistence

`BoundedAsyncWriter` adds a bounded queue, batching, bounded exponential retries, graceful draining, and drop/retry metrics. It supports `block`, `drop_new`, `drop_oldest`, and `raise` overflow policies:

```python
from tracekit import BoundedAsyncWriter

writer = BoundedAsyncWriter(
    lambda batch: backend.save_spans(batch),
    queue_size=10_000,
    batch_size=200,
    overflow="block",
    enqueue_timeout=0.5,
)
writer.start()
writer.enqueue_sync(span_dict)
writer.flush_sync()
await writer.close()
```

`writer.health_check()` reports queue depth, capacity, retries, failures, and drops. Dropped telemetry is never silent.

### Connection pooling

`AsyncConnectionPool` provides reusable async connections with health checks, timeouts, and idle-connection replacement:

```python
from backend.apps.db.pool import AsyncConnectionPool, PoolConfig

pool = AsyncConnectionPool(create_connection, config=PoolConfig(min_size=1, max_size=10))
await pool.initialize()
async with pool.acquire() as connection:
    ...
await pool.close()
```

### OTLP export

`OTLPExporter` emits OTLP/HTTP JSON with resource, sampling, span-kind, event, exception, and status metadata. `OTLPGrpcExporter` provides injectable and lazy-loaded protobuf/gRPC transport:

```python
from tracekit import OTLPExporter, OTLPGrpcExporter

http = OTLPExporter("http://collector:4318/v1/traces")
grpc = OTLPGrpcExporter("collector:4317")
```

Both exporters expose `metrics.snapshot()` and `health_check()`, cap queue size, retry with bounded backoff, and drain on `close()`.

### HTTP middleware

Framework-agnostic helpers plus pure ASGI and optional Flask middleware propagate W3C trace context without recording request or response bodies or sensitive headers:

```python
from tracekit.integrations import ASGITraceMiddleware, FlaskTraceMiddleware

app = ASGITraceMiddleware(app)
FlaskTraceMiddleware(flask_app)
```

### Deployment trade-offs

| Backend | Best for | Trade-off |
| --- | --- | --- |
| SQLite | local development, embedded use, single writer | not suited to multi-process concurrent writes |
| PostgreSQL | concurrent multi-service deployments | requires asyncpg, pool sizing, and migrations |
| ClickHouse | append-heavy analytics and high-cardinality scans | deletes are mutations and may be asynchronous |

Bounded queues cap memory but can drop telemetry under sustained overload. Prefer `block` with a short `enqueue_timeout` for correctness-sensitive paths, and monitor drop counters in each health check.

### Load and performance tests

Run the deterministic load tests without external services:

```bash
./venv/bin/python -m pytest -q tests/test_phase4_load.py
```

These cover sustained bounded async writes, batch storage inserts, concurrent pool acquisition, and bounded reliability queues.

## Testing

Run tests with pytest:
```bash
python -m pytest -q
```

Or run tests with unittest:
```bash
PYTHONPATH=sdk ./venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Build and validate distributions:
```bash
python -m pip install -e ".[dev]"
python -m pytest -q --cov=tracekit --cov=backend --cov-report=term-missing
python -m build
python -m twine check dist/*
```
