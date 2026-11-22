"""In-memory storage backend for tests and lightweight local use."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from tracekit.core.storage import TraceStorage

# Sentinel distinguishes an absent field from an explicitly stored None.
_MISSING = object()
_TIMESTAMP_FIELDS = frozenset({"start_time", "end_time", "created_at", "updated_at"})
# Missing/blank timestamps use datetime.max so ascending sorts place them after real times.
_UNSTARTED_SORT_KEY = (3, datetime.max.replace(tzinfo=timezone.utc))


class MemoryBackend(TraceStorage):
    def __init__(self, initial: dict[str, list[dict]] | None = None) -> None:
        self._collections = deepcopy(initial) if initial is not None else {}
        self._connected = False

    async def initialize(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def insert_one(self, collection: str, document: dict[str, Any]) -> None:
        # Upsert by the collection's natural id; documents without one are appended.
        documents = self._collections.setdefault(collection, [])
        key = "trace_id" if collection == "traces" else "span_id" if collection == "spans" else "id"
        key_value = document.get(key, _MISSING)
        stored = deepcopy(document)
        if key_value is not _MISSING and key_value is not None:
            for index, existing in enumerate(documents):
                if existing.get(key, _MISSING) == key_value:
                    documents[index] = stored
                    return
        documents.append(stored)

    async def find_one(
        self,
        collection: str,
        filter: dict[str, Any],
    ) -> dict[str, Any] | None:
        matches = await self.find_many(collection, filter=filter, limit=1)
        return matches[0] if matches else None

    async def find_many(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        sort: list[Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        documents = [
            document
            for document in self._collections.get(collection, [])
            if self._matches(document, filter or {})
        ]
        # Stable sorts applied in reverse field order give correct multi-key ordering.
        for field, descending in reversed(self._sort_fields(sort)):
            documents.sort(
                key=lambda document: self._sort_key(document.get(field), field),
                reverse=descending,
            )
        if offset > 0:
            documents = documents[offset:]
        if limit is not None and limit >= 0:
            documents = documents[:limit]
        # Return copies so callers cannot mutate stored state through results.
        return deepcopy(documents)

    async def count_documents(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
    ) -> int:
        return sum(
            1
            for document in self._collections.get(collection, [])
            if self._matches(document, filter or {})
        )

    # Alias kept for callers using the shorter storage API.
    async def count(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
    ) -> int:
        return await self.count_documents(collection, filter)

    async def create_index(
        self,
        collection: str,
        key_or_list: str | list[Any],
        unique: bool = False,
    ) -> None:
        # No-op: dict lookups need no index in the in-memory backend.
        return None

    async def update_one(
        self,
        collection: str,
        filter: dict[str, Any],
        update: dict[str, Any],
    ) -> int:
        for document in self._collections.get(collection, []):
            if self._matches(document, filter):
                document.update(deepcopy(update))
                return 1
        return 0

    async def delete_one(self, collection: str, filter: dict[str, Any]) -> int:
        documents = self._collections.get(collection, [])
        for index, document in enumerate(documents):
            if self._matches(document, filter):
                del documents[index]
                return 1
        return 0

    async def delete_many(
        self, collection: str, filter: dict[str, Any], limit: int | None = None
    ) -> int:
        documents = self._collections.get(collection, [])
        indexes = [
            index
            for index, document in enumerate(documents)
            if self._matches(document, filter)
        ]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            indexes = indexes[:limit]
        # Delete from the end so earlier indices remain valid.
        for index in reversed(indexes):
            del documents[index]
        return len(indexes)

    def get_stats(self) -> dict[str, Any]:
        collection_counts = {
            name: len(documents)
            for name, documents in self._collections.items()
        }
        return {
            "storage_type": self.storage_type,
            "is_connected": self.is_connected,
            "collections": collection_counts,
            "total_documents": sum(collection_counts.values()),
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def storage_type(self) -> str:
        return "memory"

    @property
    def storage_detail(self) -> str:
        return "in-memory"

    @classmethod
    def _matches(cls, document: dict[str, Any], filter: dict[str, Any]) -> bool:
        for field, expected in filter.items():
            # Each $or branch is a subfilter; any match satisfies the clause.
            if field == "$or":
                if not isinstance(expected, list) or not any(
                    cls._matches(document, subfilter)
                    for subfilter in expected
                    if isinstance(subfilter, dict)
                ):
                    return False
                continue

            actual = document.get(field, _MISSING)
            if isinstance(expected, dict) and any(
                isinstance(operator, str) and operator.startswith("$")
                for operator in expected
            ):
                if not all(
                    cls._matches_operator(actual, operator, value)
                    for operator, value in expected.items()
                ):
                    return False
            elif actual is _MISSING or actual != expected:
                return False
        return True

    @staticmethod
    def _matches_operator(actual: Any, operator: str, expected: Any) -> bool:
        # Mongo-style $ne also matches documents where the field is absent.
        if operator == "$ne":
            return actual is _MISSING or actual != expected
        if operator == "$in":
            return actual is not _MISSING and actual in expected
        if actual is _MISSING:
            return False
        try:
            if operator == "$gte":
                return actual >= expected
            if operator == "$lte":
                return actual <= expected
            if operator == "$gt":
                return actual > expected
            if operator == "$lt":
                return actual < expected
        except TypeError:
            return False
        return False

    @staticmethod
    def _sort_fields(sort: list[Any] | None) -> list[tuple[str, bool]]:
        fields: list[tuple[str, bool]] = []
        for item in sort or []:
            if isinstance(item, str):
                fields.append((item, False))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                field, direction = item
                fields.append((field, direction == -1))
        return fields

    @staticmethod
    # First tuple element groups by value kind so mixed types sort deterministically.
    def _sort_key(value: Any, field: str | None = None) -> tuple[int, Any]:
        timestamp_field = field in _TIMESTAMP_FIELDS
        if value is _MISSING or value is None:
            return _UNSTARTED_SORT_KEY if timestamp_field else (0, "")
        if timestamp_field and isinstance(value, str):
            text = value.strip()
            if not text:
                return _UNSTARTED_SORT_KEY
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                pass
            else:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return (3, parsed.astimezone(timezone.utc))
        if isinstance(value, bool):
            return (1, int(value))
        if isinstance(value, (int, float)):
            return (1, value)
        if isinstance(value, str):
            return (2, value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return (3, value.astimezone(timezone.utc))
        return (4, repr(value))
