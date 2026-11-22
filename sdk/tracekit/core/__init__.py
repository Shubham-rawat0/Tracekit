"""Core tracekit functionality - spans, contexts, storage, and sampling.

This module contains the core building blocks for distributed tracing:
- Span and SpanContext for trace structure
- Storage backends for persistence
- Samplers for controlling trace rates
- Context management for current span/trace
"""

# Aliased because .span exports a different, same-named span() factory below.

from .async_writer import BoundedAsyncWriter, RetryPolicy, WriterMetrics
from .auth import (
    APIKeyRecord,
    AuthenticationError,
    AuthError,
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
from .context import (
    get_current_span,
    get_current_trace,
    get_trace_context,
    reset_current_span,
    reset_current_trace,
    reset_trace_context,
    set_current_span,
    set_current_trace,
    set_trace_context,
)
from .db import TraceStore
from .decorators import get_default_tracer, set_default_tracer, trace
from .decorators import span as span_decorator
from .event import Event
from .projects import (
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
from .propagation import (
    SpanContext,
    TraceContext,
    current_span_context,
    extract,
    format_traceparent,
    inject,
    parse_traceparent,
)
from .reliability import BoundedBatchQueue, ExportMetrics, ReliabilityMetrics, bounded_backoff
from .sampling import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    ParentBasedSampler,
    RateLimitedSampler,
    RateLimitingSampler,
    RateLimitSampler,
    Sampler,
    sampler_from_value,
)
from .span import Span, SpanType, normalize_span_type, span
from .storage import StorageBackend, TraceStorage
from .tenant import (
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
from .trace import Trace
from .tracer import Tracer

__all__ = [
    "get_default_tracer",
    "set_default_tracer",
    "trace",
    "span_decorator",
    "Event",
    "Span",
    "SpanType",
    "normalize_span_type",
    "span",
    "SpanContext",
    "TraceContext",
    "extract",
    "inject",
    "parse_traceparent",
    "format_traceparent",
    "current_span_context",
    "Sampler",
    "AlwaysOnSampler",
    "AlwaysOffSampler",
    "RateLimitSampler",
    "RateLimitedSampler",
    "RateLimitingSampler",
    "ParentBasedSampler",
    "sampler_from_value",
    "Trace",
    "Tracer",
    "StorageBackend",
    "TraceStorage",
    "TraceStore",
    "BoundedAsyncWriter",
    "RetryPolicy",
    "WriterMetrics",
    "BoundedBatchQueue",
    "ExportMetrics",
    "ReliabilityMetrics",
    "bounded_backoff",
    "APIKeyRecord",
    "AuthError",
    "AuthenticationError",
    "AuthorizationError",
    "hash_api_key",
    "verify_api_key",
    "generate_api_key",
    "validate_api_key",
    "revoke_api_key",
    "get_api_key",
    "list_api_keys",
    "delete_api_key",
    "Project",
    "create_project",
    "get_project",
    "get_project_by_name",
    "list_projects",
    "update_project",
    "archive_project",
    "unarchive_project",
    "delete_project",
    "associate_trace_with_project",
    "find_project_traces",
    "count_project_traces",
    "Environment",
    "TenantContext",
    "TenantScopedStore",
    "create_tenant_context_from_key",
    "authorize_project_access",
    "authorize_trace_access",
    "get_current_environment",
    "normalize_environment",
    "tag_environment",
    "get_trace_context",
    "set_trace_context",
    "reset_trace_context",
    "get_current_trace",
    "set_current_trace",
    "reset_current_trace",
    "get_current_span",
    "set_current_span",
    "reset_current_span",
]
