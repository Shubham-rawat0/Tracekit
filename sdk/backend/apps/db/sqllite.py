"""SQLite storage backend for Tracekit.

Implements the TraceStorage interface using SQLite as the embedded database.
Provides a local, file-based persistence option for traces and spans.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from backend.apps.db.migrations import CURRENT_SCHEMA_VERSION, ensure_schema
from tracekit.core.storage import StorageBackend, TraceStorage

# compatibility fallback
# Older layouts exposed this ABC at app.database.base; prefer it when present.
try:
    from app.database.base import StorageBackend as _AppStorageBackend  # type: ignore
except Exception:
    _AppStorageBackend = StorageBackend


# Naive values are assumed UTC because SQLite stores timestamps as text compared lexically.
def _normalize_datetime(value: datetime) -> datetime:
    """Return a UTC-aware datetime for consistent SQLite storage."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse stored timestamp text into a UTC-aware datetime."""

    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # "Z" is rejected by datetime.fromisoformat on older supported Pythons.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _normalize_datetime(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


# Fixed microsecond precision keeps serialized UTC timestamps lexicographically comparable.
def _format_timestamp(value: datetime) -> str:
    """Serialize a datetime to the fixed-precision UTC text format."""

    return _normalize_datetime(value).isoformat(timespec="microseconds")


class _DateTimeEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        """Encode datetime values while delegating other types to JSON."""

        if isinstance(o, datetime):
            return _format_timestamp(o)
        return super().default(o)


# TRACELLM_HOME keeps the legacy default location; TRACEKIT_DB_PATH overrides it.
def default_db_path() -> Path:
    """Resolve the default SQLite database path from environment overrides."""

    override = os.getenv("TRACEKIT_DB_PATH")
    if override:
        return Path(override).expanduser()
    home = Path(os.getenv("TRACELLM_HOME", "~/.tracellm")).expanduser()
    return home / "traces.db"


# Table and column names are interpolated into SQL (only values are bound), so both need allow-lists.
ALLOWED_COLLECTIONS = frozenset({"traces", "spans", "projects", "api_keys"})

ALLOWED_FILTER_FIELDS: dict[str, frozenset[str]] = {
    "traces": frozenset(
        {
            "trace_id",
            "name",
            "start_time",
            "end_time",
            "duration_ms",
            "status",
            "attributes",
            "metadata",
            "error",
            "error_type",
            "prompt",
            "response",
            "latency",
            "latency_ms",
            "token_count",
            "model_name",
            "project_id",
            "project_name",
            "api_key",
            "key_hash",
            "environment",
            "retry_count",
            "slow_request",
            "failure_reason",
            "created_at",
            "updated_at",
            "steps",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "token_usage_source",
        }
    ),
    "spans": frozenset(
        {
            "span_id",
            "trace_id",
            "parent_span_id",
            "name",
            "span_type",
            "start_time",
            "end_time",
            "duration_ms",
            "status",
            "input",
            "output",
            "error",
            "error_type",
            "attributes",
            "events",
            "project_id",
            "environment",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "token_usage_source",
            "is_final",
            "handled",
        }
    ),
    "projects": frozenset(
        {
            "project_id",
            "name",
            "description",
            "created_at",
            "updated_at",
            "archived",
        }
    ),
    "api_keys": frozenset(
        {
            "key",
            "key_hash",
            "name",
            "salt",
            "project_id",
            "environment",
            "metadata",
            "expires_at",
            "revoked",
            "created_at",
            "updated_at",
        }
    ),
}

# Only the four range operators map safely to bound SQL parameters.
_ALLOWED_OPERATORS = frozenset({"$gte", "$lte", "$gt", "$lt"})


def _validate_collection(collection: str) -> str:
    """Reject collection names outside the SQL allow-list."""

    if collection not in ALLOWED_COLLECTIONS:
        raise ValueError(f"unsupported collection: {collection}")
    return collection


def _validate_field(collection: str, field: str) -> str:
    """Reject filter or sort fields outside the collection allow-list."""

    _validate_collection(collection)
    if field not in ALLOWED_FILTER_FIELDS[collection]:
        raise ValueError(f"unsupported field {field!r} for collection {collection!r}")
    return field


CREATE_TRACES_TABLE = """
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT UNIQUE NOT NULL,
    name TEXT,
    start_time TEXT,
    end_time TEXT,
    duration_ms REAL,
    status TEXT,
    attributes TEXT,
    metadata TEXT,
    error TEXT,
    prompt TEXT,
    response TEXT,
    latency REAL,
    token_count INTEGER,
    model_name TEXT,
    project_id TEXT,
    project_name TEXT,
    api_key TEXT,
    environment TEXT,
    retry_count INTEGER,
    slow_request INTEGER,
    failure_reason TEXT,
    created_at TEXT,
    updated_at TEXT,
    steps TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    token_usage_source TEXT
)
"""

CREATE_SPANS_TABLE = """
CREATE TABLE IF NOT EXISTS spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id TEXT UNIQUE NOT NULL,
    trace_id TEXT,
    parent_span_id TEXT,
    name TEXT,
    span_type TEXT,
    start_time TEXT,
    end_time TEXT,
    duration_ms REAL,
    status TEXT,
    input TEXT,
    output TEXT,
    error TEXT,
    attributes TEXT,
    events TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    token_usage_source TEXT,
    is_final INTEGER NOT NULL DEFAULT 0,
    handled INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_PROJECTS_TABLE = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT UNIQUE NOT NULL,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at TEXT
)
"""

CREATE_API_KEYS_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    project_id TEXT,
    environment TEXT,
    created_at TEXT
)
"""

TRACE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_traces_trace_id ON traces(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_traces_created_at ON traces(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status)",
    "CREATE INDEX IF NOT EXISTS idx_traces_model_name ON traces(model_name)",
    "CREATE INDEX IF NOT EXISTS idx_traces_project_id ON traces(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_traces_environment ON traces(environment)",
]

SPAN_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_spans_span_id ON spans(span_id)",
    "CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_spans_parent_span_id ON spans(parent_span_id)",
    "CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time)",
    "CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status)",
]

PROJECT_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_project_id ON projects(project_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects(name)",
]

API_KEY_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_project_id ON api_keys(project_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_environment ON api_keys(environment)",
]

_COLUMN_TIMESTAMP_FIELDS: dict[str, tuple[str, ...]] = {
    "traces": ("start_time", "end_time", "created_at", "updated_at"),
    "spans": ("start_time", "end_time"),
    "projects": ("created_at", "updated_at"),
    "api_keys": ("created_at", "updated_at", "expires_at"),
}

_ALL_TIMESTAMP_FIELDS = frozenset(
    field for fields in _COLUMN_TIMESTAMP_FIELDS.values() for field in fields
)

_DURATION_COLLECTIONS = frozenset({"traces", "spans"})


# Filter values must use the same UTC text format as stored timestamps to compare correctly.
def _filter_value(key: str, value: Any) -> Any:
    """Normalize filter values to the stored representation before querying."""

    if key in _ALL_TIMESTAMP_FIELDS:
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return _format_timestamp(parsed)
    if isinstance(value, datetime):
        return _format_timestamp(value)
    return value


def _build_where(filter: dict | None, collection: str | None = None) -> tuple[str, list]:
    """Build a parameterized WHERE clause from a supported filter mapping."""

    if not filter:
        return "", []

    conditions: list[str] = []
    params: list[Any] = []

    for key, value in filter.items():
        # Nested $or is unsupported; subfilters are validated as plain fields.
        if key == "$or":
            or_parts: list[str] = []
            for sub in value:
                sub_clause, sub_params = _build_where_conditions(sub, collection)
                if sub_clause:
                    or_parts.append(f"({sub_clause})")
                    params.extend(sub_params)
            if or_parts:
                conditions.append(f"({' OR '.join(or_parts)})")
            continue
        if collection is not None:
            _validate_field(collection, key)
        if isinstance(value, dict):
            for op, op_val in value.items():
                if op not in _ALLOWED_OPERATORS:
                    raise ValueError(f"unsupported filter operator: {op}")
                sql_operator = {"$gte": ">=", "$lte": "<=", "$gt": ">", "$lt": "<"}[op]
                conditions.append(f"{key} {sql_operator} ?")
                params.append(_filter_value(key, op_val))
        else:
            conditions.append(f"{key} = ?")
            params.append(_filter_value(key, value))

    return " WHERE " + " AND ".join(conditions) if conditions else "", params


def _build_where_conditions(
    filter: dict, collection: str | None = None
) -> tuple[str, list]:
    """Build an AND-joined condition list for nested OR subfilters."""

    conditions: list[str] = []
    params: list[Any] = []

    for key, value in filter.items():
        if collection is not None:
            _validate_field(collection, key)
        if isinstance(value, dict):
            for op, op_val in value.items():
                if op not in _ALLOWED_OPERATORS:
                    raise ValueError(f"unsupported filter operator: {op}")
                sql_operator = {"$gte": ">=", "$lte": "<=", "$gt": ">", "$lt": "<"}[op]
                conditions.append(f"{key} {sql_operator} ?")
                params.append(_filter_value(key, op_val))
        else:
            conditions.append(f"{key} = ?")
            params.append(_filter_value(key, value))

    return " AND ".join(conditions), params


def _build_order_by(sort: list | None, collection: str | None = None) -> str:
    """Build a validated ORDER BY clause from requested sort fields."""

    if not sort:
        return ""
    clauses: list[str] = []
    for item in sort:
        if isinstance(item, str):
            field = item
            direction = "ASC"
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            field, raw_direction = item
            direction = "DESC" if raw_direction == -1 else "ASC"
        else:
            continue
        if collection is not None:
            _validate_field(collection, field)
        clauses.append(f"{field} {direction}")
    return " ORDER BY " + ", ".join(clauses) if clauses else ""


_JSON_FIELDS = {"steps", "events", "attributes", "metadata", "input", "output"}


# Decode JSON text columns back to objects and hide the internal autoincrement row id.
def _serialize_document(doc: dict) -> dict:
    """Decode JSON and timestamp columns into an API-friendly document."""

    result = dict(doc)
    for key in _JSON_FIELDS:
        if key in result and isinstance(result[key], str):
            try:
                result[key] = json.loads(result[key])
            except Exception:
                pass
    if "slow_request" in result and result["slow_request"] is not None:
        result["slow_request"] = bool(result["slow_request"])
    for key in _ALL_TIMESTAMP_FIELDS:
        if isinstance(result.get(key), str):
            parsed = _parse_timestamp(result[key])
            if parsed is not None:
                result[key] = parsed
    result.pop("id", None)
    return result


# SQLite has no bool type, so slow_request is stored as 0/1 and restored on read.
def _deserialize_document(document: dict) -> dict:
    """Encode complex fields and datetimes into SQLite column values."""

    result = dict(document)
    for key in _JSON_FIELDS:
        if key in result and isinstance(result[key], (list, dict)):
            result[key] = json.dumps(result[key], cls=_DateTimeEncoder)
    if "slow_request" in result and result["slow_request"] is not None:
        result["slow_request"] = 1 if result["slow_request"] else 0
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = _format_timestamp(value)
    return result


# Explicit created_at is preserved, updated_at always refreshed, and duration derived from timestamps.
def _prepare_document(collection: str, document: dict) -> dict:
    """Apply timestamps, duration derivation, and collection defaults before insert."""

    result = _deserialize_document(document)
    fields = _COLUMN_TIMESTAMP_FIELDS.get(collection, ())
    now = datetime.now(timezone.utc)

    if "created_at" in fields:
        result["created_at"] = _format_timestamp(
            _parse_timestamp(result.get("created_at")) or now
        )
    if "updated_at" in fields:
        result["updated_at"] = _format_timestamp(now)

    parsed: dict[str, datetime | None] = {}
    for field in ("start_time", "end_time"):
        if field not in fields:
            continue
        parsed_value = _parse_timestamp(result.get(field))
        parsed[field] = parsed_value
        result[field] = (
            _format_timestamp(parsed_value) if parsed_value is not None else None
        )

    if "start_time" in fields and parsed.get("start_time") is None:
        result["start_time"] = _format_timestamp(now)
        parsed["start_time"] = now

    # A derived duration overwrites caller-supplied values when both endpoints are present.
    if collection in _DURATION_COLLECTIONS:
        start = parsed.get("start_time")
        end = parsed.get("end_time")
        if start is not None and end is not None:
            result["duration_ms"] = round((end - start).total_seconds() * 1000, 3)
        elif not isinstance(result.get("duration_ms"), (int, float)):
            result["duration_ms"] = None

    if collection == "traces" and result.get("latency_ms") is None:
        duration = result.get("duration_ms")
        if isinstance(duration, (int, float)):
            result["latency_ms"] = duration

    return result


async def _table_columns(conn: aiosqlite.Connection, collection: str) -> set[str]:
    """Return the live column names for a SQLite collection table."""

    _validate_collection(collection)
    cursor = await conn.execute(f"PRAGMA table_info({collection})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


# Old databases may lack newer columns; fold unknown fields into metadata instead of dropping them.
def _fold_extra_fields(document: dict, columns: set[str]) -> dict:
    """Move fields without real columns into the metadata JSON column."""

    extras = {key: value for key, value in document.items() if key not in columns}
    if not extras:
        return dict(document)
    result = {key: value for key, value in document.items() if key in columns}
    if "metadata" not in columns:
        raise ValueError(f"unsupported fields for collection: {sorted(extras)}")
    metadata = result.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(extras)
    result["metadata"] = json.dumps(metadata, cls=_DateTimeEncoder)
    return result


# Weak references let shutdown cleanup find live backends without keeping them alive.
_OPEN_BACKENDS: weakref.WeakSet[SQLiteBackend] = weakref.WeakSet()


class SQLiteBackend(TraceStorage):
    def __init__(self, db_path: str | Path | None = None) -> None:
        """Create a SQLite backend for the given database file."""

        self._db_path = Path(db_path).expanduser() if db_path else default_db_path()
        self._conn: aiosqlite.Connection | None = None
        self._connected = False

    async def initialize(self) -> None:
        """Open the database, create tables, and apply schema migrations."""

        if self._connected:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        # WAL allows readers alongside writers; busy_timeout avoids instant "database is locked" errors.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute(CREATE_TRACES_TABLE)
        await self._conn.execute(CREATE_SPANS_TABLE)
        await self._conn.execute(CREATE_PROJECTS_TABLE)
        await self._conn.execute(CREATE_API_KEYS_TABLE)
        # Additive migrations upgrade databases created by older versions before indexes are built.
        await ensure_schema(self._conn)
        for sql in TRACE_INDEXES:
            await self._conn.execute(sql)
        for sql in SPAN_INDEXES:
            await self._conn.execute(sql)
        for sql in PROJECT_INDEXES:
            await self._conn.execute(sql)
        for sql in API_KEY_INDEXES:
            await self._conn.execute(sql)
        await self._conn.commit()
        self._connected = True
        _OPEN_BACKENDS.add(self)

    async def close(self) -> None:
        """Close the underlying SQLite connection asynchronously."""

        if self._conn:
            await self._conn.close()
        self._conn = None
        self._connected = False
        _OPEN_BACKENDS.discard(self)

    def close_sync(self) -> None:
        """Stop an open aiosqlite worker without requiring a running event loop."""
        conn = self._conn
        self._conn = None
        self._connected = False
        _OPEN_BACKENDS.discard(self)
        if conn is None:
            return
        # atexit may run with no event loop, so stop and join the worker thread directly.
        conn.stop()
        thread = getattr(conn, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join()

    async def insert_one(self, collection: str, document: dict) -> None:
        """Insert or replace a single document in a collection table."""

        assert self._conn is not None
        _validate_collection(collection)
        columns = await _table_columns(self._conn, collection)
        doc = _prepare_document(collection, _fold_extra_fields(document, columns))
        doc = {key: value for key, value in doc.items() if key in columns}
        column_names = ", ".join(doc.keys())
        placeholders = ", ".join("?" for _ in doc)
        values = list(doc.values())
        # INSERT OR REPLACE gives upsert semantics; rollback avoids leaving a failed write pending.
        try:
            await self._conn.execute(
                f"INSERT OR REPLACE INTO {collection} ({column_names}) VALUES ({placeholders})",
                values,
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def insert_many(self, collection: str, documents: list[dict]) -> None:
        """Insert or replace a batch of documents in one transaction."""

        assert self._conn is not None
        _validate_collection(collection)
        if not documents:
            return
        columns = await _table_columns(self._conn, collection)
        prepared = [
            _prepare_document(collection, _fold_extra_fields(document, columns))
            for document in documents
        ]
        normalized = [
            {key: value for key, value in document.items() if key in columns}
            for document in prepared
        ]
        column_names = sorted({key for document in normalized for key in document})
        if not column_names:
            return
        placeholders = ", ".join("?" for _ in column_names)
        rows = [tuple(document.get(key) for key in column_names) for document in normalized]
        try:
            await self._conn.executemany(
                f"INSERT OR REPLACE INTO {collection} ({', '.join(column_names)}) "
                f"VALUES ({placeholders})",
                rows,
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def find_one(self, collection: str, filter: dict) -> dict | None:
        """Return the first document matching a validated filter."""

        assert self._conn is not None
        _validate_collection(collection)
        where_clause, params = _build_where(filter, collection)
        cursor = await self._conn.execute(
            f"SELECT * FROM {collection}{where_clause} LIMIT 1", params
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _serialize_document(dict(row))

    async def find_many(
        self,
        collection: str,
        filter: dict | None = None,
        sort: list | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """Return matching documents with optional sorting and pagination."""

        assert self._conn is not None
        _validate_collection(collection)
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        where_clause, params = _build_where(filter, collection)
        order_clause = _build_order_by(sort, collection)
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        if offset > 0:
            # SQLite needs a LIMIT before OFFSET; -1 means "unlimited" here.
            limit_clause = f"{limit_clause} OFFSET ?" if limit_clause else " LIMIT -1 OFFSET ?"
            params.append(offset)
        cursor = await self._conn.execute(
            f"SELECT * FROM {collection}{where_clause}{order_clause}{limit_clause}",
            params,
        )
        rows = await cursor.fetchall()
        return [_serialize_document(dict(row)) for row in rows]

    async def count_documents(
        self, collection: str, filter: dict | None = None
    ) -> int:
        """Count documents matching a validated filter."""

        assert self._conn is not None
        _validate_collection(collection)
        where_clause, params = _build_where(filter, collection)
        cursor = await self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM {collection}{where_clause}", params
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def delete_one(self, collection: str, filter: dict) -> int:
        """Delete at most one document matching a filter."""

        return await self.delete_many(collection, filter, limit=1)

    async def delete_many(
        self, collection: str, filter: dict, limit: int | None = None
    ) -> int:
        """Delete matching documents, optionally capped by a row limit."""

        assert self._conn is not None
        _validate_collection(collection)
        where_clause, params = _build_where(filter, collection)
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            params.append(limit)
        try:
            cursor = await self._conn.execute(
                f"DELETE FROM {collection}{where_clause}{limit_clause}", params
            )
            await self._conn.commit()
            return cursor.rowcount
        except Exception:
            await self._conn.rollback()
            raise

    async def delete_trace(self, trace_id: str) -> int:
        """Delete a trace and its child spans in one transaction."""

        assert self._conn is not None
        try:
            # Delete child spans first: the schema has no foreign-key cascade.
            await self._conn.execute("DELETE FROM spans WHERE trace_id = ?", (trace_id,))
            cursor = await self._conn.execute(
                "DELETE FROM traces WHERE trace_id = ?", (trace_id,)
            )
            await self._conn.commit()
            return cursor.rowcount
        except Exception:
            await self._conn.rollback()
            raise

    async def create_index(
        self, collection: str, key_or_list: str | list, unique: bool = False
    ) -> None:
        """Create validated indexes for one or more collection fields."""

        assert self._conn is not None
        _validate_collection(collection)
        if isinstance(key_or_list, str):
            keys = [key_or_list]
        else:
            keys = [k[0] if isinstance(k, (list, tuple)) else k for k in key_or_list]
        for key in keys:
            # Allow-list the field before it becomes an identifier in the DDL string.
            _validate_field(collection, key)
            idx_name = f"idx_{collection}_{key}"
            unique_str = "UNIQUE " if unique else ""
            await self._conn.execute(
                f"CREATE {unique_str}INDEX IF NOT EXISTS {idx_name} ON {collection}({key})"
            )
        await self._conn.commit()

    @property
    def schema_version(self) -> int:
        """Return the schema version this backend migrates to."""

        return CURRENT_SCHEMA_VERSION

    @property
    def is_connected(self) -> bool:
        """Report whether the database connection is open."""

        return self._connected

    @property
    def storage_type(self) -> str:
        """Return the storage backend identifier."""

        return "sqlite"

    @property
    def storage_detail(self) -> str:
        """Return the database path used for health reporting."""

        return str(self._db_path)


def _close_open_backends() -> None:
    """Close all still-open backends at process shutdown."""

    # Iterate a snapshot: close_sync() discards each backend from the weak set.
    for backend in list(_OPEN_BACKENDS):
        backend.close_sync()


def _register_shutdown_cleanup() -> None:
    """Register backend cleanup for both threading shutdown and atexit."""

    # threading's private hook runs before atexit; both are harmless because close_sync is idempotent.
    register = getattr(threading, "_register_atexit", None)
    if callable(register):
        register(_close_open_backends)
    atexit.register(_close_open_backends)


_register_shutdown_cleanup()
