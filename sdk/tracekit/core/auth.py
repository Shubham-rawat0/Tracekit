"""API Key generation, hashing, validation, revocation, and metadata for Tracekit."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tracekit.core.storage import StorageBackend


class AuthError(Exception):
    """Base exception for authentication and authorization errors."""


class AuthenticationError(AuthError):
    """Raised when an API key is invalid, expired, revoked, or malformed."""


class AuthorizationError(AuthError):
    """Raised when an authenticated entity lacks permission to access a project/resource."""


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
class APIKeyRecord:
    """Represents a stored API key record."""

    key_id: str
    key_hash: str
    salt: str
    name: str = ""
    project_id: str | None = None
    environment: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    revoked: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key_id,
            "key_hash": self.key_hash,
            "salt": self.salt,
            "name": self.name,
            "project_id": self.project_id,
            "environment": self.environment,
            "metadata": json.dumps(self.metadata) if self.metadata else "{}",
            "expires_at": (
                self.expires_at.isoformat(timespec="microseconds")
                if self.expires_at
                else None
            ),
            "revoked": 1 if self.revoked else 0,
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
    def from_dict(cls, data: dict[str, Any]) -> APIKeyRecord:
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
            key_id=str(data.get("key", "")),
            key_hash=str(data.get("key_hash", "")),
            salt=str(data.get("salt", "")),
            name=str(data.get("name", "") or ""),
            project_id=data.get("project_id"),
            environment=data.get("environment"),
            metadata=metadata,
            expires_at=_parse_datetime(data.get("expires_at")),
            revoked=bool(data.get("revoked", 0)),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


def hash_api_key(raw_key: str, salt: str | None = None) -> tuple[str, str]:
    """Hash an API key using PBKDF2-HMAC-SHA256 and a per-key salt.

    Returns:
        (key_hash, salt)
    """
    if not raw_key:
        raise ValueError("raw_key cannot be empty")
    if salt is None:
        # A fresh salt per key makes identical raw keys produce different stored hashes.
        salt = secrets.token_hex(16)

    key_hash = hashlib.pbkdf2_hmac(
        "sha256",
        raw_key.encode("utf-8"),
        salt.encode("utf-8"),
        10_000,
    ).hex()
    return key_hash, salt


def verify_api_key(raw_key: str, stored_hash: str, salt: str) -> bool:
    """Verify raw_key against stored_hash using constant-time comparison."""
    if not raw_key or not stored_hash or not salt:
        return False
    computed_hash, _ = hash_api_key(raw_key, salt=salt)
    return hmac.compare_digest(computed_hash, stored_hash)


async def _sanitize_sqlite_indexes(backend: StorageBackend) -> None:
    """Ensure SQLite backend does not enforce accidental unique constraints on project_id/environment."""
    conn = getattr(backend, "_conn", None)
    if conn is not None:
        try:
            # Older unique indexes would reject multiple keys sharing a project/environment; rebuild them non-unique.
            await conn.execute("DROP INDEX IF EXISTS idx_api_keys_project_id")
            await conn.execute("DROP INDEX IF EXISTS idx_api_keys_environment")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_keys_project_id ON api_keys(project_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_keys_environment ON api_keys(environment)"
            )
            await conn.commit()
        except Exception:
            pass


async def generate_api_key(
    backend: StorageBackend,
    name: str = "",
    project_id: str | None = None,
    environment: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | str | None = None,
    prefix: str = "tk",
) -> tuple[str, APIKeyRecord]:
    """Generate a new secure API key and persist its record without storing plaintext.

    Format: {prefix}_{key_id}_{secret}
    Returns:
        (raw_key, record)
    """
    await _sanitize_sqlite_indexes(backend)

    # key_id is embedded in the raw token so lookups can find the record without scanning hashes.
    key_id = f"{prefix}_{secrets.token_hex(8)}"
    secret = secrets.token_urlsafe(24)
    raw_key = f"{key_id}_{secret}"

    key_hash, salt = hash_api_key(raw_key)
    parsed_expires_at = _parse_datetime(expires_at) if expires_at is not None else None

    now = datetime.now(timezone.utc)
    record = APIKeyRecord(
        key_id=key_id,
        key_hash=key_hash,
        salt=salt,
        name=name,
        project_id=project_id,
        environment=environment,
        metadata=dict(metadata or {}),
        expires_at=parsed_expires_at,
        revoked=False,
        created_at=now,
        updated_at=now,
    )

    await backend.insert_one("api_keys", record.to_dict())
    return raw_key, record


async def validate_api_key(
    backend: StorageBackend,
    raw_key: str,
) -> APIKeyRecord:
    """Validate a raw API key.

    Checks existence, signature/hash, expiration, and revocation.
    Raises:
        AuthenticationError: If the key is invalid, revoked, or expired.
    """
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise AuthenticationError("API key cannot be empty")

    raw_key = raw_key.strip()

    # Extract key_id if standard tk_<id>_<secret> format
    key_id: str | None = None
    parts = raw_key.split("_")
    # Splitting only known prefixes leaves custom-format keys searchable verbatim.
    if len(parts) >= 3 and parts[0] in ("tk", "tracekit"):
        key_id = f"{parts[0]}_{parts[1]}"
    else:
        key_id = raw_key

    doc = await backend.find_one("api_keys", {"key": key_id})
    if doc is None:
        # Fallback search by key_hash or raw key
        doc = await backend.find_one("api_keys", {"key": raw_key})

    if doc is None:
        raise AuthenticationError("Invalid API key")

    record = APIKeyRecord.from_dict(doc)

    # Revocation and expiry are checked before hashing, so a bad-but-revoked key reports revocation.
    if record.revoked:
        raise AuthenticationError("API key has been revoked")

    if record.expires_at is not None:
        now = datetime.now(timezone.utc)
        if record.expires_at <= now:
            raise AuthenticationError("API key has expired")

    if not verify_api_key(raw_key, record.key_hash, record.salt):
        raise AuthenticationError("Invalid API key credentials")

    return record


async def revoke_api_key(
    backend: StorageBackend,
    key_id: str,
) -> bool:
    """Revoke an API key by its key_id.

    Returns:
        True if revoked, False if not found.
    """
    await _sanitize_sqlite_indexes(backend)
    doc = await backend.find_one("api_keys", {"key": key_id})
    if doc is None:
        return False

    record = APIKeyRecord.from_dict(doc)
    record.revoked = True
    record.updated_at = datetime.now(timezone.utc)
    # No in-place update exists, so revocation rewrites the record as a tombstone rather than erasing it.
    if hasattr(backend, "delete_one"):
        await backend.delete_one("api_keys", {"key": key_id})
    await backend.insert_one("api_keys", record.to_dict())
    return True


async def get_api_key(
    backend: StorageBackend,
    key_id: str,
) -> APIKeyRecord | None:
    """Retrieve an API key record by its key_id (never returns the secret)."""
    doc = await backend.find_one("api_keys", {"key": key_id})
    if doc is None:
        return None
    return APIKeyRecord.from_dict(doc)


async def list_api_keys(
    backend: StorageBackend,
    project_id: str | None = None,
    environment: str | None = None,
    include_revoked: bool = False,
) -> list[APIKeyRecord]:
    """List stored API key records with optional project/environment filtering."""
    query_filter: dict[str, Any] = {}
    if project_id is not None:
        query_filter["project_id"] = project_id
    if environment is not None:
        query_filter["environment"] = environment

    docs = await backend.find_many("api_keys", filter=query_filter if query_filter else None)
    records = [APIKeyRecord.from_dict(d) for d in docs]
    # Revoked keys are filtered client-side so backends without boolean filter support behave alike.
    if not include_revoked:
        records = [r for r in records if not r.revoked]
    return records


async def delete_api_key(
    backend: StorageBackend,
    key_id: str,
) -> bool:
    """Permanently delete an API key record."""
    # Backends lacking delete_one cannot honor deletion, so report False instead of silent success.
    if hasattr(backend, "delete_one"):
        count = await backend.delete_one("api_keys", {"key": key_id})
        return count > 0
    return False
