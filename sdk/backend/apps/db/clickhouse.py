"""ClickHouse storage backend using optional ``clickhouse-connect`` dependency."""

from __future__ import annotations

import importlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from tracekit.core.storage import TraceStorage

# Table names and dotted field paths are interpolated into SQL, so both are regex-restricted.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_OPERATORS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists"}
_KNOWN_COLLECTIONS = ("traces", "spans", "projects", "api_keys", "events", "metrics")


# JSONExtractString returns text, so nested values are JSON-encoded and bools become "true"/"false".
def _scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return None
    return str(value)


class ClickHouseBackend(TraceStorage):
    def __init__(
        self,
        client: Any = None,
        *,
        url: str | None = None,
        database: str = "default",
        table_prefix: str = "tracekit_",
        **connect_kwargs: Any,
    ) -> None:
        if not _IDENTIFIER.match(database):
            raise ValueError("database must be a valid identifier")
        if not _IDENTIFIER.match(table_prefix):
            raise ValueError("table_prefix must be a valid identifier")
        self.client = client
        self.url = url
        self.database = database
        self.table_prefix = table_prefix
        self.connect_kwargs = connect_kwargs
        self._connected = False
        self._collections: set[str] = set()

    @property
    def is_connected(self) -> bool:
        return self._connected and self.client is not None

    @property
    def storage_type(self) -> str:
        return "clickhouse"

    @property
    def storage_detail(self) -> str:
        if self.url:
            parts = self.url.split("://", 1)[-1].split("/", 1)[0]
            return parts or self.url
        return str(self.connect_kwargs.get("host", "clickhouse"))

    # Import clickhouse-connect lazily so the backend module works without the optional extra.
    def _load_driver(self) -> Any:
        try:
            return importlib.import_module("clickhouse_connect")
        except ImportError as exc:
            raise RuntimeError(
                "ClickHouse storage requires clickhouse-connect; "
                "install tracekit[clickhouse]"
            ) from exc

    def _table(self, collection: str) -> str:
        if not _IDENTIFIER.match(collection):
            raise ValueError(f"invalid collection name: {collection}")
        return f"`{self.database}`.`{self.table_prefix}{collection}`"

    async def initialize(self) -> None:
        if self.client is None:
            driver = self._load_driver()
            kwargs = dict(self.connect_kwargs)
            if self.url:
                kwargs.setdefault("url", self.url)
            self.client = driver.get_client(**kwargs)
        self._connected = True
        for collection in _KNOWN_COLLECTIONS:
            await self._ensure_collection(collection)

    async def close(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None) or getattr(
                self.client, "disconnect", None
            )
            if close is not None:
                close()
        self.client = None
        self._connected = False
        self._collections.clear()

    # DDL is cached per instance; documents live in MergeTree tables ordered by id.
    async def _ensure_collection(self, collection: str) -> None:
        if collection in self._collections:
            return
        self.client.command(
            f"CREATE TABLE IF NOT EXISTS {self._table(collection)} ("
            "id String, data String, created_at DateTime64(3) "
            ") ENGINE = MergeTree ORDER BY id"
        )
        self._collections.add(collection)

    @staticmethod
    def _key(document: dict[str, Any]) -> str:
        for field in ("trace_id", "span_id", "id"):
            value = document.get(field)
            if value is not None:
                return str(value)
        return uuid.uuid4().hex

    @staticmethod
    # Validate the dotted path before turning each segment into a JSONExtractString argument.
    def _field_expression(field: str) -> str:
        if not _FIELD.match(field):
            raise ValueError(f"invalid filter field: {field}")
        parts = field.split(".")
        escaped = ", ".join(f"'{part}'" for part in parts)
        return f"JSONExtractString(data, {escaped})"

    @staticmethod
    def _row_data(row: Any, columns: list[str] | None = None) -> dict[str, Any]:
        if isinstance(row, Mapping):
            raw = row.get("data")
        else:
            index = columns.index("data") if columns and "data" in columns else 0
            raw = row[index]
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, Mapping):
            return dict(raw)
        return {}

    def _where(self, filter_dict: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        if not filter_dict:
            return "", {}
        clauses: list[str] = []
        params: dict[str, Any] = {}
        counter = 0

        def add_parameter(value: Any, value_type: str = "String") -> str:
            nonlocal counter
            name = f"p{counter}"
            counter += 1
            params[name] = _scalar(value) if value_type == "String" else value
            return "{" + f"{name}:{value_type}" + "}"

        for key, value in filter_dict.items():
            if key.startswith("$"):
                continue
            expression = self._field_expression(str(key))
            if isinstance(value, dict) and any(op in _OPERATORS for op in value):
                for operator, operand in value.items():
                    # JSONHas tests key presence rather than comparing the stored value.
                    if operator == "$exists":
                        clauses.append(f"JSONHas(data, {add_parameter(str(key))}) = {int(bool(operand))}")
                    elif operator in {"$gt", "$gte", "$lt", "$lte"}:
                        # JSON values are strings; toFloat64OrNull ignores non-numeric data.
                        numeric = f"toFloat64OrNull({expression})"
                        placeholder = add_parameter(operand, "Float64")
                        sql_operator = {
                            "$gt": ">",
                            "$gte": ">=",
                            "$lt": "<",
                            "$lte": "<=",
                        }[operator]
                        clauses.append(f"{numeric} {sql_operator} {placeholder}")
                    elif operator in {"$in", "$nin"}:
                        values = operand if isinstance(operand, (list, tuple, set)) else [operand]
                        placeholders = ", ".join(add_parameter(item) for item in values)
                        clauses.append(
                            f"{expression} {'IN' if operator == '$in' else 'NOT IN'} "
                            f"({placeholders})"
                        )
                    elif operator == "$ne":
                        clauses.append(f"{expression} != {add_parameter(operand)}")
                    elif operator == "$eq":
                        clauses.append(f"{expression} = {add_parameter(operand)}")
            else:
                clauses.append(f"{expression} = {add_parameter(value)}")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    async def insert_one(self, collection: str, document: dict[str, Any]) -> None:
        await self.insert_many(collection, [document])

    async def insert_many(
        self, collection: str, documents: list[dict[str, Any]]
    ) -> None:
        if not documents:
            return
        await self._ensure_collection(collection)
        rows: list[dict[str, Any]] = []
        for document in documents:
            key = self._key(document)
            payload = dict(document)
            if "id" not in payload:
                payload["id"] = key
            # created_at populates the table's time column; fall back to start_time or now.
            created_at = payload.get("created_at") or payload.get("start_time")
            if created_at is None:
                created_at = datetime.now(timezone.utc).isoformat()
            rows.append(
                {
                    "id": key,
                    "data": json.dumps(payload, default=str, sort_keys=True),
                    "created_at": str(created_at),
                }
            )
        # clickhouse-connect is synchronous, so this call blocks the event loop.
        self.client.insert(
            self._table(collection),
            rows,
            column_names=["id", "data", "created_at"],
        )

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
        sql = f"SELECT data FROM {self._table(collection)}{where_sql}"
        if sort:
            order_parts: list[str] = []
            for item in sort:
                field, direction = item if isinstance(item, (list, tuple)) else (item, -1)
                descending = direction in (-1, "desc", "DESC")
                order_parts.append(
                    f"{self._field_expression(str(field))} {'DESC' if descending else 'ASC'}"
                )
            sql += " ORDER BY " + ", ".join(order_parts)
        if limit is not None:
            params["limit_value"] = limit
            sql += " LIMIT {limit_value:UInt64}"
        if offset:
            params["offset_value"] = offset
            sql += " OFFSET {offset_value:UInt64}"
        result = self.client.query(sql, parameters=params)
        columns = list(getattr(result, "column_names", []) or [])
        return [self._row_data(row, columns) for row in result.result_rows]

    async def count_documents(
        self, collection: str, filter: dict[str, Any] | None = None
    ) -> int:
        await self._ensure_collection(collection)
        where_sql, params = self._where(filter)
        result = self.client.query(
            f"SELECT count() FROM {self._table(collection)}{where_sql}",
            parameters=params,
        )
        return int(result.result_rows[0][0])

    async def delete_many(
        self, collection: str, filter: dict[str, Any], limit: int | None = None
    ) -> int:
        if limit is not None and limit < 1:
            return 0
        await self._ensure_collection(collection)
        where_sql, params = self._where(filter)
        table = self._table(collection)
        if limit is not None:
            # ALTER DELETE has no LIMIT, so resolve ids first and delete only those rows.
            result = self.client.query(
                f"SELECT id FROM {table}{where_sql} LIMIT {int(limit)}",
                parameters=params,
            )
            ids = [row[0] for row in result.result_rows]
            if not ids:
                return 0
            placeholders = ", ".join(
                "{" + f"id{i}:String" + "}" for i in range(len(ids))
            )
            params = {f"id{i}": str(value) for i, value in enumerate(ids)}
            where_sql = f" WHERE id IN ({placeholders})"
        self.client.command(
            f"ALTER TABLE {table} DELETE{where_sql} SETTINGS mutations_sync = 1",
            parameters=params,
        )
        if limit is not None:
            return len(ids)
        result = self.client.query(
            f"SELECT count() FROM {table}{where_sql}", parameters=params
        )
        return int(result.result_rows[0][0])

    async def create_index(
        self, collection: str, key_or_list: str | list[Any], unique: bool = False
    ) -> None:
        await self._ensure_collection(collection)
        fields = [key_or_list] if isinstance(key_or_list, str) else [
            item[0] if isinstance(item, (list, tuple)) else item for item in key_or_list
        ]
        suffix = "_".join(str(field).replace(".", "_") for field in fields)
        if not _IDENTIFIER.match(suffix):
            raise ValueError("invalid index field")
        expression = ", ".join(self._field_expression(str(field)) for field in fields)
        # Bloom-filter indexes speed up the JSONExtractString predicates used by filters.
        self.client.command(
            f"ALTER TABLE {self._table(collection)} ADD INDEX IF NOT EXISTS "
            f"`{self.table_prefix}{collection}_{suffix}` ({expression}) "
            "TYPE bloom_filter GRANULARITY 4"
        )

    async def health_check(self) -> dict[str, Any]:
        result = await super().health_check()
        if self.is_connected:
            try:
                # A failed probe reports "unavailable" instead of raising to the caller.
                response = self.client.query("SELECT 1")
                result["status"] = "ok" if response.result_rows[0][0] == 1 else "unavailable"
            except Exception:
                result["status"] = "unavailable"
        return result
