"""Multi-tenancy, environment tagging, and project-level isolation for Tracekit."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from tracekit.core.auth import APIKeyRecord, AuthorizationError
from tracekit.core.storage import StorageBackend


class Environment:
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


ENV_ALIASES: dict[str, str] = {
    "dev": Environment.DEVELOPMENT,
    "devel": Environment.DEVELOPMENT,
    "development": Environment.DEVELOPMENT,
    "local": Environment.DEVELOPMENT,
    "stage": Environment.STAGING,
    "stg": Environment.STAGING,
    "staging": Environment.STAGING,
    "prod": Environment.PRODUCTION,
    "prd": Environment.PRODUCTION,
    "production": Environment.PRODUCTION,
    "test": Environment.TEST,
    "testing": Environment.TEST,
    "qa": "qa",
    "ci": Environment.TEST,
}


def normalize_environment(env: str | None, default: str = Environment.DEVELOPMENT) -> str:
    """Normalize environment string, resolving common aliases while preserving custom environments."""
    if not env:
        return default
    cleaned = str(env).strip().lower()
    return ENV_ALIASES.get(cleaned, cleaned)


def get_current_environment(default: str = Environment.DEVELOPMENT) -> str:
    """Resolve current environment from TRACEKIT_ENVIRONMENT or fallback variables."""
    # Tracekit-specific names win over generic ENV/ENVIRONMENT to avoid unrelated host variables.
    env = (
        os.getenv("TRACEKIT_ENVIRONMENT")
        or os.getenv("TRACEKIT_ENV")
        or os.getenv("ENVIRONMENT")
        or os.getenv("ENV")
    )
    return normalize_environment(env, default=default)


def tag_environment(
    record: dict[str, Any] | Any,
    environment: str | None = None,
) -> Any:
    """Tag a trace, span, or document with an environment name."""
    env = normalize_environment(environment or get_current_environment())
    # Environment is duplicated into attributes/metadata so tag-based filters and span records agree.
    if isinstance(record, dict):
        record["environment"] = env
        attrs = record.setdefault("attributes", {})
        if isinstance(attrs, dict):
            attrs["environment"] = env
    elif hasattr(record, "attributes") and isinstance(record.attributes, dict):
        record.attributes["environment"] = env
        if hasattr(record, "metadata") and isinstance(record.metadata, dict):
            record.metadata["environment"] = env
    return record


@dataclass
class TenantContext:
    """Context representing an authenticated tenant or API key scope."""

    api_key_id: str | None = None
    project_id: str | None = None
    allowed_projects: set[str] = field(default_factory=set)
    environment: str | None = None
    is_admin: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def can_access_project(self, target_project_id: str) -> bool:
        """Check whether this context is authorized to access target_project_id."""
        if not target_project_id:
            return False
        # Admin or wildcard scopes bypass checks; otherwise only the key's project and explicit grants count.
        if self.is_admin or "*" in self.allowed_projects:
            return True
        if self.project_id and self.project_id == target_project_id:
            return True
        return target_project_id in self.allowed_projects


def create_tenant_context_from_key(key_record: APIKeyRecord | dict[str, Any]) -> TenantContext:
    """Create a TenantContext from an APIKeyRecord or dictionary."""
    if isinstance(key_record, APIKeyRecord):
        meta = dict(key_record.metadata)
        key_id = key_record.key_id
        project_id = key_record.project_id
        env = key_record.environment
    else:
        raw_meta = key_record.get("metadata")
        if isinstance(raw_meta, str):
            try:
                meta = json.loads(raw_meta)
            except Exception:
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = dict(raw_meta)
        else:
            meta = {}
        key_id = key_record.get("key")
        project_id = key_record.get("project_id")
        env = key_record.get("environment")

    is_admin = bool(meta.get("is_admin", False) or meta.get("admin", False))
    raw_allowed = meta.get("allowed_projects", [])
    allowed = set(raw_allowed) if isinstance(raw_allowed, (list, tuple, set)) else set()
    # A key's own project is implicitly allowed even when metadata omits allowed_projects.
    if project_id:
        allowed.add(project_id)

    return TenantContext(
        api_key_id=key_id,
        project_id=project_id,
        allowed_projects=allowed,
        environment=env,
        is_admin=is_admin,
        metadata=meta,
    )


def _to_tenant_context(context: TenantContext | APIKeyRecord | dict[str, Any] | str) -> TenantContext:
    if isinstance(context, TenantContext):
        return context
    if isinstance(context, str):
        # Direct project_id passed as context
        return TenantContext(project_id=context, allowed_projects={context})
    return create_tenant_context_from_key(context)


def authorize_project_access(
    context: TenantContext | APIKeyRecord | dict[str, Any] | str,
    project_id: str,
) -> None:
    """Enforce that the tenant context is authorized to access project_id.

    Raises:
        AuthorizationError: If project_id is empty or access is unauthorized.
    """
    if not project_id:
        raise AuthorizationError("project_id must not be empty")

    tenant_ctx = _to_tenant_context(context)
    if not tenant_ctx.can_access_project(project_id):
        raise AuthorizationError(
            f"Unauthorized: access denied to project '{project_id}'"
        )


def authorize_trace_access(
    context: TenantContext | APIKeyRecord | dict[str, Any] | str,
    trace: dict[str, Any],
) -> None:
    """Enforce that the tenant context is authorized to access the given trace.

    Raises:
        AuthorizationError: If trace belongs to an unauthorized project.
    """
    trace_project_id = trace.get("project_id")
    tenant_ctx = _to_tenant_context(context)

    if not trace_project_id:
        # Trace is unassigned to any project
        # Scoped contexts cannot read unassigned traces because ownership cannot be proven.
        if not tenant_ctx.is_admin and tenant_ctx.project_id:
            raise AuthorizationError(
                "Cross-project trace access denied: trace has no project assignment"
            )
        return

    if not tenant_ctx.can_access_project(trace_project_id):
        raise AuthorizationError(
            f"Cross-project trace access denied: authorized for '{tenant_ctx.project_id}', "
            f"cannot access trace in project '{trace_project_id}'"
        )


class TenantScopedStore:
    """Storage wrapper enforcing project-level isolation and environment tagging."""

    def __init__(
        self,
        backend: StorageBackend,
        context: TenantContext | APIKeyRecord | dict[str, Any] | str,
    ) -> None:
        self.backend = backend
        self.context = _to_tenant_context(context)

    async def insert_trace(self, trace_doc: dict[str, Any]) -> None:
        """Insert trace ensuring project association and tenant authorization."""
        doc = dict(trace_doc)
        trace_proj = doc.get("project_id")

        if trace_proj is None:
            # Bind trace to tenant's project if context specifies one
            if self.context.project_id:
                doc["project_id"] = self.context.project_id
        else:
            authorize_project_access(self.context, trace_proj)

        if "environment" not in doc or not doc["environment"]:
            # Key context wins over process environment so writes stay consistent with the caller's scope.
            env = self.context.environment or get_current_environment()
            tag_environment(doc, env)

        await self.backend.insert_one("traces", doc)

    async def find_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Retrieve trace and verify cross-project authorization."""
        # The unfiltered lookup is safe only because ownership is checked on the document before it is returned.
        trace = await self.backend.find_one("traces", {"trace_id": trace_id})
        if trace is None:
            return None
        authorize_trace_access(self.context, trace)
        return trace

    async def find_traces(
        self,
        filter: dict[str, Any] | None = None,
        sort: list[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find traces scoped strictly to the authorized project(s)."""
        query_filter = dict(filter or {})
        # Caller-supplied project_id is overwritten so a forged filter cannot widen access.
        if not self.context.is_admin and self.context.project_id:
            query_filter["project_id"] = self.context.project_id
        return await self.backend.find_many("traces", filter=query_filter, sort=sort, limit=limit)

    async def count_traces(self, filter: dict[str, Any] | None = None) -> int:
        """Count traces scoped strictly to the authorized project(s)."""
        query_filter = dict(filter or {})
        # Same enforcement as find_traces: scoped tenants cannot count another project's traces.
        if not self.context.is_admin and self.context.project_id:
            query_filter["project_id"] = self.context.project_id
        return await self.backend.count_documents("traces", filter=query_filter)

    async def delete_trace(self, trace_id: str) -> bool:
        """Delete trace after validating project ownership."""
        trace = await self.backend.find_one("traces", {"trace_id": trace_id})
        if trace is None:
            return False

        authorize_trace_access(self.context, trace)

        if hasattr(self.backend, "delete_one"):
            deleted = await self.backend.delete_one("traces", {"trace_id": trace_id})
            # Spans are removed with their trace to avoid orphaned children.
            await self.backend.delete_one("spans", {"trace_id": trace_id})
            return deleted > 0
        return False
