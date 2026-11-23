"""Sequential SQLite schema migrations for Tracekit."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

# Bump only alongside a new numbered entry in _MIGRATIONS.
CURRENT_SCHEMA_VERSION = 6

CREATE_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS SCHEMA_VERSION (
    version INTEGER PRIMARY KEY,
    applied_at TEXT
)
"""

_BASELINE_TABLES = ("traces", "spans", "projects", "api_keys")

# Target columns for additively upgrading databases created by older versions.
_CANONICAL_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "traces": (
        ("trace_id", "TEXT"),
        ("name", "TEXT"),
        ("start_time", "TEXT"),
        ("end_time", "TEXT"),
        ("duration_ms", "REAL"),
        ("status", "TEXT"),
        ("attributes", "TEXT"),
        ("metadata", "TEXT"),
        ("error", "TEXT"),
        ("prompt", "TEXT"),
        ("response", "TEXT"),
        ("latency", "REAL"),
        ("token_count", "INTEGER"),
        ("model_name", "TEXT"),
        ("project_id", "TEXT"),
        ("project_name", "TEXT"),
        ("api_key", "TEXT"),
        ("environment", "TEXT"),
        ("retry_count", "INTEGER"),
        ("slow_request", "INTEGER"),
        ("failure_reason", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("steps", "TEXT"),
        ("error_type", "TEXT"),
        ("latency_ms", "REAL"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("total_tokens", "INTEGER"),
        ("token_usage_source", "TEXT"),
    ),
    "spans": (
        ("span_id", "TEXT"),
        ("trace_id", "TEXT"),
        ("parent_span_id", "TEXT"),
        ("name", "TEXT"),
        ("span_type", "TEXT"),
        ("start_time", "TEXT"),
        ("end_time", "TEXT"),
        ("duration_ms", "REAL"),
        ("status", "TEXT"),
        ("input", "TEXT"),
        ("output", "TEXT"),
        ("error", "TEXT"),
        ("attributes", "TEXT"),
        ("events", "TEXT"),
        ("error_type", "TEXT"),
        ("project_id", "TEXT"),
        ("environment", "TEXT"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("total_tokens", "INTEGER"),
        ("token_usage_source", "TEXT"),
        ("is_final", "INTEGER NOT NULL DEFAULT 0"),
        ("handled", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "projects": (
        ("project_id", "TEXT"),
        ("name", "TEXT"),
        ("description", "TEXT"),
        ("created_at", "TEXT"),
        ("archived", "INTEGER DEFAULT 0"),
        ("updated_at", "TEXT"),
    ),
    "api_keys": (
        ("key", "TEXT"),
        ("key_hash", "TEXT"),
        ("name", "TEXT"),
        ("salt", "TEXT"),
        ("project_id", "TEXT"),
        ("environment", "TEXT"),
        ("metadata", "TEXT"),
        ("expires_at", "TEXT"),
        ("revoked", "INTEGER DEFAULT 0"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ),
}


# Match the UTC microsecond format used by the timestamp columns in stored documents.
def _now() -> str:
    """Return the UTC timestamp format used by schema bookkeeping."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    """Check whether a named table exists before migrating it."""

    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    """Return the column names currently present on a table."""

    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


# No version table means a pre-migration database, which callers bootstrap at version 1.
async def get_schema_version(conn: aiosqlite.Connection) -> int | None:
    """Return the highest applied schema version, or None for legacy databases."""

    if not await _table_exists(conn, "SCHEMA_VERSION"):
        return None
    cursor = await conn.execute("SELECT MAX(version) FROM SCHEMA_VERSION")
    row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


async def _set_schema_version(conn: aiosqlite.Connection, version: int) -> None:
    """Record a schema version as applied."""

    await conn.execute(
        "INSERT OR REPLACE INTO SCHEMA_VERSION (version, applied_at) VALUES (?, ?)",
        (version, _now()),
    )


# PRAGMA-based check makes this safe to repeat after a partially applied upgrade.
async def _add_column_if_missing(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add a column only when the table does not already have it."""

    columns = await _table_columns(conn, table)
    if column not in columns:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# Version 1 marks the baseline tables already created by SQLiteBackend.initialize().
async def _migration_1(conn: aiosqlite.Connection) -> None:
    """Mark the baseline schema created by SQLiteBackend.initialize()."""


# Older databases predate several columns; add them without rewriting existing rows.
async def _migration_2(conn: aiosqlite.Connection) -> None:
    """Add canonical columns that older databases may lack."""

    for table in _BASELINE_TABLES:
        if not await _table_exists(conn, table):
            raise RuntimeError(f"cannot migrate missing table: {table}")
        for column, definition in _CANONICAL_COLUMNS[table]:
            await _add_column_if_missing(conn, table, column, definition)


# IF NOT EXISTS keeps these index additions safe after a partial upgrade.
async def _migration_3(conn: aiosqlite.Connection) -> None:
    """Create index improvements for common trace lookups."""

    indexes = (
        "CREATE INDEX IF NOT EXISTS idx_traces_error_type ON traces(error_type)",
        "CREATE INDEX IF NOT EXISTS idx_traces_latency_ms ON traces(latency_ms)",
        "CREATE INDEX IF NOT EXISTS idx_traces_start_time ON traces(start_time)",
        "CREATE INDEX IF NOT EXISTS idx_spans_span_type ON spans(span_type)",
        "CREATE INDEX IF NOT EXISTS idx_spans_error_type ON spans(error_type)",
        "CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash)",
        "CREATE INDEX IF NOT EXISTS idx_projects_archived ON projects(archived)",
    )
    for statement in indexes:
        await conn.execute(statement)


# Composite indexes cover the common project/time and time-range filters.
async def _migration_4(conn: aiosqlite.Connection) -> None:
    """Add composite indexes for project, time, model, and span queries."""

    indexes = (
        "CREATE INDEX IF NOT EXISTS idx_traces_project_start ON traces(project_id, start_time)",
        "CREATE INDEX IF NOT EXISTS idx_traces_environment_start ON traces(environment, start_time)",
        "CREATE INDEX IF NOT EXISTS idx_traces_model_start ON traces(model_name, start_time)",
        "CREATE INDEX IF NOT EXISTS idx_traces_duration ON traces(duration_ms)",
        "CREATE INDEX IF NOT EXISTS idx_spans_trace_start ON spans(trace_id, start_time)",
    )
    for statement in indexes:
        await conn.execute(statement)


async def _migration_5(conn: aiosqlite.Connection) -> None:
    """Add token usage columns to trace and span tables."""

    for table in ("traces", "spans"):
        if not await _table_exists(conn, table):
            continue
        for column, definition in _CANONICAL_COLUMNS[table]:
            if column.startswith("token") or column.endswith("_tokens"):
                await _add_column_if_missing(conn, table, column, definition)


async def _migration_6(conn: aiosqlite.Connection) -> None:
    """Add final/handled markers to the span table."""

    if not await _table_exists(conn, "spans"):
        return
    for column, definition in _CANONICAL_COLUMNS["spans"]:
        if column in ("is_final", "handled"):
            await _add_column_if_missing(conn, "spans", column, definition)


# Versions must be contiguous; ensure_schema refuses to skip a missing step.
_MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
}


# Bootstrap unversioned databases at 1, apply pending steps in order, then commit once.
async def ensure_schema(conn: aiosqlite.Connection) -> int:
    """Bootstrap legacy databases and apply pending migrations in order."""

    await conn.execute(CREATE_SCHEMA_VERSION_TABLE)
    version = await get_schema_version(conn)
    if version is None:
        version = 1
        await _set_schema_version(conn, version)

    while version < CURRENT_SCHEMA_VERSION:
        next_version = version + 1
        migration = _MIGRATIONS.get(next_version)
        if migration is None:
            raise RuntimeError(f"missing migration version {next_version}")
        await migration(conn)
        await _set_schema_version(conn, next_version)
        version = next_version

    await conn.commit()
    return version
