"""Developer-facing retrieval and management APIs."""

# Thin facade over core modules: SDK callers get a stable import path while logic stays in one place.
# Underscore aliases avoid shadowing the public wrappers defined below.
from __future__ import annotations

from datetime import datetime
from typing import Any

from tracekit.core.auth import (
    APIKeyRecord,
)
from tracekit.core.auth import (
    delete_api_key as _delete_api_key,
)
from tracekit.core.auth import (
    generate_api_key as _generate_api_key,
)
from tracekit.core.auth import (
    get_api_key as _get_api_key,
)
from tracekit.core.auth import (
    list_api_keys as _list_api_keys,
)
from tracekit.core.auth import (
    revoke_api_key as _revoke_api_key,
)
from tracekit.core.auth import (
    validate_api_key as _validate_api_key,
)
from tracekit.core.db import TraceStore
from tracekit.core.projects import (
    Project,
)
from tracekit.core.projects import (
    archive_project as _archive_project,
)
from tracekit.core.projects import (
    create_project as _create_project,
)
from tracekit.core.projects import (
    delete_project as _delete_project,
)
from tracekit.core.projects import (
    get_project as _get_project,
)
from tracekit.core.projects import (
    get_project_by_name as _get_project_by_name,
)
from tracekit.core.projects import (
    list_projects as _list_projects,
)
from tracekit.core.projects import (
    unarchive_project as _unarchive_project,
)
from tracekit.core.projects import (
    update_project as _update_project,
)
from tracekit.core.storage import StorageBackend
from tracekit.core.tenant import TenantContext, TenantScopedStore
from tracekit.core.tracer import Tracer


async def get_trace(store: TraceStore, trace_id: str) -> dict[str, Any] | None:
    return await store.get_trace(trace_id)


async def list_traces(
    store: TraceStore, limit: int = 100, **filters: Any
) -> list[dict[str, Any]]:
    return await store.list_traces(limit=limit, **filters)


async def get_trace_spans(store: TraceStore, trace_id: str) -> list[dict[str, Any]]:
    return await store.get_spans_for_trace(trace_id)


async def replay_trace(store: TraceStore, trace_id: str) -> dict[str, Any] | None:
    return await store.get_trace_replay(trace_id)


async def search_traces(store: TraceStore, **filters: Any) -> list[dict[str, Any]]:
    return await store.search_traces(**filters)


async def delete_trace(store: TraceStore, trace_id: str) -> bool:
    return await store.delete_trace(trace_id)


async def delete_spans_older_than(store: TraceStore, days: float) -> int:
    return await store.delete_spans_older_than(days)


# Local replay rebuilds the tree from the in-memory tracer's spans; replay_trace reads the store.
def replay_trace_local(tracer: Tracer, trace_id: str) -> dict[str, Any] | None:
    return tracer.build_trace_replay(trace_id)


async def create_project(
    backend: StorageBackend,
    name: str,
    project_id: str | None = None,
    description: str = "",
    metadata: dict[str, Any] | None = None,
) -> Project:
    return await _create_project(
        backend,
        name,
        project_id=project_id,
        description=description,
        metadata=metadata,
    )


async def get_project(backend: StorageBackend, project_id: str) -> Project | None:
    return await _get_project(backend, project_id)


async def get_project_by_name(backend: StorageBackend, name: str) -> Project | None:
    return await _get_project_by_name(backend, name)


async def list_projects(
    backend: StorageBackend,
    include_archived: bool = False,
    limit: int | None = None,
) -> list[Project]:
    return await _list_projects(
        backend,
        include_archived=include_archived,
        limit=limit,
    )


async def update_project(
    backend: StorageBackend,
    project_id: str,
    name: str | None = None,
    description: str | None = None,
    archived: bool | None = None,
) -> Project | None:
    return await _update_project(
        backend,
        project_id,
        name=name,
        description=description,
        archived=archived,
    )


async def archive_project(backend: StorageBackend, project_id: str) -> bool:
    return await _archive_project(backend, project_id)


async def unarchive_project(backend: StorageBackend, project_id: str) -> bool:
    return await _unarchive_project(backend, project_id)


async def delete_project(
    backend: StorageBackend,
    project_id: str,
    cascade_traces: bool = False,
) -> bool:
    return await _delete_project(
        backend,
        project_id,
        cascade_traces=cascade_traces,
    )


async def generate_api_key(
    backend: StorageBackend,
    name: str = "",
    project_id: str | None = None,
    environment: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | str | None = None,
    prefix: str = "tk",
) -> tuple[str, APIKeyRecord]:
    return await _generate_api_key(
        backend,
        name=name,
        project_id=project_id,
        environment=environment,
        metadata=metadata,
        expires_at=expires_at,
        prefix=prefix,
    )


async def validate_api_key(backend: StorageBackend, raw_key: str) -> APIKeyRecord:
    return await _validate_api_key(backend, raw_key)


async def get_api_key(backend: StorageBackend, key_id: str) -> APIKeyRecord | None:
    return await _get_api_key(backend, key_id)


async def list_api_keys(
    backend: StorageBackend,
    project_id: str | None = None,
    environment: str | None = None,
    include_revoked: bool = False,
) -> list[APIKeyRecord]:
    return await _list_api_keys(
        backend,
        project_id=project_id,
        environment=environment,
        include_revoked=include_revoked,
    )


async def revoke_api_key(backend: StorageBackend, key_id: str) -> bool:
    return await _revoke_api_key(backend, key_id)


async def delete_api_key(backend: StorageBackend, key_id: str) -> bool:
    return await _delete_api_key(backend, key_id)


# Tenant helpers force queries through TenantScopedStore so project isolation cannot be bypassed.
async def search_traces_for_tenant(
    backend: StorageBackend,
    context: TenantContext,
    **filters: Any,
) -> list[dict[str, Any]]:
    limit = filters.pop("limit", None)
    sort = filters.pop("sort", None)
    store = TenantScopedStore(backend, context)
    return await store.find_traces(filter=filters or None, sort=sort, limit=limit)


async def delete_trace_for_tenant(
    backend: StorageBackend,
    context: TenantContext,
    trace_id: str,
) -> bool:
    store = TenantScopedStore(backend, context)
    return await store.delete_trace(trace_id)
