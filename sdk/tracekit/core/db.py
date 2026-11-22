"""Persistence helpers/facade for Tracekit core.

This replaces broken imports from the previous codebase and provides a small
async-friendly wrapper around a StorageBackend.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from tracekit.core.span import normalize_span_type
from tracekit.core.storage import StorageBackend


class TraceStore:
    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend

    async def initialize(self) -> None:
        await self.backend.initialize()

    async def close(self) -> None:
        await self.backend.close()

    async def save_trace(self, trace_data: dict[str, Any]) -> None:
        await self.backend.insert_one("traces", trace_data)

    async def save_span(self, span_data: dict[str, Any]) -> None:
        await self.backend.insert_one("spans", span_data)

    async def save_spans(self, spans: list[dict[str, Any]]) -> None:
        if not spans:
            return
        # Prefer bulk insert when available; fall back for minimal backends.
        insert_many = getattr(self.backend, "insert_many", None)
        if insert_many is not None:
            await insert_many("spans", spans)
            return
        for span in spans:
            await self.save_span(span)

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return await self.backend.find_one("traces", {"trace_id": trace_id})

    async def list_traces(
        self, limit: int = 100, **filters: Any
    ) -> list[dict[str, Any]]:
        if filters:
            filters.setdefault("limit", limit)
            return await self.search_traces(**filters)
        return await self.backend.find_many(
            "traces", sort=[("start_time", -1)], limit=limit
        )

    def _normalize_time(self, value: datetime | str | None) -> str | None:
        """Normalize datetime or ISO string, kept for API compatibility."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    def _parse_time(self, value: Any) -> datetime | None:
        """Parse stored/query timestamps into comparable aware datetimes."""
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
        else:
            return None
        # Treat naive timestamps as UTC so cross-source comparisons are stable.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    async def search_traces(
        self,
        *,
        status: str | None = None,
        span_type: str | None = None,
        name: str | None = None,
        name_contains: str | None = None,
        q: str | None = None,
        trace_id: str | None = None,
        model_name: str | None = None,
        error: bool | str | None = None,
        error_type: str | None = None,
        project_id: str | None = None,
        environment: str | None = None,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        min_latency_ms: float | None = None,
        max_latency_ms: float | None = None,
        latency_min: float | None = None,
        latency_max: float | None = None,
        sort_by: str = "start_time",
        sort_desc: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filter_dict: dict[str, Any] = {}
        for key, value in (
            ("status", status),
            ("name", name),
            ("trace_id", trace_id),
            ("model_name", model_name),
            ("project_id", project_id),
            ("environment", environment),
        ):
            if value is not None:
                filter_dict[key] = value

        # Whitelist sort fields to prevent arbitrary/backend-specific field injection.
        allowed_sort_fields = frozenset(
            {
                "start_time",
                "end_time",
                "created_at",
                "updated_at",
                "duration_ms",
                "latency_ms",
                "name",
                "status",
                "project_id",
                "environment",
                "error_type",
                "token_count",
            }
        )
        if sort_by not in allowed_sort_fields:
            raise ValueError(f"unsupported sort field: {sort_by}")
        direction = -1 if sort_desc else 1

        # Fetch full sorted set first; several filters below require in-memory evaluation.
        traces = await self.backend.find_many(
            "traces",
            filter=filter_dict,
            sort=[(sort_by, direction)],
            limit=None,
        )

        if name_contains:
            needle = name_contains.lower()
            traces = [
                trace
                for trace in traces
                if needle in str(trace.get("name") or "").lower()
            ]
        if q:
            needle = q.lower()
            traces = [
                trace
                for trace in traces
                if needle in str(trace.get("trace_id") or "").lower()
                or needle in str(trace.get("name") or "").lower()
            ]

        start_dt = self._parse_time(start_time)
        end_dt = self._parse_time(end_time)
        if start_dt is not None or end_dt is not None:
            filtered: list[dict[str, Any]] = []
            for trace in traces:
                trace_start = self._parse_time(trace.get("start_time"))
                if trace_start is None:
                    continue
                if start_dt is not None and trace_start < start_dt:
                    continue
                if end_dt is not None and trace_start > end_dt:
                    continue
                filtered.append(trace)
            traces = filtered

        low = min_latency_ms if min_latency_ms is not None else latency_min
        high = max_latency_ms if max_latency_ms is not None else latency_max
        if low is not None or high is not None:
            latency_filtered: list[dict[str, Any]] = []
            for trace in traces:
                latency = trace.get("latency_ms")
                if latency is None:
                    latency = trace.get("duration_ms")
                if latency is None:
                    latency = trace.get("latency")
                try:
                    latency_value = float(latency)
                except (TypeError, ValueError):
                    continue
                if low is not None and latency_value < low:
                    continue
                if high is not None and latency_value > high:
                    continue
                latency_filtered.append(trace)
            traces = latency_filtered

        if error is not None or error_type is not None:
            error_filtered: list[dict[str, Any]] = []
            for trace in traces:
                error_value = trace.get("error")
                error_text = "" if error_value is None else str(error_value).strip()
                if error is True and not error_text:
                    continue
                if error is False and error_text:
                    continue
                if isinstance(error, str) and error.lower() not in error_text.lower():
                    continue
                if error_type is not None:
                    stored_type = trace.get("error_type")
                    if not stored_type and isinstance(error_value, dict):
                        stored_type = error_value.get("type")
                    if not stored_type and error_text:
                        stored_type = "error"
                    if str(stored_type or "").lower() != error_type.lower():
                        continue
                error_filtered.append(trace)
            traces = error_filtered

        if span_type is not None:
            normalized_span_type = normalize_span_type(span_type).value
            matching_trace_ids: set[str] = set()
            trace_ids = [t["trace_id"] for t in traces if "trace_id" in t]
            # span_type lives on spans, so traces are filtered by scanning their span sets.
            for trace_id in trace_ids:
                spans = await self.backend.find_many(
                    "spans",
                    filter={"trace_id": trace_id},
                    sort=[("start_time", 1)],
                )
                for span in spans:
                    span_type_value = span.get("span_type") or span.get("type")
                    if span_type_value is None:
                        continue
                    try:
                        stored_type = normalize_span_type(span_type_value).value
                    except (TypeError, ValueError):
                        continue
                    if stored_type == normalized_span_type:
                        matching_trace_ids.add(trace_id)
                        break
            traces = [t for t in traces if t.get("trace_id") in matching_trace_ids]

        if offset > 0:
            traces = traces[offset:]
        if limit >= 0:
            traces = traces[:limit]
        return traces

    async def search_traces_page(self, **filters: Any) -> dict[str, Any]:
        page_filters = dict(filters)
        page_filters["limit"] = -1
        page_filters["offset"] = 0
        traces = await self.search_traces(**page_filters)
        total = len(traces)
        limit = int(filters.get("limit", 50))
        offset = max(0, int(filters.get("offset", 0)))
        if limit >= 0:
            items = traces[offset : offset + limit]
        else:
            items = traces[offset:]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort_by": filters.get("sort_by", "start_time"),
            "sort_desc": bool(filters.get("sort_desc", True)),
        }

    async def delete_spans_older_than(self, days: float) -> int:
        if days < 0:
            raise ValueError("days must be non-negative")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filter_dict = {"start_time": {"$lt": self._normalize_time(cutoff)}}
        delete_many = getattr(self.backend, "delete_many", None)
        if delete_many is not None:
            return int(await delete_many("spans", filter_dict))
        spans = await self.backend.find_many("spans", filter=filter_dict)
        deleted = 0
        for span in spans:
            span_id = span.get("span_id")
            if span_id is None:
                continue
            deleted += await self.backend.delete_one("spans", {"span_id": span_id})
        return deleted

    async def delete_trace(self, trace_id: str) -> bool:
        # Prefer backend-native atomic delete; generic fallback may leave partial state on failure.
        delete_trace = getattr(self.backend, "delete_trace", None)
        if delete_trace is not None:
            return int(await delete_trace(trace_id)) > 0
        if not hasattr(self.backend, "delete_one"):
            raise NotImplementedError(
                f"Backend {type(self.backend).__name__} does not support delete_trace"
            )
        trace_deleted = await self.backend.delete_one("traces", {"trace_id": trace_id})
        await self.backend.delete_one("spans", {"trace_id": trace_id})
        return trace_deleted > 0

    async def get_spans_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return await self.backend.find_many("spans", filter={"trace_id": trace_id}, sort=[("start_time", 1)])

    async def get_trace_replay(self, trace_id: str) -> dict[str, Any] | None:
        trace = await self.get_trace(trace_id)
        if trace is None:
            return None
        spans = await self.get_spans_for_trace(trace_id)
        nodes = []
        by_id: dict[str, dict[str, Any]] = {}
        for s in spans:
            node = {**s, "children": []}
            nodes.append(node)
            sid = s.get("span_id")
            if sid:
                by_id[sid] = node
        roots: list[dict[str, Any]] = []
        for node in nodes:
            pid = node.get("parent_span_id")
            if pid and pid in by_id:
                by_id[pid]["children"].append(node)
            else:
                roots.append(node)
        return {"trace": trace, "spans": spans, "tree": roots}


def run_async(coro):
    """Run an async coroutine from sync code when no loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("run_async cannot be used while an event loop is running")
    return asyncio.run(coro)
