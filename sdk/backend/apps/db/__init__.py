"""Storage backends and connection pooling."""

# Drivers for Postgres/ClickHouse load lazily, so eager re-export needs no optional deps.
from backend.apps.db.clickhouse import ClickHouseBackend
from backend.apps.db.memory import MemoryBackend
from backend.apps.db.pool import AsyncConnectionPool, PoolConfig, PoolTimeoutError
from backend.apps.db.postgres import PostgreSQLBackend
from backend.apps.db.sqllite import SQLiteBackend

__all__ = [
    "AsyncConnectionPool",
    "ClickHouseBackend",
    "MemoryBackend",
    "PoolConfig",
    "PoolTimeoutError",
    "PostgreSQLBackend",
    "SQLiteBackend",
]
