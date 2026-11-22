"""Unit tests for Phase 3 API keys, projects, environment tagging, and multi-tenant isolation."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.apps.db.memory import MemoryBackend
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.auth import (
    APIKeyRecord,
    AuthenticationError,
    AuthorizationError,
    delete_api_key,
    generate_api_key,
    get_api_key,
    hash_api_key,
    list_api_keys,
    revoke_api_key,
    validate_api_key,
    verify_api_key,
)
from tracekit.core.projects import (
    Project,
    archive_project,
    associate_trace_with_project,
    count_project_traces,
    create_project,
    delete_project,
    find_project_traces,
    get_project,
    get_project_by_name,
    list_projects,
    unarchive_project,
    update_project,
)
from tracekit.core.tenant import (
    Environment,
    TenantContext,
    TenantScopedStore,
    authorize_project_access,
    authorize_trace_access,
    create_tenant_context_from_key,
    get_current_environment,
    normalize_environment,
    tag_environment,
)


class TestAPIKeyHashingAndVerification(unittest.TestCase):
    def test_hashing_creates_unique_salts_and_hashes(self):
        raw_key = "tk_test_supersecretkey123"
        hash1, salt1 = hash_api_key(raw_key)
        hash2, salt2 = hash_api_key(raw_key)

        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(hash1, hash2)
        self.assertTrue(verify_api_key(raw_key, hash1, salt1))
        self.assertTrue(verify_api_key(raw_key, hash2, salt2))
        self.assertFalse(verify_api_key("wrong_key", hash1, salt1))
        self.assertFalse(verify_api_key(raw_key, hash1, salt2))

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            hash_api_key("")


class TestAuthAndProjectsMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = MemoryBackend()
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_generate_and_validate_api_key(self):
        raw_key, record = await generate_api_key(
            self.backend,
            name="Test Ingestion Key",
            project_id="proj_alpha",
            environment="production",
            metadata={"owner": "alice", "roles": ["ingest"]},
        )
        self.assertTrue(raw_key.startswith("tk_"))
        self.assertNotIn(raw_key, record.key_hash)

        # Database must never contain plaintext raw_key
        doc = await self.backend.find_one("api_keys", {"key": record.key_id})
        self.assertIsNotNone(doc)
        self.assertNotEqual(doc["key_hash"], raw_key)
        self.assertEqual(doc["project_id"], "proj_alpha")
        self.assertEqual(doc["environment"], "production")

        # Validate valid key
        validated = await validate_api_key(self.backend, raw_key)
        self.assertEqual(validated.key_id, record.key_id)
        self.assertEqual(validated.project_id, "proj_alpha")
        self.assertEqual(validated.metadata["owner"], "alice")

    async def test_validate_api_key_invalid_credentials(self):
        raw_key, record = await generate_api_key(self.backend, name="Key 1")

        # Tampered secret
        tampered = raw_key[:-4] + "xxxx"
        with self.assertRaises(AuthenticationError):
            await validate_api_key(self.backend, tampered)

        # Non-existent key
        with self.assertRaises(AuthenticationError):
            await validate_api_key(self.backend, "tk_0000000000000000_nonexistent")

        # Empty key
        with self.assertRaises(AuthenticationError):
            await validate_api_key(self.backend, "")

    async def test_api_key_expiration(self):
        now = datetime.now(timezone.utc)
        # Expired key
        past = now - timedelta(hours=1)
        raw_expired, _ = await generate_api_key(self.backend, name="Expired", expires_at=past)
        with self.assertRaises(AuthenticationError) as ctx:
            await validate_api_key(self.backend, raw_expired)
        self.assertIn("expired", str(ctx.exception).lower())

        # Future key
        future = now + timedelta(days=7)
        raw_valid, _ = await generate_api_key(self.backend, name="Valid", expires_at=future)
        validated = await validate_api_key(self.backend, raw_valid)
        self.assertIsNotNone(validated)

    async def test_api_key_revocation(self):
        raw_key, record = await generate_api_key(self.backend, name="To Revoke")
        # Works before revocation
        val = await validate_api_key(self.backend, raw_key)
        self.assertFalse(val.revoked)

        # Revoke
        revoked = await revoke_api_key(self.backend, record.key_id)
        self.assertTrue(revoked)

        # Now validation fails
        with self.assertRaises(AuthenticationError) as ctx:
            await validate_api_key(self.backend, raw_key)
        self.assertIn("revoked", str(ctx.exception).lower())

        # Revoking unknown key returns False
        self.assertFalse(await revoke_api_key(self.backend, "unknown_key"))

    async def test_api_key_crud_and_listing(self):
        _, k1 = await generate_api_key(self.backend, name="K1", project_id="p1", environment="dev")
        _, k2 = await generate_api_key(self.backend, name="K2", project_id="p1", environment="prod")
        _, k3 = await generate_api_key(self.backend, name="K3", project_id="p2", environment="dev")

        retrieved = await get_api_key(self.backend, k1.key_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.name, "K1")

        all_keys = await list_api_keys(self.backend)
        self.assertEqual(len(all_keys), 3)

        p1_keys = await list_api_keys(self.backend, project_id="p1")
        self.assertEqual(len(p1_keys), 2)

        # Revoke k2
        await revoke_api_key(self.backend, k2.key_id)
        active_keys = await list_api_keys(self.backend, include_revoked=False)
        self.assertEqual(len(active_keys), 2)
        all_including_revoked = await list_api_keys(self.backend, include_revoked=True)
        self.assertEqual(len(all_including_revoked), 3)

        # Delete k3
        self.assertTrue(await delete_api_key(self.backend, k3.key_id))
        self.assertEqual(len(await list_api_keys(self.backend, include_revoked=True)), 2)

    async def test_project_crud_and_archival(self):
        p1 = await create_project(self.backend, name="Project Alpha", project_id="proj-a", description="First")
        self.assertEqual(p1.project_id, "proj-a")
        self.assertEqual(p1.name, "Project Alpha")
        self.assertFalse(p1.archived)

        # Retrieve
        fetched = await get_project(self.backend, "proj-a")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "Project Alpha")

        fetched_by_name = await get_project_by_name(self.backend, "Project Alpha")
        self.assertIsNotNone(fetched_by_name)
        self.assertEqual(fetched_by_name.project_id, "proj-a")

        # Duplicate name / id rejected
        with self.assertRaises(ValueError):
            await create_project(self.backend, name="Project Alpha")
        with self.assertRaises(ValueError):
            await create_project(self.backend, name="Another", project_id="proj-a")

        # Update
        updated = await update_project(self.backend, "proj-a", description="Updated description")
        self.assertEqual(updated.description, "Updated description")

        # Archive
        self.assertTrue(await archive_project(self.backend, "proj-a"))
        active = await list_projects(self.backend, include_archived=False)
        self.assertEqual(len(active), 0)
        all_p = await list_projects(self.backend, include_archived=True)
        self.assertEqual(len(all_p), 1)

        # Unarchive
        self.assertTrue(await unarchive_project(self.backend, "proj-a"))
        active_again = await list_projects(self.backend, include_archived=False)
        self.assertEqual(len(active_again), 1)

    async def test_project_deletion_and_cascade(self):
        await create_project(self.backend, name="To Delete", project_id="proj-del")
        # Add traces
        await self.backend.insert_one("traces", {"trace_id": "t1", "project_id": "proj-del"})
        await self.backend.insert_one("spans", {"span_id": "s1", "trace_id": "t1"})
        await self.backend.insert_one("traces", {"trace_id": "t2", "project_id": "proj-other"})

        # Delete with cascade
        deleted = await delete_project(self.backend, "proj-del", cascade_traces=True)
        self.assertTrue(deleted)
        self.assertIsNone(await get_project(self.backend, "proj-del"))
        self.assertIsNone(await self.backend.find_one("traces", {"trace_id": "t1"}))
        self.assertIsNone(await self.backend.find_one("spans", {"span_id": "s1"}))
        self.assertIsNotNone(await self.backend.find_one("traces", {"trace_id": "t2"}))

    async def test_trace_project_association_and_queries(self):
        trace_dict = {"trace_id": "t_assoc", "name": "llm_call"}
        associated = associate_trace_with_project(trace_dict, "proj-assoc", "Associated Project")
        self.assertEqual(associated["project_id"], "proj-assoc")
        self.assertEqual(associated["project_name"], "Associated Project")
        self.assertEqual(associated["attributes"]["project_id"], "proj-assoc")

        await self.backend.insert_one("traces", associated)
        traces = await find_project_traces(self.backend, "proj-assoc")
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["trace_id"], "t_assoc")

        count = await count_project_traces(self.backend, "proj-assoc")
        self.assertEqual(count, 1)


class TestEnvironmentHelpers(unittest.TestCase):
    def test_normalize_environment(self):
        self.assertEqual(normalize_environment("dev"), "development")
        self.assertEqual(normalize_environment("DEVEL"), "development")
        self.assertEqual(normalize_environment("prod"), "production")
        self.assertEqual(normalize_environment("stage"), "staging")
        self.assertEqual(normalize_environment("ci"), "test")
        self.assertEqual(normalize_environment("custom-env"), "custom-env")
        self.assertEqual(normalize_environment(None), "development")

    def test_get_current_environment_env_var(self):
        old_env = os.environ.get("TRACEKIT_ENVIRONMENT")
        try:
            os.environ["TRACEKIT_ENVIRONMENT"] = "staging"
            self.assertEqual(get_current_environment(), "staging")
            os.environ["TRACEKIT_ENVIRONMENT"] = "prod"
            self.assertEqual(get_current_environment(), "production")
        finally:
            if old_env is not None:
                os.environ["TRACEKIT_ENVIRONMENT"] = old_env
            else:
                os.environ.pop("TRACEKIT_ENVIRONMENT", None)

    def test_tag_environment(self):
        doc: dict[str, Any] = {"trace_id": "t1"}
        tag_environment(doc, "staging")
        self.assertEqual(doc["environment"], "staging")
        self.assertEqual(doc["attributes"]["environment"], "staging")


class TestTenantIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = MemoryBackend()
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_authorization_checks(self):
        ctx_a = TenantContext(api_key_id="k_a", project_id="proj_a")
        ctx_b = TenantContext(api_key_id="k_b", project_id="proj_b")
        ctx_admin = TenantContext(api_key_id="k_admin", is_admin=True)

        authorize_project_access(ctx_b, "proj_b")
        with self.assertRaises(AuthorizationError):
            authorize_project_access(ctx_b, "proj_a")
        authorize_project_access(ctx_a, "proj_a")
        with self.assertRaises(AuthorizationError):
            authorize_project_access(ctx_a, "proj_b")

        authorize_project_access(ctx_admin, "proj_a")
        authorize_project_access(ctx_admin, "proj_b")

        # Trace access
        trace_a = {"trace_id": "ta", "project_id": "proj_a"}
        trace_b = {"trace_id": "tb", "project_id": "proj_b"}

        authorize_trace_access(ctx_a, trace_a)
        with self.assertRaises(AuthorizationError):
            authorize_trace_access(ctx_a, trace_b)

        authorize_trace_access(ctx_admin, trace_a)
        authorize_trace_access(ctx_admin, trace_b)

    async def test_tenant_scoped_store_isolation(self):
        store_a = TenantScopedStore(self.backend, context="proj_a")
        store_b = TenantScopedStore(self.backend, context="proj_b")

        # Insert traces
        await store_a.insert_trace({"trace_id": "t_a1", "name": "trace in a"})
        await store_b.insert_trace({"trace_id": "t_b1", "name": "trace in b"})

        # Cannot insert trace with mismatched project_id
        with self.assertRaises(AuthorizationError):
            await store_a.insert_trace({"trace_id": "t_bad", "project_id": "proj_b"})

        # Query scoping
        traces_a = await store_a.find_traces()
        self.assertEqual(len(traces_a), 1)
        self.assertEqual(traces_a[0]["trace_id"], "t_a1")

        traces_b = await store_b.find_traces()
        self.assertEqual(len(traces_b), 1)
        self.assertEqual(traces_b[0]["trace_id"], "t_b1")

        # Direct find prevents cross-project access
        found_a = await store_a.find_trace("t_a1")
        self.assertIsNotNone(found_a)
        with self.assertRaises(AuthorizationError):
            await store_a.find_trace("t_b1")

        # Cross-project deletion prevented
        with self.assertRaises(AuthorizationError):
            await store_a.delete_trace("t_b1")

        # Owned deletion succeeds
        self.assertTrue(await store_a.delete_trace("t_a1"))
        self.assertIsNone(await store_a.find_trace("t_a1"))


class TestSQLiteAuthAndProjects(unittest.IsolatedAsyncioTestCase):
    """End-to-end multi-tenancy verification on real SQLite backend."""

    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temp_dir.name) / "test_auth_projects.db"
        self.backend = SQLiteBackend(self.db_path)
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()
        self._temp_dir.cleanup()

    async def test_sqlite_key_lifecycle_and_multi_project_isolation(self):
        # 1. Projects
        p1 = await create_project(self.backend, name="Finance AI", project_id="proj_fin")
        p2 = await create_project(self.backend, name="Customer Care", project_id="proj_cc")
        self.assertEqual(p1.project_id, "proj_fin")
        self.assertEqual(p2.project_id, "proj_cc")

        # 2. API Keys
        raw_key_fin, rec_fin = await generate_api_key(
            self.backend,
            name="Finance Service",
            project_id="proj_fin",
            environment="production",
        )
        raw_key_cc, rec_cc = await generate_api_key(
            self.backend,
            name="Care Service",
            project_id="proj_cc",
            environment="staging",
        )

        # 3. Validation
        val_fin = await validate_api_key(self.backend, raw_key_fin)
        self.assertEqual(val_fin.project_id, "proj_fin")
        self.assertEqual(val_fin.environment, "production")

        val_cc = await validate_api_key(self.backend, raw_key_cc)
        self.assertEqual(val_cc.project_id, "proj_cc")
        self.assertEqual(val_cc.environment, "staging")

        # 4. Scoped Storage
        store_fin = TenantScopedStore(self.backend, context=val_fin)
        store_cc = TenantScopedStore(self.backend, context=val_cc)

        await store_fin.insert_trace({"trace_id": "tf1", "name": "calc_tax"})
        await store_cc.insert_trace({"trace_id": "tc1", "name": "answer_ticket"})

        # Fin cannot read Care trace
        with self.assertRaises(AuthorizationError):
            await store_fin.find_trace("tc1")

        # Care cannot delete Fin trace
        with self.assertRaises(AuthorizationError):
            await store_cc.delete_trace("tf1")

        # Count scoped
        self.assertEqual(await store_fin.count_traces(), 1)
        self.assertEqual(await store_cc.count_traces(), 1)


class DummyTraceObj:
    def __init__(self):
        self.attributes = {}
        self.metadata = {}


class TestCoverageEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = MemoryBackend()
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_auth_edge_cases(self):
        self.assertIsNone(await get_api_key(self.backend, "nonexistent"))
        self.assertFalse(await delete_api_key(self.backend, "nonexistent"))

        # from_dict parsing variations
        rec1 = APIKeyRecord.from_dict({"key": "k", "metadata": '{"role": "admin"}'})
        self.assertEqual(rec1.metadata.get("role"), "admin")
        rec2 = APIKeyRecord.from_dict({"key": "k", "metadata": None})
        self.assertEqual(rec2.metadata, {})
        rec3 = APIKeyRecord.from_dict({"key": "k", "metadata": "invalid json{"})
        self.assertEqual(rec3.metadata, {})

        # list_api_keys with env and project filter
        _, k = await generate_api_key(self.backend, project_id="p1", environment="staging")
        res = await list_api_keys(self.backend, project_id="p1", environment="staging")
        self.assertEqual(len(res), 1)

    async def test_project_edge_cases(self):
        with self.assertRaises(ValueError):
            await create_project(self.backend, name="")
        with self.assertRaises(ValueError):
            await create_project(self.backend, name="Valid", project_id="")

        self.assertIsNone(await get_project(self.backend, "nonexistent"))
        self.assertIsNone(await get_project_by_name(self.backend, "nonexistent"))
        self.assertIsNone(await update_project(self.backend, "nonexistent", name="New"))

        await create_project(self.backend, name="Proj 1", project_id="p1")
        await create_project(self.backend, name="Proj 2", project_id="p2")
        with self.assertRaises(ValueError):
            await update_project(self.backend, "p1", name="")
        with self.assertRaises(ValueError):
            await update_project(self.backend, "p1", name="Proj 2")

        # Project from_dict variations
        proj_dict1 = Project.from_dict({"project_id": "p", "metadata": '{"team": "core"}'})
        self.assertEqual(proj_dict1.metadata.get("team"), "core")
        proj_dict2 = Project.from_dict({"project_id": "p", "metadata": None})
        self.assertEqual(proj_dict2.metadata, {})

        # Associate with object having attributes & metadata
        dummy = DummyTraceObj()
        associated = associate_trace_with_project(dummy, "p1", "Project 1")
        self.assertEqual(associated.attributes["project_id"], "p1")
        self.assertEqual(associated.metadata["project_id"], "p1")

        # Query and count with status and environment filters
        await self.backend.insert_one(
            "traces",
            {"trace_id": "t1", "project_id": "p1", "status": "ok", "environment": "production"},
        )
        await self.backend.insert_one(
            "traces",
            {"trace_id": "t2", "project_id": "p1", "status": "error", "environment": "staging"},
        )

        filtered = await find_project_traces(self.backend, "p1", status="ok", environment="production")
        self.assertEqual(len(filtered), 1)
        cnt = await count_project_traces(self.backend, "p1", status="ok", environment="production")
        self.assertEqual(cnt, 1)

    async def test_tenant_edge_cases(self):
        ctx = TenantContext(project_id="p1", allowed_projects={"*"})
        self.assertTrue(ctx.can_access_project("p2"))
        self.assertFalse(ctx.can_access_project(""))

        # create_tenant_context_from_key with string metadata and admin flag
        key_dict = {
            "key": "k_admin",
            "metadata": '{"is_admin": true, "allowed_projects": ["p9"]}',
            "project_id": "p1",
        }
        ctx_from_dict = create_tenant_context_from_key(key_dict)
        self.assertTrue(ctx_from_dict.is_admin)
        self.assertIn("p9", ctx_from_dict.allowed_projects)
        self.assertIn("p1", ctx_from_dict.allowed_projects)

        with self.assertRaises(AuthorizationError):
            authorize_project_access(ctx_from_dict, "")

        # authorize_trace_access when trace has no project_id
        non_admin_ctx = TenantContext(project_id="p1", is_admin=False)
        with self.assertRaises(AuthorizationError):
            authorize_trace_access(non_admin_ctx, {"trace_id": "no_proj"})

        admin_ctx = TenantContext(is_admin=True)
        authorize_trace_access(admin_ctx, {"trace_id": "no_proj"})

        # tag_environment on object with attributes & metadata
        dummy = DummyTraceObj()
        tag_environment(dummy, "production")
        self.assertEqual(dummy.attributes["environment"], "production")
        self.assertEqual(dummy.metadata["environment"], "production")

        # TenantScopedStore with unassigned context
        open_store = TenantScopedStore(self.backend, context=TenantContext())
        await open_store.insert_trace({"trace_id": "t_open", "name": "open trace"})
        self.assertIsNone(await open_store.find_trace("nonexistent"))
        self.assertFalse(await open_store.delete_trace("nonexistent"))


if __name__ == "__main__":
    unittest.main()
