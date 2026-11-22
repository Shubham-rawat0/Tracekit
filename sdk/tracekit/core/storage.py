"""Storage backend interface for Tracekit."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize storage resources."""

    @abstractmethod
    async def close(self) -> None:
        """Close storage resources."""

    @abstractmethod
    async def insert_one(self, collection: str, document: dict[str, Any]) -> None:
        """Insert or replace a document in a collection."""

    async def insert_many(
        self, collection: str, documents: list[dict[str, Any]]
    ) -> None:
        # Default naive bulk insert; backends can override for atomic/batched performance.
        for document in documents:
            await self.insert_one(collection, document)

    @abstractmethod
    async def find_one(self, collection: str, filter: dict[str, Any]) -> dict[str, Any] | None:
        """Find a single document matching filter."""

    @abstractmethod
    async def find_many(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        sort: list[Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Find documents matching filter."""

    @abstractmethod
    async def count_documents(self, collection: str, filter: dict[str, Any] | None = None) -> int:
        """Count documents matching filter."""

    async def delete_many(
        self, collection: str, filter: dict[str, Any], limit: int | None = None
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    async def create_index(
        self, collection: str, key_or_list: str | list[Any], unique: bool = False
    ) -> None:
        """Create an index."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether storage is connected."""

    @property
    @abstractmethod
    def storage_type(self) -> str:
        """Storage type identifier."""

    @property
    @abstractmethod
    def storage_detail(self) -> str:
        """Storage detail (e.g. path)."""


class TraceStorage(StorageBackend, ABC):
    """Domain-oriented tracing storage interface.

    Implementations remain compatible with the generic ``StorageBackend``
    collection API while offering explicit trace/span operations.
    """

    async def save_trace(self, trace: dict[str, Any]) -> None:
        await self.insert_one("traces", trace)

    async def save_span(self, span: dict[str, Any]) -> None:
        await self.insert_one("spans", span)

    async def save_traces(self, traces: list[dict[str, Any]]) -> None:
        if traces:
            await self.insert_many("traces", traces)

    async def save_spans(self, spans: list[dict[str, Any]]) -> None:
        if spans:
            await self.insert_many("spans", spans)

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return await self.find_one("traces", {"trace_id": trace_id})

    async def get_span(self, span_id: str) -> dict[str, Any] | None:
        return await self.find_one("spans", {"span_id": span_id})

    async def search_traces(self, **filters: Any) -> list[dict[str, Any]]:
        from tracekit.core.db import TraceStore

        return await TraceStore(self).search_traces(**filters)

    async def delete_trace(self, trace_id: str) -> bool:
        deleted = await self.delete_many("traces", {"trace_id": trace_id}, limit=1)
        # Only remove spans when the trace record was actually deleted.
        if deleted:
            await self.delete_many("spans", {"trace_id": trace_id})
        return deleted > 0

    async def health_check(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.is_connected else "unavailable",
            "storage_type": self.storage_type,
            "storage_detail": self.storage_detail,
        }
