"""Compatibility base module for storage backends."""

# Legacy `backend.apps.db.base` imports keep working via the canonical ABCs.
from tracekit.core.storage import StorageBackend, TraceStorage

__all__ = ["StorageBackend", "TraceStorage"]
