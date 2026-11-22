"""PostgreSQL storage backend using optional ``asyncpg`` dependency."""

from __future__ import annotations

import importlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tracekit.core.storage import TraceStorage

# Identifiers are interpolated into SQL, so restrict names to plain unquoted-safe tokens.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Filter operators outside this set are ignored rather than translated into SQL.
_OPERATORS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists"}
_KNOWN_COLLECTIONS = ("traces", "spans", "projects", "api_keys", "events", "metrics")


# JSONB values are extracted as text, so nested data is encoded and bools become "true"/"false".
def _scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return None
    return str(value)


class PostgreSQLBackend(TraceStorage):
    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        table_prefix: str = "tracekit_",
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 5.0,
        **connect_kwargs: Any,
    ) -> None:
        if not _IDENTIFIER.match(table_prefix):
            raise ValueError("table_prefix must contain only letters, digits, and underscores")
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("invalid pool sizes")
        self.dsn = dsn
        self.pool = pool
        self.table_prefix = table_prefix
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout
        self.connect_kwargs = connect_kwargs
        self._driver: Any = None
        self._connected = False
        self._collections: set[str] = set()

    @property
    def is_connected(self) -> bool:
        return self._connected and self.pool is not None

    @property
    def storage_type(self) -> str:
        return "postgresql"

    @property
    def storage_detail(self) -> str:
        # Credentials are stripped so status output cannot leak a password.
        if self.dsn:
            parts = urlsplit(self.dsn)
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            if parts.username:
                netloc = f"{parts.username}@{netloc}"
            return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        return str(self.connect_kwargs.get("host", "postgresql"))

    # Import asyncpg lazily so the backend module works without the optional extra installed.
    def _load_driver(self) -> Any:
        if self._driver is None:
            try:
                self._driver = importlib.import_module("asyncpg")
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL storage requires asyncpg; install tracekit[postgres]"
                ) from exc
        return self._driver

    def _table(self, collection: str) -> str:
        if not _IDENTIFIER.match(collection):
            raise ValueError(f"invalid collection name: {collection}")
        return f'"{self.table_prefix}{collection}"'

    def _index_name(self, collection: str, suffix: str) -> str:
        if not _IDENTIFIER.match(suffix):
            raise ValueError(f"invalid index suffix: {suffix}")
        return f'"{self.table_prefix}{collection}_{suffix}"'

    async def initialize(self) -> None:
        if self.pool is None:
            driver = self._load_driver()
            create_pool = driver.create_pool
            if self.dsn:
                self.pool = await create_pool(
                    self.dsn,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    timeout=self.timeout,
                )
            else:
                self.pool = await create_pool(
                    min_size=self.min_size,
                    max_size=self.max_size,
                    timeout=self.timeout,
                    **self.connect_kwargs,
                )
        self._connected = True
        for collection in _KNOWN_COLLECTIONS:
            await self._ensure_collection(collection)

    async def close(self) -> None:
        if self.pool is not None:
            close = getattr(self.pool, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        self.pool = None
        self._connected = False
        self._collections.clear()

    # Cache DDL per instance; each collection is a key/data JSONB table with a GIN index.
    async def _ensure_collection(self, collection: str) -> None:
        table = self._table(collection)
        if collection in self._collections:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                "(key TEXT PRIMARY KEY, data JSONB NOT NULL)"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {self._index_name(collection, 'data_gin')} "
                f"ON {table} USING GIN (data)"
            )
        self._collections.add(collection)

    @staticmethod
    def _key(document: dict[str, Any]) -> str:
        # Prefer a stable caller id so upserts replace the same row across batches.
        for field in ("trace_id", "span_id", "id"):
            value = document.get(field)
            if value is not None:
                return str(value)
        return uuid.uuid4().hex

    @staticmethod
    def _row_data(row: Any) -> dict[str, Any]:
        raw = row["data"] if isinstance(row, Mapping) else row[0]
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw)

    @staticmethod
    def _extract_keys(rows: list[Any]) -> list[str]:
        keys: list[str] = []
        for row in rows:
            value = row["key"] if isinstance(row, Mapping) else row[0]
            if value is not None:
                keys.append(str(value))
        return keys

    # Dotted keys address nested JSON: ->> handles top-level keys, #>> needs a text[] path.
    def _json_expression(self, key: str, parameter: str) -> str:
        parts = [part for part in key.split(".") if part]
        if not parts:
            parts = [key]
        if len(parts) == 1:
            return f"data->>${parameter}"
        return f"data#>>${parameter}::text[]"

    def _where(self, filter_dict: dict[str, Any] | None) -> tuple[str, list[Any]]:
        if not filter_dict:
            return "", []
        params: list[Any] = []
        clauses: list[str] = []

        def add_expression(key: str, operator: str, value: Any) -> str:
            if operator in {"$gt", "$gte", "$lt", "$lte"}:
                parts = [part for part in key.split(".") if part]
                parameter_key = str(len(params) + 1)
                params.append(parts if len(parts) > 1 else parts[0] if parts else key)
                expression = self._json_expression(key, parameter_key)
                params.append(_scalar(value))
                parameter_value = f"${len(params)}"
                # ->> yields text, so cast both sides to numeric to avoid lexicographic ordering.
                sql_operator = {
                    "$gt": ">",
                    "$gte": ">=",
                    "$lt": "<",
                    "$lte": "<=",
                }[operator]
                return f"({expression})::numeric {sql_operator} ({parameter_value})::numeric"

            parts = [part for part in key.split(".") if part]
            parameter_key = str(len(params) + 1)
            params.append(parts if len(parts) > 1 else parts[0] if parts else key)
            expression = self._json_expression(key, parameter_key)
            if operator == "$exists":
                placeholder = f"${parameter_key}"
                return f"data ? {placeholder}" if value else f"NOT (data ? {placeholder})"
            parameter_value = f"${len(params) + 1}"
            if operator in {"$in", "$nin"}:
                values = value if isinstance(value, (list, tuple, set)) else [value]
                params.append([_scalar(item) for item in values])
            else:
                params.append(_scalar(value))
            if operator == "$in":
                return f"{expression} = ANY ({parameter_value}::text[])"
            if operator == "$nin":
                return f"{expression} <> ALL ({parameter_value}::text[])"
            if operator == "$ne":
                # IS DISTINCT FROM keeps inequality meaningful for NULL values.
                return f"{expression} IS DISTINCT FROM {parameter_value}"
            return f"{expression} = {parameter_value}"

        for key, value in filter_dict.items():
            if key.startswith("$"):
                continue
            if isinstance(value, dict) and any(op in _OPERATORS for op in value):
                for operator, operand in value.items():
                    if operator in _OPERATORS:
                        clauses.append(add_expression(key, operator, operand))
            else:
                clauses.append(add_expression(key, "$eq", value))
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    async def insert_one(self, collection: str, document: dict[str, Any]) -> None:
        await self.insert_many(collection, [document])

    async def insert_many(
        self, collection: str, documents: list[dict[str, Any]]
    ) -> None:
        if not documents:
            return
        await self._ensure_collection(collection)
        table = self._table(collection)
        # ON CONFLICT makes repeated inserts idempotent; fall back when executemany is missing.
        sql = (
            f"INSERT INTO {table} (key, data) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data"
        )
        rows = []
        for document in documents:
            key = self._key(document)
            payload = dict(document)
            if "id" not in payload:
                payload["id"] = key
            rows.append((key, json.dumps(payload, default=str, sort_keys=True)))
        async with self.pool.acquire() as connection:
            executor = getattr(connection, "executemany", None)
            if executor is not None:
                await executor(sql, rows)
                return
            for row in rows:
                await connection.execute(sql, *row)

    async def find_one(
        self, collection: str, filter: dict[str, Any]
    ) -> dict[str, Any] | None:
        results = await self.find_many(collection, filter, limit=1)
        return results[0] if results else None

    async def find_many(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        sort: list[Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        await self._ensure_collection(collection)
        where_sql, params = self._where(filter)
        sql = f"SELECT data::text FROM {self._table(collection)}{where_sql}"
        if sort:
            order_parts: list[str] = []
            for item in sort:
                field, direction = item if isinstance(item, (list, tuple)) else (item, -1)
                parts = [part for part in str(field).split(".") if part]
                parameter = str(len(params) + 1)
                params.append(parts if len(parts) > 1 else parts[0] if parts else field)
                expression = self._json_expression(str(field), parameter)
                descending = direction in (-1, "desc", "DESC")
                order_parts.append(f"{expression} {'DESC' if descending else 'ASC'}")
            sql += " ORDER BY " + ", ".join(order_parts)
        if limit is not None:
            params.append(limit)
            sql += f" LIMIT ${len(params)}"
        if offset:
            params.append(offset)
            sql += f" OFFSET ${len(params)}"
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(sql, *params)
        return [self._row_data(row) for row in rows]

    async def count_documents(
        self, collection: str, filter: dict[str, Any] | None = None
    ) -> int:
        await self._ensure_collection(collection)
        where_sql, params = self._where(filter)
        sql = f"SELECT count(*) FROM {self._table(collection)}{where_sql}"
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(sql, *params)
        return int(value or 0)

    async def delete_many(
        self, collection: str, filter: dict[str, Any], limit: int | None = None
    ) -> int:
        if limit is not None and limit < 1:
            return 0
        await self._ensure_collection(collection)
        where_sql, params = self._where(filter)
        table = self._table(collection)
        async with self.pool.acquire() as connection:
            if limit is not None:
                # PostgreSQL has no DELETE LIMIT, so resolve keys first and delete by id.
                params.append(limit)
                rows = await connection.fetch(
                    f"SELECT key FROM {table}{where_sql} LIMIT ${len(params)}", *params
                )
                keys = self._extract_keys(list(rows))
                if not keys:
                    return 0
                await connection.execute(
                    f"DELETE FROM {table} WHERE key = ANY ($1::text[])", keys
                )
                return len(keys)
            result = await connection.execute(f"DELETE FROM {table}{where_sql}", *params)
        if isinstance(result, int):
            return result
        match = re.search(r"DELETE\s+(\d+)", str(result))
        return int(match.group(1)) if match else 0

    @staticmethod
    # DDL cannot bind parameters, so JSON path parts are escaped into string literals.
    def _json_literal_expression(key: str) -> str:
        parts = [part.replace("'", "''") for part in key.split(".") if part]
        if not parts:
            parts = [key.replace("'", "''")]
        if len(parts) == 1:
            return f"data->>'{parts[0]}'"
        array = "{" + ",".join(parts) + "}"
        return f"data#>>'{array}'::text[]"

    async def create_index(
        self, collection: str, key_or_list: str | list[Any], unique: bool = False
    ) -> None:
        await self._ensure_collection(collection)
        fields = [key_or_list] if isinstance(key_or_list, str) else [
            item[0] if isinstance(item, (list, tuple)) else item for item in key_or_list
        ]
        suffix_parts: list[str] = []
        expression_parts: list[str] = []
        for field in fields:
            name = str(field)
            if not _IDENTIFIER.match(name.replace(".", "_")):
                raise ValueError(f"invalid index field: {name}")
            suffix_parts.append(name.replace(".", "_"))
            expression_parts.append(f"({self._json_literal_expression(name)})")
        suffix = "_".join(suffix_parts)
        unique_sql = "UNIQUE " if unique else ""
        # Generated names are truncated to stay under PostgreSQL's identifier length limit.
        sql = (
            f"CREATE {unique_sql}INDEX IF NOT EXISTS "
            f"{self._index_name(collection, f'idx_{suffix}'[:48])} "
            f"ON {self._table(collection)} ({', '.join(expression_parts)})"
        )
        async with self.pool.acquire() as connection:
            await connection.execute(sql)

    async def health_check(self) -> dict[str, Any]:
        result = await super().health_check()
        if self.is_connected:
            try:
                # A failed probe reports "unavailable" instead of raising to the caller.
                async with self.pool.acquire() as connection:
                    value = await connection.fetchval("SELECT 1")
                result["status"] = "ok" if value == 1 else "unavailable"
            except Exception:
                result["status"] = "unavailable"
        return result
