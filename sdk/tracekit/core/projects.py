"""Project management, lifecycle, trace association, and query helpers for Tracekit."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tracekit.core.storage import StorageBackend


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


@dataclass
class Project:
    """Represents a Tracekit project."""

    project_id: str
    name: str
    description: str = ""
    archived: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "archived": 1 if self.archived else 0,
            "created_at": (
                self.created_at.isoformat(timespec="microseconds")
                if self.created_at
                else _now_iso()
            ),
            "updated_at": (
                self.updated_at.isoformat(timespec="microseconds")
                if self.updated_at
                else _now_iso()
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        raw_meta = data.get("metadata")
        if isinstance(raw_meta, str):
            try:
                metadata = json.loads(raw_meta)
            except Exception:
                metadata = {}
        elif isinstance(raw_meta, dict):
            metadata = dict(raw_meta)
        else:
            metadata = {}

        return cls(
            project_id=str(data.get("project_id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "") or ""),
            archived=bool(data.get("archived", 0)),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
            metadata=metadata,
        )


async def create_project(
    backend: StorageBackend,
    name: str,
    project_id: str | None = None,
    description: str = "",
    metadata: dict[str, Any] | None = None,
) -> Project:
    """Create a new project in storage.

    Raises:
        ValueError: If name is empty or a project with the same name/id already exists.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Project name cannot be empty")
    name = name.strip()

    if project_id is None:
        project_id = f"proj_{secrets.token_hex(6)}"
    else:
        project_id = str(project_id).strip()
        if not project_id:
            raise ValueError("project_id cannot be empty string")

    # Check for name/id uniqueness
    existing_id = await backend.find_one("projects", {"project_id": project_id})
    if existing_id is not None:
        raise ValueError(f"Project with ID '{project_id}' already exists")

    existing_name = await backend.find_one("projects", {"name": name})
    if existing_name is not None:
        raise ValueError(f"Project with name '{name}' already exists")

    now = datetime.now(timezone.utc)
    project = Project(
        project_id=project_id,
        name=name,
        description=description,
        archived=False,
        created_at=now,
        updated_at=now,
        metadata=dict(metadata or {}),
    )

    await backend.insert_one("projects", project.to_dict())
    return project


async def get_project(
    backend: StorageBackend,
    project_id: str,
) -> Project | None:
    """Retrieve a project by its project_id."""
    doc = await backend.find_one("projects", {"project_id": project_id})
    if doc is None:
        return None
    return Project.from_dict(doc)


async def get_project_by_name(
    backend: StorageBackend,
    name: str,
) -> Project | None:
    """Retrieve a project by its unique name."""
    doc = await backend.find_one("projects", {"name": name})
    if doc is None:
        return None
    return Project.from_dict(doc)


async def list_projects(
    backend: StorageBackend,
    include_archived: bool = False,
    limit: int | None = None,
) -> list[Project]:
    """List projects, optionally including archived ones."""
    query_filter: dict[str, Any] = {}
    if not include_archived:
        # archived is stored as 0/1; the Python filter backs up backends that ignore the numeric query.
        query_filter["archived"] = 0

    docs = await backend.find_many(
        "projects",
        filter=query_filter if query_filter else None,
        limit=limit,
    )
    projects = [Project.from_dict(d) for d in docs]
    if not include_archived:
        projects = [p for p in projects if not p.archived]
    return projects


async def update_project(
    backend: StorageBackend,
    project_id: str,
    name: str | None = None,
    description: str | None = None,
    archived: bool | None = None,
) -> Project | None:
    """Update project fields."""
    project = await get_project(backend, project_id)
    if project is None:
        return None

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Project name cannot be empty")
        if name != project.name:
            existing_name = await backend.find_one("projects", {"name": name})
            if existing_name is not None:
                raise ValueError(f"Project with name '{name}' already exists")
            project.name = name

    if description is not None:
        project.description = description

    if archived is not None:
        project.archived = bool(archived)

    project.updated_at = datetime.now(timezone.utc)
    # Updates rewrite the whole record because the storage API has no in-place update.
    if hasattr(backend, "delete_one"):
        await backend.delete_one("projects", {"project_id": project_id})
    await backend.insert_one("projects", project.to_dict())
    return project


async def archive_project(
    backend: StorageBackend,
    project_id: str,
) -> bool:
    """Archive a project (soft delete)."""
    # Archived projects remain directly retrievable and are only hidden from list_projects by default.
    updated = await update_project(backend, project_id, archived=True)
    return updated is not None


async def unarchive_project(
    backend: StorageBackend,
    project_id: str,
) -> bool:
    """Unarchive an archived project."""
    updated = await update_project(backend, project_id, archived=False)
    return updated is not None


async def delete_project(
    backend: StorageBackend,
    project_id: str,
    cascade_traces: bool = False,
) -> bool:
    """Permanently delete a project, optionally cascading to its traces and spans."""
    if not hasattr(backend, "delete_one"):
        return False

    if cascade_traces:
        # Cascade delete traces and spans belonging to this project
        # Spans are found through their parent traces by trace_id, not queried directly by project.
        traces = await backend.find_many("traces", filter={"project_id": project_id})
        for trace in traces:
            trace_id = trace.get("trace_id")
            if trace_id:
                await backend.delete_one("traces", {"trace_id": trace_id})
                await backend.delete_one("spans", {"trace_id": trace_id})

    deleted = await backend.delete_one("projects", {"project_id": project_id})
    return deleted > 0


def associate_trace_with_project(
    trace_data: Any,
    project_id: str,
    project_name: str | None = None,
) -> Any:
    """Associate a trace dictionary or Trace instance with a project."""
    # Project identity is written to several locations because different query paths read different fields.
    if isinstance(trace_data, dict):
        trace_data["project_id"] = project_id
        if project_name is not None:
            trace_data["project_name"] = project_name
        attrs = trace_data.setdefault("attributes", {})
        if isinstance(attrs, dict):
            attrs["project_id"] = project_id
            if project_name is not None:
                attrs["project_name"] = project_name
    elif hasattr(trace_data, "attributes") and isinstance(trace_data.attributes, dict):
        trace_data.attributes["project_id"] = project_id
        if project_name is not None:
            trace_data.attributes["project_name"] = project_name
        if hasattr(trace_data, "metadata") and isinstance(trace_data.metadata, dict):
            trace_data.metadata["project_id"] = project_id
            if project_name is not None:
                trace_data.metadata["project_name"] = project_name
    return trace_data


async def find_project_traces(
    backend: StorageBackend,
    project_id: str,
    status: str | None = None,
    environment: str | None = None,
    limit: int = 50,
    sort: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Query traces scoped to a specific project."""
    # project_id is always in the filter: there is no unscoped listing path here.
    query_filter: dict[str, Any] = {"project_id": project_id}
    if status is not None:
        query_filter["status"] = status
    if environment is not None:
        query_filter["environment"] = environment

    return await backend.find_many(
        "traces",
        filter=query_filter,
        limit=limit,
        sort=sort,
    )


async def count_project_traces(
    backend: StorageBackend,
    project_id: str,
    environment: str | None = None,
    status: str | None = None,
) -> int:
    """Count traces scoped to a specific project."""
    # Without this scope, counts would span all projects and leak other tenants' volumes.
    query_filter: dict[str, Any] = {"project_id": project_id}
    if environment is not None:
        query_filter["environment"] = environment
    if status is not None:
        query_filter["status"] = status

    return await backend.count_documents("traces", filter=query_filter)
