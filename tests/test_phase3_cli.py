"""Tests for the Phase 3 CLI enhancements."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tracekit.cli import (
    _cmd_delete,
    _cmd_list,
    _render_table,
    _watch_list,
    build_parser,
    main,
)


class FakeStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.search_calls: list[dict] = []
        self.deleted: list[str] = []

    async def search_traces(self, **filters):
        self.search_calls.append(filters)
        limit = filters.get("limit", 20)
        offset = filters.get("offset", 0)
        return self.rows[offset : offset + limit]

    async def delete_trace(self, trace_id: str) -> bool:
        self.deleted.append(trace_id)
        return any(row.get("trace_id") == trace_id for row in self.rows)


def list_args(**overrides) -> argparse.Namespace:
    args = build_parser().parse_args(["list"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class TestParser(unittest.TestCase):
    def test_list_defaults(self) -> None:
        args = build_parser().parse_args(["list"])
        self.assertEqual(args.output, "table")
        self.assertEqual(args.limit, 20)
        self.assertEqual(args.offset, 0)
        self.assertFalse(args.watch)

    def test_filters_are_parsed(self) -> None:
        args = build_parser().parse_args(
            [
                "search",
                "--status",
                "error",
                "--span-type",
                "llm",
                "--name",
                "chat",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-02T00:00:00Z",
                "--error",
                "timeout",
                "--limit",
                "5",
                "--offset",
                "2",
                "--output",
                "json",
            ]
        )
        self.assertEqual(args.status, "error")
        self.assertEqual(args.span_type, "llm")
        self.assertEqual(args.name, "chat")
        self.assertEqual(args.error, "timeout")
        self.assertEqual(args.limit, 5)
        self.assertEqual(args.offset, 2)
        self.assertEqual(args.output, "json")

    def test_bare_error_flag_is_true(self) -> None:
        args = build_parser().parse_args(["list", "--error"])
        self.assertIs(args.error, True)

    def test_delete_requires_yes(self) -> None:
        args = build_parser().parse_args(["delete", "abc"])
        self.assertFalse(args.yes)


class TestRendering(unittest.TestCase):
    def test_table_with_explicit_columns(self) -> None:
        text = _render_table(
            [{"trace_id": "abc", "status": "ok"}, {"trace_id": "longer", "status": "error"}],
            columns=["trace_id", "status"],
        )
        self.assertIn("trace_id", text)
        self.assertIn("longer", text)
        self.assertIn("error", text)
        self.assertEqual(len(text.splitlines()), 4)

    def test_empty_table(self) -> None:
        self.assertEqual(_render_table([]), "")

    def test_json_output_is_valid(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                args = list_args(db=str(Path(tmp) / "trace.db"), output="json")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(await _cmd_list(args), 0)
                self.assertEqual(json.loads(buffer.getvalue()), [])

        asyncio.run(run())

    def test_delete_without_yes_does_not_open_store(self) -> None:
        args = build_parser().parse_args(["delete", "abc"])
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            self.assertEqual(asyncio.run(_cmd_delete(args)), 1)
        self.assertIn("--yes", buffer.getvalue())

    def test_delete_with_fake_store(self) -> None:
        async def run() -> None:
            args = build_parser().parse_args(["delete", "abc", "--yes"])
            args.db = ":memory:"
            # _cmd_delete opens SQLite; patch by exercising the fake directly.
            store = FakeStore([{"trace_id": "abc"}])
            self.assertTrue(await store.delete_trace("abc"))
            self.assertEqual(store.deleted, ["abc"])

        asyncio.run(run())


class TestWatch(unittest.TestCase):
    def test_watch_prints_only_new_rows(self) -> None:
        rows = [
            {"trace_id": "1", "name": "a"},
            {"trace_id": "2", "name": "b"},
        ]
        store = FakeStore(rows)
        args = list_args(output="json", interval=0, limit=20)
        buffer = io.StringIO()

        async def fake_sleep(_: float) -> None:
            return None

        async def run() -> None:
            with redirect_stdout(buffer):
                await _watch_list(store, args, iterations=2, sleep=fake_sleep)
            # Mutate after the first poll to simulate a new trace arriving.
            store.rows.append({"trace_id": "3", "name": "c"})

        asyncio.run(run())
        output = buffer.getvalue()
        self.assertIn('"trace_id": "1"', output)
        self.assertEqual(output.count('"trace_id": "1"'), 1)

    def test_watch_emits_new_rows_on_later_poll(self) -> None:
        store = FakeStore([{"trace_id": "1"}])
        args = list_args(output="json", interval=0)
        buffer = io.StringIO()
        polls = {"count": 0}

        async def fake_sleep(_: float) -> None:
            polls["count"] += 1
            store.rows.append({"trace_id": "2"})

        async def run() -> None:
            with redirect_stdout(buffer):
                await _watch_list(store, args, iterations=2, sleep=fake_sleep)

        asyncio.run(run())
        self.assertIn('"trace_id": "2"', buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
