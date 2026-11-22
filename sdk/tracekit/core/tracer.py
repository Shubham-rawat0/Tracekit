"""Tracer implementation for managing traces and spans."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterator, Protocol

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    reset_current_span,
    reset_current_trace,
    set_current_span,
    set_current_trace,
)
from tracekit.core.event import Event
from tracekit.core.propagation import SpanContext, extract
from tracekit.core.sampling import Sampler, sampler_from_value
from tracekit.core.span import Span, SpanType, normalize_span_type
from tracekit.core.storage import StorageBackend
from tracekit.core.tokens import (
    TOKEN_USAGE_PROVIDER,
    aggregate_token_usage,
    estimate_token_usage,
    finalize_provider_usage,
    is_llm_span,
    normalize_provider_usage,
)
from tracekit.core.trace import Trace

logger = logging.getLogger(__name__)


class StreamPublisher(Protocol):
    """Duck-typed protocol for publishing lifecycle events."""
    def publish(self, event: dict[str, object]) -> None: ...


class StreamServerProtocol(Protocol):
    """Duck-typed protocol for the streaming server."""
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class _Unset:
    """Sentinel for omitted storage parameter."""
    pass


# Distinguishes omitted storage from explicit None, which disables persistence.
_UNSET = _Unset()


class Tracer:
    def __init__(
        self,
        storage: StorageBackend | None | _Unset = _UNSET,
        require_trace: bool = False,
        sampler: Sampler | bool | float | None = None,
        auto_flush: bool = False,
        flush_batch_size: int = 100,
        flush_interval: float | None = None,
        persist_enabled: bool = True,
        db_path: str | None = None,
        stream_enabled: bool = False,
        stream_host: str = "127.0.0.1",
        stream_port: int = 8765,
        stream_manager: StreamPublisher | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
        environment: str | None = None,
    ) -> None:
        self.storage: StorageBackend | None
        # Explicit storage=None wins over persist_enabled, keeping this tracer memory-only.
        self._default_storage_requested = storage is _UNSET and persist_enabled
        self._default_db_path = db_path
        if storage is _UNSET:
            self.storage = None
        else:
            self.storage = storage

        self.require_trace = require_trace
        self.sampler = sampler_from_value(sampler)
        self.auto_flush = bool(auto_flush)
        self.flush_batch_size = max(0, int(flush_batch_size or 0))
        self.flush_interval = flush_interval

        self._stream_enabled = stream_enabled
        self._stream_host = stream_host
        self._stream_port = stream_port
        self._stream_manager: StreamPublisher | None = stream_manager
        self._stream_server: StreamServerProtocol | None = None
        self.project_id = project_id
        self.project_name = project_name
        self.environment = environment

        self._traces: dict[str, Trace] = {}
        self._spans: dict[str, Span] = {}
        self._trace_token_usage: dict[str, dict[str, dict[str, Any]]] = {}
        self._flushed_trace_ids: set[str] = set()
        self._flushed_span_ids: set[str] = set()
        self._ended_since_flush = 0
        self._flush_task: asyncio.Task | None = None
        self._flush_loop_task: asyncio.Task | None = None
        self._flushing = False

    def _ensure_storage(self) -> None:
        if self.storage is not None or not self._default_storage_requested:
            return
        from backend.apps.db.sqllite import SQLiteBackend

        self.storage = (
            SQLiteBackend(db_path=self._default_db_path)
            if self._default_db_path
            else SQLiteBackend()
        )

    @property
    def stream_manager(self) -> StreamPublisher | None:
        """Read-only access to the stream manager."""
        return self._stream_manager

    def _create_trace(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        sampled: bool | None = None,
        parent_context: SpanContext | None = None,
    ) -> Trace:
        trace = Trace(
            name=name,
            attributes=attributes or {},
            metadata=dict(metadata or {}),
            project_id=self.project_id,
            project_name=self.project_name,
            environment=self.environment,
        )
        # Reuse the remote trace id so every service reports one distributed trace.
        if parent_context is not None:
            trace.trace_id = parent_context.trace_id
        parent_sampled = None if parent_context is None else parent_context.sampled
        # An explicit value wins; otherwise inherit upstream sampling before consulting the sampler.
        if sampled is None:
            if parent_context is not None:
                sampled = parent_context.sampled
            else:
                sampled = self.sampler.should_sample(
                    trace_id=trace.trace_id,
                    name=name,
                    parent_sampled=parent_sampled,
                )
        trace.metadata["sampled"] = bool(sampled)
        return trace

    def _publish_event(self, event: dict[str, object]) -> None:
        """Publish a lifecycle event, catching all errors to never break tracing."""
        if self._stream_manager is not None:
            try:
                self._stream_manager.publish(event)
            except Exception as e:
                # Optional-stream failures stay at debug level and never reach tracing callers.
                logger.debug("Stream publisher error (ignored): %s", e)

    def _publish_trace_start(self, trace: Trace) -> None:
        # Unsampled traces aren't recorded, so lifecycle events for them would be misleading.
        if not trace.metadata.get("sampled", True):
            return
        self._publish_event({
            "type": "trace_start",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace.trace_id,
            "sampled": True,
            "trace": trace.to_dict(),
        })

    def _publish_trace_end(self, trace: Trace) -> None:
        if not trace.metadata.get("sampled", True):
            return
        self._publish_event({
            "type": "trace_end",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace.trace_id,
            "sampled": True,
            "trace": trace.to_dict(),
        })

    def _publish_span_start(self, span: Span) -> None:
        if not span.attributes.get("sampled", True):
            return
        self._publish_event({
            "type": "span_start",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "sampled": True,
            "span": span.to_dict(),
        })

    def _publish_span_end(self, span: Span) -> None:
        if not span.attributes.get("sampled", True):
            return
        self._publish_event({
            "type": "span_end",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "sampled": True,
            "span": span.to_dict(),
        })

    def start_trace(
        self,
        name: str = "trace",
        attributes: dict[str, Any] | None = None,
        sampled: bool | None = None,
        parent_context: SpanContext | None = None,
        **metadata: Any,
    ) -> Trace:
        trace = self._create_trace(
            name=name,
            attributes=attributes,
            metadata=metadata,
            sampled=sampled,
            parent_context=parent_context,
        )
        trace.start()
        # Only sampled traces enter memory; unsampled ones leave no stored record.
        if trace.metadata.get("sampled", True):
            self._traces[trace.trace_id] = trace
        # Keep the contextvar token on the trace for exact restoration at end_trace.
        token = set_current_trace(trace)
        setattr(trace, "_tracekit_trace_token", token)
        self._ensure_flush_loop()
        self._publish_trace_start(trace)
        return trace

    def _ensure_flush_loop(self) -> None:
        if self.flush_interval is None or float(self.flush_interval) <= 0:
            return
        if self._flush_loop_task is not None and not self._flush_loop_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Without a running loop there is nowhere to schedule periodic flushes.
            return
        self._flush_loop_task = loop.create_task(self._flush_loop())

    def continue_trace(
        self,
        headers: dict[str, Any] | None,
        name: str = "remote-trace",
        attributes: dict[str, Any] | None = None,
        sampled: bool | None = None,
        **metadata: Any,
    ) -> Trace:
        """Start a trace continued from incoming headers (W3C traceparent)."""
        # Invalid or absent headers yield no parent context, so a fresh root trace starts.
        return self.start_trace(
            name=name,
            attributes=attributes,
            sampled=sampled,
            parent_context=extract(headers),
            **metadata,
        )

    def _set_span_token_usage(self, span: Span, usage: dict[str, Any]) -> None:
        """Store token counts and source on a span and its trace ledger."""

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        source = usage.get("token_usage_source") or TOKEN_USAGE_PROVIDER
        span.input_tokens = input_tokens
        span.output_tokens = output_tokens
        span.total_tokens = total_tokens
        span.token_usage_source = source
        legacy_usage = dict(span.attributes.get("token_usage") or {})
        legacy_usage.update(
            {
                "prompt_tokens": input_tokens,
                "input_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        )
        span.attributes["token_usage"] = legacy_usage
        span.attributes["token_usage_source"] = source
        self._trace_token_usage.setdefault(span.trace_id, {})[span.span_id] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "token_usage_source": source,
        }

    def _span_token_usage(self, span: Span) -> dict[str, Any] | None:
        """Read token counts from a span, including legacy attribute payloads."""

        if span.input_tokens is not None or span.output_tokens is not None or span.total_tokens is not None:
            return {
                "input_tokens": span.input_tokens,
                "output_tokens": span.output_tokens,
                "total_tokens": span.total_tokens,
                "token_usage_source": span.token_usage_source or TOKEN_USAGE_PROVIDER,
            }
        token_usage = (span.attributes or {}).get("token_usage")
        if isinstance(token_usage, dict):
            normalized = normalize_provider_usage(sources=(token_usage,))
            if normalized:
                return finalize_provider_usage(normalized)
        return None

    def end_trace(
        self,
        trace: Trace | None = None,
        status: str = "ok",
        error: str | None = None,
        error_type: str | None = None,
    ) -> Trace | None:
        """Close a trace and aggregate its LLM span token usage."""

        trace = trace or get_current_trace()
        if trace is None:
            return None
        trace.end(status="error" if error else status, error=error)
        spans = self.get_spans_for_trace(trace.trace_id)
        usage_by_span = dict(self._trace_token_usage.get(trace.trace_id, {}))
        prompt = None
        response = None
        model = None
        for s in spans:
            if is_llm_span(s) and s.span_id not in usage_by_span:
                usage = self._span_token_usage(s)
                if usage is not None:
                    usage_by_span[s.span_id] = usage
            if is_llm_span(s):
                if prompt is None:
                    prompt = s.input
                if s.output is not None:
                    response = s.output
                if model is None:
                    model = (s.attributes or {}).get("model")
        token_usage = aggregate_token_usage(usage_by_span.values())
        if token_usage:
            trace.input_tokens = token_usage["input_tokens"]
            trace.output_tokens = token_usage["output_tokens"]
            trace.total_tokens = token_usage["total_tokens"]
            trace.token_usage_source = token_usage["token_usage_source"]
            trace.token_count = token_usage["total_tokens"]
            trace.metadata.setdefault("input_tokens", token_usage["input_tokens"])
            trace.metadata.setdefault("output_tokens", token_usage["output_tokens"])
            trace.metadata.setdefault("total_tokens", token_usage["total_tokens"])
            trace.metadata.setdefault("token_usage_source", token_usage["token_usage_source"])
        if prompt is not None:
            trace.prompt = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
            trace.metadata.setdefault("prompt", prompt)
        if response is not None:
            trace.response = response if isinstance(response, str) else json.dumps(response, default=str)
            trace.metadata.setdefault("response", response)
        if model is not None:
            trace.model_name = str(model)
            trace.metadata.setdefault("model", str(model))
        if error_type is not None:
            trace.error_type = error_type
        elif trace.error_type is None and error:
            trace.error_type = "Error"
        if trace.duration_ms is not None:
            trace.metadata.setdefault("latency_ms", trace.duration_ms)
            threshold = float(trace.metadata.get("slow_request_threshold_ms", 5000))
            if trace.duration_ms > threshold:
                trace.metadata["slow_request"] = True
        token = getattr(trace, "_tracekit_trace_token", None)
        # Reset only if this trace is still current; a nested trace may have replaced it.
        if token is not None and get_current_trace() is trace:
            # Context cleanup is best-effort because the token may already be stale.
            try:
                reset_current_trace(token)
            except Exception:
                pass
            try:
                delattr(trace, "_tracekit_trace_token")
            except Exception:
                pass
        if trace.metadata.get("sampled", True):
            self._ended_since_flush += 1
            self._maybe_schedule_flush(trace_ended=True)
        self._publish_trace_end(trace)
        return trace

    def _maybe_end_auto_trace(self, span: Span) -> None:
        # Auto-created traces have no explicit end call, so close them when their last span ends.
        auto_trace = self._traces.get(span.trace_id)
        if (
            auto_trace is not None
            and (
                getattr(auto_trace, "_tracekit_auto_trace", False)
                or getattr(auto_trace, "_tracekit_remote_trace", False)
            )
            and auto_trace.status == "running"
            and not any(
                s.trace_id == span.trace_id and s.status == "running"
                for s in self._spans.values()
            )
        ):
            self.end_trace(auto_trace)

    def start_span(
        self,
        name: str,
        span_type: SpanType | str = SpanType.INTERNAL,
        trace: Trace | None = None,
        parent: Span | None = None,
        attributes: dict[str, Any] | None = None,
        input: Any = None,
        set_current: bool = True,
        require_trace: bool | None = None,
        parent_context: SpanContext | None = None,
        sampled: bool | None = None,
        **extra: Any,
    ) -> Span:
        require_trace = self.require_trace if require_trace is None else require_trace
        trace = trace or get_current_trace()
        if trace is None:
            # A remote parent without a local trace creates one that auto-closes with its spans.
            if parent_context is not None:
                trace = self.start_trace(
                    name="remote-trace",
                    sampled=sampled,
                    parent_context=parent_context,
                )
                setattr(trace, "_tracekit_remote_trace", True)
                setattr(trace, "_tracekit_auto_trace", True)
            elif require_trace:
                raise RuntimeError(
                    "no active trace; call start_trace()/trace() first or pass require_trace=False"
                )
            else:
                trace = self.start_trace(name="auto-trace")
                setattr(trace, "_tracekit_auto_trace", True)
        elif parent_context is not None and parent_context.trace_id != trace.trace_id:
            # A foreign trace id would corrupt the active trace's span tree.
            raise ValueError(
                "parent_context trace_id does not match the active trace"
            )
        span_type = normalize_span_type(span_type)
        parent = parent or get_current_span()
        # With no local parent, the remote span becomes this span's parent.
        if parent is None and parent_context is not None:
            parent_span_id = parent_context.span_id
        else:
            parent_span_id = parent.span_id if parent else None
        trace_sampled = bool(trace.metadata.get("sampled", True))
        # Both the local trace and any upstream parent must agree before recording a span.
        if sampled is None:
            sampled = trace_sampled
            if parent_context is not None:
                sampled = sampled and parent_context.sampled
        attrs = dict(attributes or {})
        attrs.update(extra)
        # Mark dropped spans so downstream helpers can skip work without a second flag.
        if not sampled:
            attrs.setdefault("sampled", False)
        span = Span(
            name=name,
            trace_id=trace.trace_id,
            parent_span_id=parent_span_id,
            span_type=span_type,
            attributes=attrs,
            input=input,
            project_id=self.project_id or trace.project_id,
            environment=self.environment or trace.environment,
        )
        span.start()
        if sampled:
            self._spans[span.span_id] = span
        # Span tokens are stored for exact restoration when the span ends.
        if set_current:
            token = set_current_span(span)
            setattr(span, "_tracekit_span_token", token)
        self._ensure_flush_loop()
        self._publish_span_start(span)
        return span

    def start_llm_span(
        self,
        name: str = "llm",
        model: str | None = None,
        prompt: Any = None,
        messages: Any = None,
        attributes: dict[str, Any] | None = None,
        require_trace: bool | None = None,
        parent_context: SpanContext | None = None,
        sampled: bool | None = None,
        **extra: Any,
    ) -> Span:
        attrs = dict(attributes or {})
        if model is not None:
            attrs["model"] = model
        if messages is not None:
            attrs["messages"] = messages
        return self.start_span(
            name=name,
            span_type=SpanType.LLM,
            attributes=attrs,
            input=prompt,
            require_trace=require_trace,
            parent_context=parent_context,
            sampled=sampled,
            **extra,
        )

    def start_tool_span(
        self,
        name: str,
        tool_name: str | None = None,
        tool_input: Any = None,
        attributes: dict[str, Any] | None = None,
        require_trace: bool | None = None,
        parent_context: SpanContext | None = None,
        sampled: bool | None = None,
        **extra: Any,
    ) -> Span:
        attrs = dict(attributes or {})
        attrs["tool_name"] = tool_name or name
        return self.start_span(
            name=name,
            span_type=SpanType.TOOL,
            attributes=attrs,
            input=tool_input,
            require_trace=require_trace,
            parent_context=parent_context,
            sampled=sampled,
            **extra,
        )

    def end_span(
        self,
        span: Span | None = None,
        status: str | None = None,
        error: str | None = None,
        output: Any = None,
        attributes: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> Span | None:
        span = span or get_current_span()
        if span is None:
            return None
        if error_type is not None:
            span.error_type = error_type
        if output is not None:
            span.output = output
        if attributes:
            span.attributes.update(attributes)
        span.end(status=status, error=error)
        if span.duration_ms is not None:
            span.attributes.setdefault("latency_ms", span.duration_ms)
        token = getattr(span, "_tracekit_span_token", None)
        if token is not None and get_current_span() is span:
            try:
                reset_current_span(token)
            except Exception:
                pass
            try:
                delattr(span, "_tracekit_span_token")
            except Exception:
                pass
        self._maybe_end_auto_trace(span)
        if span.attributes.get("sampled", True) and span.span_id in self._spans:
            self._ended_since_flush += 1
            self._maybe_schedule_flush()
        self._publish_span_end(span)
        return span

    def record_llm_result(
        self,
        span: Span | None = None,
        response: Any = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        latency_ms: float | None = None,
        token_usage: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        usage_metadata: dict[str, Any] | None = None,
        **metadata: Any,
    ) -> Span | None:
        """Record an LLM result with provider usage or a jittered estimate."""

        span = span or get_current_span()
        if span is None:
            return None
        if response is not None:
            span.output = response
        if model is not None:
            span.attributes["model"] = model
        provider_usage = normalize_provider_usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            total_tokens=total_tokens,
            sources=(token_usage, usage, usage_metadata),
        )
        resolved_usage: dict[str, Any] | None
        if provider_usage:
            resolved_usage = finalize_provider_usage(provider_usage)
        elif span.token_usage_source is not None:
            resolved_usage = {
                "input_tokens": span.input_tokens,
                "output_tokens": span.output_tokens,
                "total_tokens": span.total_tokens,
                "token_usage_source": span.token_usage_source,
            }
        elif is_llm_span(span):
            output_value = response if response is not None else span.output
            try:
                resolved_usage = estimate_token_usage(span.input, output_value)
            except Exception:
                logger.debug("token estimation failed", exc_info=True)
                resolved_usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "token_usage_source": "estimated",
                }
        else:
            resolved_usage = None
        if resolved_usage is not None:
            self._set_span_token_usage(span, resolved_usage)
        if latency_ms is not None:
            span.attributes["latency_ms"] = latency_ms
        if metadata:
            span.attributes.update(metadata)
        event_attributes: dict[str, Any] = {}
        if model is not None:
            event_attributes["model"] = model
        if latency_ms is not None:
            event_attributes["latency_ms"] = latency_ms
        if resolved_usage is not None:
            event_attributes.update(
                {
                    "prompt_tokens": resolved_usage["input_tokens"],
                    "input_tokens": resolved_usage["input_tokens"],
                    "completion_tokens": resolved_usage["output_tokens"],
                    "output_tokens": resolved_usage["output_tokens"],
                    "total_tokens": resolved_usage["total_tokens"],
                    "token_usage_source": resolved_usage["token_usage_source"],
                }
            )
        if event_attributes:
            span.add_event("llm.result", event_attributes)
        return span

    def record_tool_result(
        self,
        span: Span | None = None,
        output: Any = None,
        error: str | None = None,
        **metadata: Any,
    ) -> Span | None:
        span = span or get_current_span()
        if span is None:
            return None
        if output is not None:
            span.output = output
        if error is not None:
            span.error = error
            span.status = "error"
            span.add_event("tool.error", {"error": error})
        if metadata:
            span.attributes.update(metadata)
        return span

    def record_exception(
        self,
        exc: BaseException,
        span: Span | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Event | None:
        """Record an exception on the current or specified span/trace."""
        span = span or get_current_span()
        if span is None:
            return None
        return span.record_exception(exc, attributes)

    def record_retry(
        self,
        span: Span | None = None,
        attempt: int = 1,
        reason: str | None = None,
        **attributes: Any,
    ) -> Span | None:
        span = span or get_current_span()
        if span is None:
            return None
        event_attributes: dict[str, Any] = {"attempt": attempt}
        if reason is not None:
            event_attributes["reason"] = reason
        event_attributes.update(attributes)
        span.add_event("retry", event_attributes)
        span.attributes["retry_count"] = int(span.attributes.get("retry_count", 0)) + 1
        return span

    @contextmanager
    def span(
        self,
        name: str,
        span_type: SpanType | str = SpanType.INTERNAL,
        attributes: dict[str, Any] | None = None,
        input: Any = None,
        require_trace: bool | None = None,
        parent_context: SpanContext | None = None,
        sampled: bool | None = None,
    ) -> Iterator[Span]:
        parent = get_current_span()
        span = self.start_span(
            name=name,
            span_type=span_type,
            parent=parent,
            attributes=attributes,
            input=input,
            set_current=False,
            require_trace=require_trace,
            parent_context=parent_context,
            sampled=sampled,
        )
        # start_span skipped context setup so this manager owns the context lifetime.
        token = set_current_span(span)
        try:
            yield span
            if span.status == "running":
                self.end_span(span, status="ok")
        except Exception as e:
            # Record the failure before re-raising so callers still see the original exception.
            self.end_span(span, status="error", error=str(e), error_type=type(e).__name__)
            raise
        finally:
            reset_current_span(token)

    @asynccontextmanager
    async def async_span(
        self,
        name: str,
        span_type: SpanType | str = SpanType.INTERNAL,
        attributes: dict[str, Any] | None = None,
        input: Any = None,
        require_trace: bool | None = None,
        parent_context: SpanContext | None = None,
        sampled: bool | None = None,
    ) -> AsyncIterator[Span]:
        parent = get_current_span()
        span = self.start_span(
            name=name,
            span_type=span_type,
            parent=parent,
            attributes=attributes,
            input=input,
            set_current=False,
            require_trace=require_trace,
            parent_context=parent_context,
            sampled=sampled,
        )
        token = set_current_span(span)
        try:
            yield span
            if span.status == "running":
                self.end_span(span, status="ok")
        except Exception as e:
            self.end_span(span, status="error", error=str(e), error_type=type(e).__name__)
            raise
        finally:
            reset_current_span(token)

    @contextmanager
    def trace(
        self,
        name: str = "trace",
        attributes: dict[str, Any] | None = None,
        sampled: bool | None = None,
        parent_context: SpanContext | None = None,
        **metadata: Any,
    ) -> Iterator[Trace]:
        # Keep prior context values as a fallback in case token resets fail.
        prev_trace = get_current_trace()
        prev_span = get_current_span()
        t = self._create_trace(
            name=name,
            attributes=attributes,
            metadata=metadata,
            sampled=sampled,
            parent_context=parent_context,
        )
        t.start()
        if t.metadata.get("sampled", True):
            self._traces[t.trace_id] = t
        t_token = set_current_trace(t)
        # A new trace must not inherit the previous trace's active span.
        s_token = set_current_span(None)
        self._ensure_flush_loop()
        self._publish_trace_start(t)
        try:
            yield t
            if t.status == "running":
                self.end_trace(t, status="ok")
        except Exception as e:
            self.end_trace(t, status="error", error=str(e), error_type=type(e).__name__)
            raise
        finally:
            try:
                reset_current_span(s_token)
            except Exception:
                set_current_span(prev_span)
            try:
                reset_current_trace(t_token)
            except Exception:
                set_current_trace(prev_trace)

    @asynccontextmanager
    async def async_trace(
        self,
        name: str = "trace",
        attributes: dict[str, Any] | None = None,
        sampled: bool | None = None,
        parent_context: SpanContext | None = None,
        **metadata: Any,
    ) -> AsyncIterator[Trace]:
        prev_trace = get_current_trace()
        prev_span = get_current_span()
        t = self._create_trace(
            name=name,
            attributes=attributes,
            metadata=metadata,
            sampled=sampled,
            parent_context=parent_context,
        )
        t.start()
        if t.metadata.get("sampled", True):
            self._traces[t.trace_id] = t
        t_token = set_current_trace(t)
        s_token = set_current_span(None)
        self._ensure_flush_loop()
        self._publish_trace_start(t)
        try:
            yield t
            if t.status == "running":
                self.end_trace(t, status="ok")
        except Exception as e:
            self.end_trace(t, status="error", error=str(e), error_type=type(e).__name__)
            raise
        finally:
            try:
                reset_current_span(s_token)
            except Exception:
                set_current_span(prev_span)
            try:
                reset_current_trace(t_token)
            except Exception:
                set_current_trace(prev_trace)

    def _maybe_schedule_flush(self, trace_ended: bool = False) -> None:
        if (self.storage is None and not self._default_storage_requested) or not self.auto_flush:
            return
        # Trace completion always flushes; batch thresholds apply only to span ends.
        if (
            not trace_ended
            and self.flush_batch_size > 0
            and self._ended_since_flush < self.flush_batch_size
        ):
            return
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous callers can await flush() explicitly.
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = loop.create_task(self._flush_once())

    async def _flush_once(self) -> None:
        try:
            await self.flush()
        except Exception:
            # Background flushing must never crash the application.
            pass

    async def start_flush_loop(self) -> None:
        """Start periodic background flushing when ``flush_interval`` is set."""

        if self.flush_interval is None or float(self.flush_interval) <= 0:
            return
        if self._flush_loop_task is not None and not self._flush_loop_task.done():
            return
        self._flush_loop_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        interval = float(self.flush_interval or 0)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.flush()
            except asyncio.CancelledError:
                # Cancellation must propagate so stop_flush_loop can await this task.
                raise
            except Exception:
                # Storage failures are retried on the next tick instead of killing the loop.
                pass

    async def stop_flush_loop(self) -> None:
        task = self._flush_loop_task
        self._flush_loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def health_check(self) -> dict[str, Any]:
        """Return a lightweight snapshot of tracer and configuration state."""

        from tracekit.core.config import load_config

        storage_configured = self.storage is not None or self._default_storage_requested
        storage_connected = (
            None if self.storage is None else bool(getattr(self.storage, "is_connected", True))
        )
        status = "degraded" if storage_connected is False else "ok"
        return {
            "status": status,
            "storage": {
                "configured": storage_configured,
                "connected": storage_connected,
                "backend": (
                    type(self.storage).__name__
                    if self.storage is not None
                    else ("SQLiteBackend" if self._default_storage_requested else None)
                ),
            },
            "sampler": type(self.sampler).__name__,
            "sampled_traces": len(self._traces),
            "pending_spans": len(self._spans),
            "auto_flush": self.auto_flush,
            "flush_interval": self.flush_interval,
            "flush_loop_active": self._flush_loop_task is not None and not self._flush_loop_task.done(),
            "config": load_config(None).public_dict(),
        }

    async def close(self) -> None:
        """Stop background tasks, flush pending data, and close storage."""
        # Stop scheduled producers first so no new work is queued after the final flush.
        await self.stop_flush_loop()
        await self.stop_stream_server()
        await self.flush()
        if self.storage is not None:
            await self.storage.close()

    async def flush(self) -> None:
        """Persist pending traces and spans, then remove them from memory.

        Flushing is idempotent: IDs already written by this tracer are skipped,
        and successfully written items are dropped from the in-memory buffers.
        Items that fail to write remain buffered for a later retry.
        """
        # Batch and periodic triggers can overlap, so only one flush may run at a time.
        if self._flushing:
            return

        traces_to_flush = [
            tr for tr in self._traces.values()
            if tr.trace_id not in self._flushed_trace_ids
        ]
        spans_to_flush = [
            sp for sp in self._spans.values()
            if sp.span_id not in self._flushed_span_ids
        ]
        if not traces_to_flush and not spans_to_flush:
            return

        self._ensure_storage()
        # With persistence disabled, keep data buffered in memory for later use.
        if self.storage is None:
            return

        self._flushing = True
        try:
            if not self.storage.is_connected:
                await self.storage.initialize()

            trace_dicts = [tr.to_dict() for tr in traces_to_flush]
            span_dicts = [sp.to_dict() for sp in spans_to_flush]

            if trace_dicts:
                await self.storage.insert_many("traces", trace_dicts)
            if span_dicts:
                await self.storage.insert_many("spans", span_dicts)

            # Remember written ids after popping buffers so re-added items aren't written twice.
            for tr in traces_to_flush:
                self._flushed_trace_ids.add(tr.trace_id)
                self._traces.pop(tr.trace_id, None)

            for sp in spans_to_flush:
                self._flushed_span_ids.add(sp.span_id)
                self._spans.pop(sp.span_id, None)

            self._ended_since_flush = 0
        finally:
            self._flushing = False

    async def start_stream_server(self) -> object | None:
        """Start the streaming WebSocket server."""
        if not self._stream_enabled:
            return None
        if self._stream_server is not None:
            return self._stream_server

        if self._stream_manager is None:
            from tracekit.streaming.manager import StreamManager
            self._stream_manager = StreamManager()

        from tracekit.streaming.server import StreamServer
        server = StreamServer(self._stream_manager, host=self._stream_host, port=self._stream_port)
        await server.start()
        self._stream_server = server
        return server

    async def stop_stream_server(self) -> None:
        """Stop the streaming WebSocket server."""
        if self._stream_server is not None:
            await self._stream_server.stop()
            self._stream_server = None

    def get_trace(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)

    def get_spans_for_trace(self, trace_id: str) -> list[Span]:
        spans = [s for s in self._spans.values() if s.trace_id == trace_id]
        return sorted(
            spans,
            key=lambda s: (
                s.start_time or datetime.max.replace(tzinfo=timezone.utc),
                s.span_id,
            ),
        )

    def build_trace_replay(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.get_trace(trace_id)
        if trace is None:
            return None
        spans = self.get_spans_for_trace(trace_id)
        by_id = {s.span_id: s.to_dict() for s in spans}
        for s in by_id.values():
            s["children"] = []
        roots: list[dict[str, Any]] = []
        for s in by_id.values():
            parent_id = s.get("parent_span_id")
            # Spans with a missing parent become roots so partial traces still render.
            if parent_id and parent_id in by_id:
                by_id[parent_id]["children"].append(s)
            else:
                roots.append(s)
        return {
            "trace": trace.to_dict(),
            "spans": [s.to_dict() for s in spans],
            "tree": roots,
        }