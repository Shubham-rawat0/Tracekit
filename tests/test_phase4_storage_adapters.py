import asyncio
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from backend.apps.db.clickhouse import ClickHouseBackend
from backend.apps.db.clickhouse import _scalar as clickhouse_scalar
from backend.apps.db.postgres import PostgreSQLBackend
from backend.apps.db.postgres import _scalar as postgres_scalar


class FakePgConnection:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, tuple]] = []

    @staticmethod
    def _table(sql: str) -> str:
        match = re.search(r'"(tracekit_[A-Za-z0-9_]+)"', sql)
        return match.group(1) if match else ""

    async def execute(self, sql: str, *params) -> str:
        self.calls.append((sql, params))
        table = self._table(sql)
        if sql.startswith("CREATE"):
            return "CREATE"
        if sql.startswith("INSERT"):
            key, data = params
            self.store.setdefault(table, {})[str(key)] = json.loads(data)
            return "INSERT 0 1"
        if sql.startswith("DELETE"):
            keys = params[0] if params and isinstance(params[0], list) else []
            documents = self.store.setdefault(table, {})
            for key in keys:
                documents.pop(str(key), None)
            return f"DELETE {len(keys)}"
        return "OK"

    async def executemany(self, sql: str, rows) -> None:
        for row in rows:
            await self.execute(sql, *row)

    async def fetch(self, sql: str, *params) -> list[tuple]:
        self.calls.append((sql, params))
        table = self._table(sql)
        documents = self.store.get(table, {})
        if "data::text" in sql:
            rows = [(json.dumps(document),) for document in documents.values()]
        else:
            rows = [(key,) for key in documents]
        if "LIMIT" in sql and params and isinstance(params[-1], int):
            rows = rows[: params[-1]]
        return rows

    async def fetchval(self, sql: str, *params):
        self.calls.append((sql, params))
        if "count(" in sql:
            return len(self.store.get(self._table(sql), {}))
        if "SELECT 1" in sql:
            return 1
        return None


class FakePgPool:
    def __init__(self) -> None:
        self.connection = FakePgConnection()
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self) -> None:
        self.closed = True


class FakeQueryResult:
    def __init__(self, rows, columns=None) -> None:
        self.result_rows = rows
        self.column_names = columns or []


class FakeCHClient:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, dict | None]] = []

    @staticmethod
    def _table(sql: str) -> str:
        match = re.search(r"`([^`]+)`\.`([^`]+)`", sql)
        return match.group(0) if match else ""

    def command(self, sql: str, parameters: dict | None = None) -> None:
        self.calls.append((sql, parameters))
        if "DELETE" in sql and parameters:
            table = self._table(sql)
            for value in parameters.values():
                self.store.get(table, {}).pop(str(value), None)

    def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
        self.calls.append((sql, parameters))
        if "SELECT 1" in sql:
            return FakeQueryResult([(1,)])
        table = self._table(sql)
        documents = self.store.get(table, {})
        if "count()" in sql:
            return FakeQueryResult([(len(documents),)])
        if "SELECT id" in sql:
            rows = [(key,) for key in documents]
            match = re.search(r"LIMIT (\d+)", sql)
            if match:
                rows = rows[: int(match.group(1))]
            return FakeQueryResult(rows, ["id"])
        return FakeQueryResult(
            [(json.dumps(document),) for document in documents.values()], ["data"]
        )

    def insert(self, table: str, rows, column_names=None) -> None:
        self.rows = rows
        self.calls.append((f"INSERT {table}", None))
        for row in rows:
            document = json.loads(row["data"])
            self.store.setdefault(table, {})[document["id"]] = document

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_postgres_backend_roundtrip_delete_and_health():
    pool = FakePgPool()
    backend = PostgreSQLBackend(pool=pool)

    await backend.initialize()
    await backend.save_trace({"trace_id": "t1", "name": "checkout"})
    assert await backend.get_trace("t1") == {"trace_id": "t1", "name": "checkout", "id": "t1"}
    assert await backend.count_documents("traces") == 1
    assert (await backend.health_check())["status"] == "ok"

    assert await backend.delete_trace("t1") is True
    assert await backend.count_documents("traces") == 0

    await backend.close()
    assert pool.closed is True


@pytest.mark.asyncio
async def test_postgres_backend_filter_sql_and_optional_driver_error():
    backend = PostgreSQLBackend(pool=FakePgPool())
    where_sql, params = backend._where(
        {"name": "checkout", "duration_ms": {"$gte": 10}, "status": {"$in": ["ok", "error"]}}
    )
    assert "data->>$1 = $2" in where_sql
    assert "::numeric >= ($4)::numeric" in where_sql
    assert "ANY ($6::text[])" in where_sql
    assert params == ["name", "checkout", "duration_ms", "10", "status", ["ok", "error"]]

    missing = PostgreSQLBackend("postgresql://localhost/tracekit")
    with patch("importlib.import_module", side_effect=ImportError("missing")):
        with pytest.raises(RuntimeError, match="PostgreSQL storage requires asyncpg"):
            await missing.initialize()


@pytest.mark.asyncio
async def test_clickhouse_backend_roundtrip_delete_and_health():
    client = FakeCHClient()
    backend = ClickHouseBackend(client=client)

    await backend.initialize()
    await backend.save_trace({"trace_id": "t2", "name": "checkout"})
    assert (await backend.get_trace("t2"))["name"] == "checkout"
    assert await backend.count_documents("traces") == 1
    assert (await backend.health_check())["status"] == "ok"

    assert await backend.delete_trace("t2") is True
    assert await backend.count_documents("traces") == 0

    await backend.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_clickhouse_backend_filter_sql_and_optional_driver_error():
    backend = ClickHouseBackend(client=FakeCHClient())
    where_sql, params = backend._where(
        {"name": "checkout", "duration_ms": {"$lt": 50}, "status": {"$in": ["ok"]}}
    )
    assert "JSONExtractString(data, 'name') = {p0:String}" in where_sql
    assert "toFloat64OrNull(JSONExtractString(data, 'duration_ms')) < {p1:Float64}" in where_sql
    assert "IN ({p2:String})" in where_sql
    assert params == {"p0": "checkout", "p1": 50, "p2": "ok"}

    missing = ClickHouseBackend(url="http://localhost:8123")
    with patch("importlib.import_module", side_effect=ImportError("missing")):
        with pytest.raises(RuntimeError, match="ClickHouse storage requires clickhouse-connect"):
            await missing.initialize()


@pytest.mark.asyncio
async def test_backend_invalid_collection_rejected():
    postgres = PostgreSQLBackend(pool=FakePgPool())
    clickhouse = ClickHouseBackend(client=FakeCHClient())
    await postgres.initialize()
    await clickhouse.initialize()

    with pytest.raises(ValueError, match="invalid collection"):
        await postgres.insert_one("bad;drop", {"id": "x"})
    with pytest.raises(ValueError, match="invalid collection"):
        await clickhouse.insert_one("bad;drop", {"id": "x"})


class NoExecManyConnection(FakePgConnection):
    executemany = None


class NoExecManyPool(FakePgPool):
    def __init__(self) -> None:
        super().__init__()
        self.connection = NoExecManyConnection()


class BrokenPgPool(FakePgPool):
    @asynccontextmanager
    async def acquire(self):
        raise RuntimeError("db down")
        yield


class IntResultPgConnection(FakePgConnection):
    async def execute(self, sql: str, *params) -> str:
        if sql.startswith("DELETE"):
            return "DELETE 5"
        return await super().execute(sql, *params)


class IntResultPgPool(FakePgPool):
    def __init__(self) -> None:
        super().__init__()
        self.connection = IntResultPgConnection()


class BrokenCHClient(FakeCHClient):
    def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
        raise RuntimeError("clickhouse down")


@pytest.mark.asyncio
async def test_postgres_backend_helpers_and_driver_initialization():
    assert postgres_scalar({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert postgres_scalar([1, 2]) == "[1, 2]"
    assert postgres_scalar(True) == "true"
    assert postgres_scalar(False) == "false"
    assert postgres_scalar(None) is None
    assert postgres_scalar(7) == "7"

    backend = PostgreSQLBackend(
        "postgresql://user:secret@localhost:5432/tracekit", pool=FakePgPool()
    )
    assert backend.storage_detail == "postgresql://user@localhost:5432/tracekit"
    assert PostgreSQLBackend(host="db").storage_detail == "db"
    assert backend._json_expression("name", "1") == "data->>$1"
    assert backend._json_expression("meta.name", "2") == "data#>>$2::text[]"
    assert backend._row_data({"data": {"x": 1}}) == {"x": 1}
    assert backend._row_data((json.dumps({"x": 2}),)) == {"x": 2}
    assert backend._extract_keys([{"key": "a"}, {"key": None}, ("b",)]) == ["a", "b"]

    with pytest.raises(ValueError, match="invalid index suffix"):
        backend._index_name("traces", "bad-suffix")

    where_sql, params = backend._where(
        {
            "$or": {"ignored": True},
            "meta.name": {"$exists": False},
            "status": {"$ne": "ok"},
            "tags": {"$nin": ["a", "b"]},
            "duration_ms": {"$gte": 3},
        }
    )
    assert "NOT (data ? $1)" in where_sql
    assert "IS DISTINCT FROM" in where_sql
    assert "<> ALL" in where_sql
    assert "::numeric >= " in where_sql
    assert params[0] == ["meta", "name"]
    assert params[-1] == "3"

    pools: list[tuple[tuple, dict]] = []

    async def create_pool(*args, **kwargs):
        pools.append((args, kwargs))
        return FakePgPool()

    driver = SimpleNamespace(create_pool=create_pool)
    dsn_backend = PostgreSQLBackend("postgresql://user:secret@localhost:5432/tracekit")
    with patch.object(dsn_backend, "_load_driver", return_value=driver):
        await dsn_backend.initialize()
    assert pools[0][0] == ("postgresql://user:secret@localhost:5432/tracekit",)

    no_dsn = PostgreSQLBackend(host="db", min_size=1, max_size=2)
    with patch.object(no_dsn, "_load_driver", return_value=driver):
        await no_dsn.initialize()
    assert pools[1][0] == ()
    assert pools[1][1]["host"] == "db"


@pytest.mark.asyncio
async def test_postgres_backend_query_options_and_delete_paths():
    pool = NoExecManyPool()
    backend = PostgreSQLBackend(pool=pool)
    await backend.initialize()
    pool.connection.calls.clear()
    await backend.insert_many("traces", [])
    assert pool.connection.calls == []

    await backend.insert_many("traces", [{"span_id": "s1"}, {"id": "known", "name": "x"}])
    assert "INSERT" in pool.connection.calls[-1][0]

    with pytest.raises(ValueError, match="limit must be non-negative"):
        await backend.find_many("traces", limit=-1)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        await backend.find_many("traces", offset=-1)

    await backend.find_many(
        "traces",
        sort=[("name", "asc"), ["meta.duration_ms", "DESC"]],
        limit=1,
        offset=2,
    )
    sql = pool.connection.calls[-1][0]
    assert "ORDER BY data->>$1 ASC" in sql
    assert "data#>>$2::text[] DESC" in sql
    assert "LIMIT $3" in sql
    assert "OFFSET $4" in sql

    assert await backend.count_documents("traces", {"name": "x"}) == 2
    assert await backend.delete_many("traces", {"name": "x"}, limit=0) == 0
    assert await backend.delete_many("traces", {"name": "x"}, limit=1) == 1
    assert await backend.delete_many("traces", {"name": "x"}) == 0

    int_pool = IntResultPgPool()
    int_backend = PostgreSQLBackend(pool=int_pool)
    await int_backend.initialize()
    assert await int_backend.delete_many("traces", {"name": "x"}) == 5

    await backend.create_index("traces", ["name", ("meta.value", 1)], unique=True)
    assert "CREATE UNIQUE INDEX" in pool.connection.calls[-1][0]
    with pytest.raises(ValueError, match="invalid index field"):
        await backend.create_index("traces", "bad-field")

    backend.pool = BrokenPgPool()
    assert (await backend.health_check())["status"] == "unavailable"
    assert (await PostgreSQLBackend().health_check())["status"] == "unavailable"


@pytest.mark.asyncio
async def test_clickhouse_backend_helpers_and_driver_initialization():
    assert clickhouse_scalar({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert clickhouse_scalar([1, 2]) == "[1, 2]"
    assert clickhouse_scalar(True) == "true"
    assert clickhouse_scalar(False) == "false"
    assert clickhouse_scalar(None) is None
    assert clickhouse_scalar(7) == "7"

    backend = ClickHouseBackend(url="http://clickhouse:8123/analytics", client=FakeCHClient())
    assert backend.storage_detail == "clickhouse:8123"
    assert ClickHouseBackend(host="ch").storage_detail == "ch"
    assert backend._field_expression("meta.name") == "JSONExtractString(data, 'meta', 'name')"
    with pytest.raises(ValueError, match="invalid filter field"):
        backend._field_expression("bad-field")
    assert backend._row_data({"data": {"x": 1}}) == {"x": 1}
    assert backend._row_data(("ignored", json.dumps({"x": 2})), ["other", "data"]) == {"x": 2}
    assert backend._row_data((None,)) == {}

    where_sql, params = backend._where(
        {
            "$skip": True,
            "meta.name": {"$exists": True},
            "status": {"$ne": "ok"},
            "cost": {"$gte": 2},
            "tags": {"$in": ["a"]},
        }
    )
    assert "JSONHas(data, {p0:String}) = 1" in where_sql
    assert "toFloat64OrNull(JSONExtractString(data, 'cost')) >= {p2:Float64}" in where_sql
    assert "!= {p1:String}" in where_sql
    assert "IN ({p3:String})" in where_sql
    assert params["p0"] == "meta.name"
    assert params["p2"] == 2

    created: list[dict] = []

    class FakeDriver:
        @staticmethod
        def get_client(**kwargs):
            created.append(kwargs)
            return FakeCHClient()

    driver_backend = ClickHouseBackend(url="http://clickhouse:8123/analytics")
    with patch.object(driver_backend, "_load_driver", return_value=FakeDriver):
        await driver_backend.initialize()
    assert created[0]["url"] == "http://clickhouse:8123/analytics"


@pytest.mark.asyncio
async def test_clickhouse_backend_query_options_and_delete_paths():
    client = FakeCHClient()
    backend = ClickHouseBackend(client=client)
    await backend.initialize()
    await backend.insert_many("traces", [])
    assert "INSERT" not in " ".join(sql for sql, _ in client.calls)

    await backend.insert_many(
        "traces",
        [
            {"span_id": "s1", "start_time": "2026-01-01T00:00:00Z"},
            {"id": "known", "created_at": "2025-01-01T00:00:00Z"},
        ],
    )
    assert client.rows[0]["id"] == "s1"
    assert client.rows[0]["created_at"] == "2026-01-01T00:00:00Z"
    assert "T00:00:00" in client.rows[1]["created_at"]

    with pytest.raises(ValueError, match="limit must be non-negative"):
        await backend.find_many("traces", limit=-1)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        await backend.find_many("traces", offset=-1)

    await backend.find_many("traces", sort=["name", ("meta.value", "DESC")], limit=1, offset=2)
    sql = client.calls[-1][0]
    assert "ORDER BY JSONExtractString(data, 'name') DESC" in sql
    assert "JSONExtractString(data, 'meta', 'value') DESC" in sql
    assert "LIMIT {limit_value:UInt64}" in sql
    assert "OFFSET {offset_value:UInt64}" in sql

    assert await backend.count_documents("traces", {"name": "x"}) == 2
    assert await backend.delete_many("traces", {"name": "x"}, limit=0) == 0
    assert await backend.delete_many("traces", {"id": "s1"}, limit=1) == 1
    assert await backend.delete_many("traces", {"id": "known"}) == 0

    await backend.create_index("traces", [("name", 1), "meta.value"], unique=True)
    assert "ADD INDEX IF NOT EXISTS" in client.calls[-1][0]
    with pytest.raises(ValueError, match="invalid index field"):
        await backend.create_index("traces", "bad-field")

    broken = ClickHouseBackend(client=BrokenCHClient())
    await broken.initialize()
    assert (await broken.health_check())["status"] == "unavailable"
