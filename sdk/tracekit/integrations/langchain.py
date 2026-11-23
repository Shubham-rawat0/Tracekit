"""LangChain callback handler integration for Tracekit."""

from __future__ import annotations

import json
import threading
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from tracekit.core.context import get_current_trace
from tracekit.core.span import SpanType
from tracekit.core.tokens import extract_token_usage
from tracekit.core.tracer import Tracer

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.outputs import LLMResult
except Exception:  # pragma: no cover - fallback for environments without langchain_core
    class BaseCallbackHandler:  # type: ignore
        pass

    class LLMResult:  # type: ignore
        pass


# Sentinel distinguishes "attribute absent" from an explicit None when restoring callbacks.
_MISSING = object()

_SESSION_IDLE_SECONDS = 10.0

# Per-invocation overrides set by auto-instrumentation before delegating to LangChain.
_RUN_OPTIONS: ContextVar[dict[str, bool]] = ContextVar("tracekit_langchain_run_options", default={})


def _set_run_options(options: dict[str, bool]) -> Any:
    """Merge run-scoped flags over the ambient options and return a reset token."""

    return _RUN_OPTIONS.set({**_RUN_OPTIONS.get(), **options})


def _reset_run_options(token: Any) -> None:
    """Restore the options that were active before a run."""

    _RUN_OPTIONS.reset(token)


_extract_token_usage = extract_token_usage


def _serialize_chat_messages(messages: List[List[Any]]) -> List[List[Any]]:
    serialized: List[List[Any]] = []
    for prompt in messages:
        serialized_prompt = []
        for message in prompt:
            model_dump = getattr(message, "model_dump", None)
            if callable(model_dump):
                serialized_prompt.append(model_dump(mode="json"))
                continue
            to_json = getattr(message, "json", None)
            if callable(to_json):
                serialized_prompt.append(json.loads(to_json()))
                continue
            if isinstance(message, dict):
                serialized_prompt.append(message)
                continue
            serialized_prompt.append(
                {
                    "type": getattr(message, "type", type(message).__name__),
                    "content": getattr(message, "content", str(message)),
                }
            )
        serialized.append(serialized_prompt)
    return serialized


def _callbacks_with_handler(existing: Any, handler: "TracekitCallbackHandler") -> Any:
    """Return callbacks with the TraceKit handler added without dropping existing ones."""

    # LangChain callbacks can be a manager, a list/tuple, or absent; normalize every shape.
    if existing is _MISSING or existing is None:
        return [handler]

    add_handler = getattr(existing, "add_handler", None)
    if callable(add_handler):
        add_handler(handler)
        return existing

    handlers = getattr(existing, "handlers", None)
    if isinstance(handlers, list):
        if handler not in handlers:
            handlers.append(handler)
        return existing

    if isinstance(existing, (list, tuple)):
        callbacks = list(existing)
        if handler not in callbacks:
            callbacks.append(handler)
        return callbacks

    return [existing, handler]


class _WrappedTarget:
    def __init__(self, target: Any, previous: Any) -> None:
        """Remember a target and its original callbacks for later restoration."""

        self.target = target
        self.previous = previous

    def __enter__(self) -> Any:
        """Return the wrapped target when used as a context manager."""

        return self.target

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        """Restore the original callbacks when the scope exits."""

        # Restore the exact prior shape (absent vs None) so wrapping is fully reversible.
        if self.previous is _MISSING:
            try:
                delattr(self.target, "callbacks")
            except AttributeError:
                pass
        else:
            self.target.callbacks = self.previous
        return False


class TracekitCallbackHandler(BaseCallbackHandler):
    """Maps LangChain callbacks to Tracekit spans."""

    run_inline = True

    def __init__(
        self,
        tracer: Optional[Tracer] = None,
        allow_failure: bool = False,
        mark_final: bool = False,
    ) -> None:
        """Create a callback handler that writes spans through the given tracer."""

        self.tracer = tracer or Tracer()
        self.allow_failure = allow_failure
        self.mark_final = mark_final
        # Maps LangChain run ids to active spans so end/error callbacks can close the right one.
        self._runs: Dict[str, Any] = {}
        self._run_traces: Dict[str, Any] = {}
        self._run_roots: Dict[str, str] = {}
        self._session_lock = threading.RLock()
        self._session_trace: Any | None = None
        self._session_roots: set[str] = set()
        self._session_root_kinds: Dict[str, str] = {}
        self._session_last_activity = 0.0
        self._session_status = "ok"
        self._session_error: str | None = None
        self._session_timer: threading.Timer | None = None

    def _key(self, run_id: UUID | str) -> str:
        """Normalize a LangChain run id for the active-run map."""

        return str(run_id)

    def _allow_failure(self) -> bool:
        """Whether tool failures should be recorded but not fail the session."""

        return bool(_RUN_OPTIONS.get().get("allow_failure", self.allow_failure))

    def _mark_final(self) -> bool:
        """Whether created LLM spans are decisive for the trace status."""

        return bool(_RUN_OPTIONS.get().get("mark_final", self.mark_final))

    def _parent_span(self, parent_run_id: UUID | str | None) -> Any | None:
        """Resolve a LangChain parent run id to its TraceKit span."""

        # LangChain passes parent_run_id; resolving it here preserves nested span hierarchy.
        if parent_run_id is None:
            return None
        return self._runs.get(self._key(parent_run_id))

    def _schedule_session_close(self) -> None:
        """Schedule idle cleanup for a handler-owned trace."""

        if self._session_timer is not None:
            self._session_timer.cancel()
        timer = threading.Timer(_SESSION_IDLE_SECONDS, self._close_idle_session)
        timer.daemon = True
        self._session_timer = timer
        timer.start()

    def _close_idle_session(self) -> None:
        """Close the managed session once no top-level runs remain."""

        with self._session_lock:
            if self._session_trace is None or self._session_roots:
                return
            trace = self._session_trace
            status = self._session_status
            error = self._session_error
            if self._session_timer is not None:
                self._session_timer.cancel()
            self._session_trace = None
            self._session_roots.clear()
            self._session_root_kinds.clear()
            self._session_status = "ok"
            self._session_error = None
            self._session_timer = None
        if trace is not None and trace.status == "running":
            self.tracer.end_trace(trace, status=status, error=error)

    def close(self) -> None:
        """Close a completed managed session before its tracer is shut down."""

        self._close_idle_session()

    def _touch_session(
        self,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        """Record session activity and carry the first error to final closure."""

        with self._session_lock:
            if self._session_trace is None:
                return
            self._session_last_activity = time.monotonic()
            if status == "error":
                self._session_status = "error"
                if error is not None:
                    self._session_error = error
            self._schedule_session_close()

    def _owns_trace(self, trace: Any | None) -> bool:
        """Ignore ambient traces that belong to a different tracer."""

        return trace is not None and trace.trace_id in self.tracer._traces

    def _register_session_root(self, root_id: str, kind: str) -> None:
        """Attach a top-level LangChain run to the current managed session."""

        with self._session_lock:
            if self._session_trace is None:
                return
            self._session_last_activity = time.monotonic()
            self._session_roots.add(root_id)
            self._session_root_kinds[root_id] = kind
            self._run_roots[root_id] = root_id
            self._schedule_session_close()

    def _session_for(self, root_id: str, kind: str) -> Any | None:
        """Return the shared managed trace, creating one when the session is stale."""

        with self._session_lock:
            now = time.monotonic()
            trace = self._session_trace
            if (
                trace is None
                or trace.status != "running"
                or now - self._session_last_activity > _SESSION_IDLE_SECONDS
            ):
                trace = self.tracer.start_trace(name="langchain-session")
                self._session_trace = trace
                self._session_roots.clear()
                self._session_root_kinds.clear()
                self._session_status = "ok"
                self._session_error = None
            self._session_last_activity = now
            self._session_roots.add(root_id)
            self._session_root_kinds[root_id] = kind
            self._run_roots[root_id] = root_id
            self._schedule_session_close()
            return trace

    def _trace_for(
        self,
        run_id: UUID | str,
        parent_run_id: UUID | str | None,
        parent: Any | None,
        kind: str = "chain",
    ) -> Any | None:
        """Resolve the trace for a callback, even when it runs without ambient context."""

        key = self._key(run_id)
        current = get_current_trace()
        if current is not None and self._owns_trace(current):
            self._run_traces[key] = current
            if (
                current is self._session_trace
                and parent_run_id is None
                and parent is None
            ):
                self._register_session_root(key, kind)
            return current
        if parent is not None:
            trace = getattr(parent, "_tracekit_trace", None)
            if trace is not None:
                self._run_traces[key] = trace
                self._touch_session()
                return trace
        if parent_run_id is not None:
            trace = self._run_traces.get(self._key(parent_run_id))
            if trace is not None and trace.status == "running":
                self._run_traces[key] = trace
                self._touch_session()
                return trace
        trace = self._run_traces.get(key)
        if trace is not None and trace.status == "running":
            self._touch_session()
            return trace
        return self._session_for(key, kind)

    def _record_run(self, run_id: UUID | str, span: Any, trace: Any | None) -> None:
        """Track a started run together with the trace its span belongs to."""

        self._runs[self._key(run_id)] = span
        resolved = trace or getattr(span, "_tracekit_trace", None)
        if resolved is not None:
            self._run_traces[self._key(run_id)] = resolved

    def _finish_run(
        self,
        run_id: UUID | str,
        status: str = "ok",
        error: str | None = None,
        handled: bool = False,
    ) -> Any | None:
        """Pop a completed run's span and trace bookkeeping."""

        key = self._key(run_id)
        root_id = self._run_roots.pop(key, None)
        self._run_traces.pop(key, None)
        if root_id is not None:
            with self._session_lock:
                self._session_roots.discard(root_id)
                kind = self._session_root_kinds.pop(root_id, None)
            if handled:
                self._touch_session()
            else:
                self._touch_session(status, error)
            if kind in ("chain", "llm"):
                self._close_idle_session()
        return self._runs.pop(key, None)

    def wrap(self, target: Any) -> _WrappedTarget:
        """Attach this handler to a target and return a restorable wrapper."""

        # Add this handler without dropping callbacks already attached to the target.
        previous = getattr(target, "callbacks", _MISSING)
        target.callbacks = _callbacks_with_handler(previous, self)
        return _WrappedTarget(target, previous)

    def on_chain_start(
        self,
        serialized: Dict[str, Any] | None,
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start a chain span for a LangChain chain run."""

        name = (serialized or {}).get("name") or (serialized or {}).get("id", ["chain"])[-1] or "chain"
        attrs = {"tags": tags or [], "metadata": metadata or {}, "langchain": {"type": "chain"}}
        parent = self._parent_span(parent_run_id)
        trace = self._trace_for(run_id, parent_run_id, parent, kind="chain")
        span = self.tracer.start_span(
            name=str(name),
            span_type=SpanType.CHAIN,
            trace=trace,
            parent=parent,
            attributes=attrs,
            input=inputs,
        )
        self._record_run(run_id, span, trace)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a chain span with the chain outputs."""

        span = self._finish_run(run_id)
        if span:
            self.tracer.end_span(span, status="ok", output=outputs)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a chain span in the error state."""

        span = self._finish_run(run_id, status="error", error=str(error))
        if span:
            self.tracer.end_span(span, status="error", error=str(error), error_type=type(error).__name__)

    def on_llm_start(
        self,
        serialized: Dict[str, Any] | None,
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        invocation_params: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start an LLM span and retain its prompts for token estimation."""

        name = (serialized or {}).get("name") or "llm"
        model = (((invocation_params or {}).get("model") or (invocation_params or {}).get("model_name")) or (metadata or {}).get("model") or (metadata or {}).get("model_name"))
        attrs = {
            "tags": tags or [],
            "metadata": metadata or {},
            "invocation_params": invocation_params or {},
            "langchain": {"type": "llm"},
        }
        parent = self._parent_span(parent_run_id)
        trace = self._trace_for(run_id, parent_run_id, parent, kind="llm")
        span = self.tracer.start_llm_span(
            name=str(name),
            model=model,
            prompt=prompts,
            messages=prompts,
            attributes=attrs,
            trace=trace,
            parent=parent,
            mark_final=self._mark_final(),
        )
        self._record_run(run_id, span, trace)

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any] | None,
        messages: List[List[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start an LLM span for a LangChain chat-model run."""

        model_name = (serialized or {}).get("name") or "chat_model"
        model_id = (serialized or {}).get("id")
        if model_name == "chat_model" and isinstance(model_id, (list, tuple)) and model_id:
            model_name = str(model_id[-1])
        invocation_params = kwargs.get("invocation_params") or {}
        model = (
            invocation_params.get("model")
            or invocation_params.get("model_name")
            or (metadata or {}).get("model")
            or (metadata or {}).get("model_name")
        )
        chat_messages = _serialize_chat_messages(messages)
        attrs = {
            "tags": tags or [],
            "metadata": metadata or {},
            "invocation_params": invocation_params,
            "langchain": {"type": "chat_model"},
        }
        parent = self._parent_span(parent_run_id)
        trace = self._trace_for(run_id, parent_run_id, parent, kind="llm")
        span = self.tracer.start_llm_span(
            name=str(model_name),
            model=model,
            prompt=chat_messages,
            messages=chat_messages,
            attributes=attrs,
            trace=trace,
            parent=parent,
            mark_final=self._mark_final(),
        )
        self._record_run(run_id, span, trace)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record LLM output, provider usage or estimates, then close the span."""

        span = self._finish_run(run_id)
        if not span:
            return
        text = None
        token_usage: dict[str, int] = {}
        model = span.attributes.get("model") or (getattr(span, "metadata", None) or {}).get("model")
        try:
            generations = getattr(response, "generations", None) or []
            if generations and generations[0]:
                gen0 = generations[0][0]
                text = getattr(gen0, "text", None) or getattr(getattr(gen0, "message", None), "content", None)
            if not text:
                text = span.attributes.get("streaming_output") or None
            llm_output = getattr(response, "llm_output", None) or {}
            token_usage = _extract_token_usage(response)
            model = llm_output.get("model_name") or model
        except Exception:
            text = str(response)
        self.tracer.record_llm_result(
            span,
            response=text if text is not None else str(response),
            model=model,
            token_usage=token_usage,
        )
        self.tracer.end_span(span, status="ok")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record partial LLM usage and close the span in the error state."""

        span = self._finish_run(run_id, status="error", error=str(error))
        if span:
            try:
                self.tracer.record_llm_result(
                    span,
                    response=span.attributes.get("streaming_output") or None,
                    model=span.attributes.get("model"),
                )
            except Exception:
                pass
            self.tracer.end_span(span, status="error", error=str(error), error_type=type(error).__name__)

    def on_tool_start(
        self,
        serialized: Dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        inputs: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start a tool span for a LangChain tool run."""

        name = (serialized or {}).get("name") or "tool"
        attrs = {"tags": tags or [], "metadata": metadata or {}, "langchain": {"type": "tool"}}
        tool_input = inputs if inputs is not None else input_str
        parent = self._parent_span(parent_run_id)
        trace = self._trace_for(run_id, parent_run_id, parent, kind="tool")
        span = self.tracer.start_tool_span(
            name=str(name),
            tool_name=str(name),
            tool_input=tool_input,
            attributes=attrs,
            trace=trace,
            parent=parent,
            allow_failure=self._allow_failure(),
        )
        self._record_run(run_id, span, trace)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record a tool result and close the tool span."""

        span = self._finish_run(run_id)
        if span:
            self.tracer.record_tool_result(span, output=output)
            self.tracer.end_span(span, status="ok")

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record a tool failure and close the tool span."""

        handled = self._allow_failure()
        span = self._finish_run(run_id, status="error", error=str(error), handled=handled)
        if span:
            span.handled = handled
            self.tracer.record_tool_result(span, error=str(error))
            self.tracer.end_span(span, status="error", error=str(error), error_type=type(error).__name__)

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: Any = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: List[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Accumulate streamed LLM tokens on the active span."""

        span = self._runs.get(self._key(run_id))
        if span is None:
            return None
        # Accumulate streamed tokens on the span so the full output survives until export.
        tokens = span.attributes.setdefault("streaming_tokens", [])
        if not isinstance(tokens, list):
            tokens = []
            span.attributes["streaming_tokens"] = tokens
        additions = [token] if isinstance(token, str) else [str(item) for item in token]
        for addition in additions:
            tokens.append(addition)
            span.add_event("llm.new_token", {"token": addition})
        span.attributes["streaming_output"] = "".join(str(item) for item in tokens)
        return None

    def on_retry_start(
        self,
        retry_state: Any,
        *,
        run_id: UUID | str,
        parent_run_id: UUID | str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record a retry attempt and its failure reason on the active span."""

        span = self._runs.get(self._key(run_id))
        if span is None:
            return None
        raw_attempt = getattr(retry_state, "attempt", 1)
        try:
            attempt = int(raw_attempt)
        except (TypeError, ValueError):
            attempt = 1
        exception = getattr(retry_state, "exception", None)
        reason = str(exception) if exception is not None else kwargs.get("reason")
        self.tracer.record_retry(span, attempt=attempt, reason=reason)
        return None

    def on_retry_end(
        self,
        retry_state: Any,
        *,
        run_id: UUID | str,
        parent_run_id: UUID | str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record the outcome of a retry attempt on the active span."""

        span = self._runs.get(self._key(run_id))
        if span is None:
            return None
        raw_attempt = getattr(retry_state, "attempt", 1)
        try:
            attempt = int(raw_attempt)
        except (TypeError, ValueError):
            attempt = 1
        exception = getattr(retry_state, "exception", None)
        event_attributes: dict[str, Any] = {"attempt": attempt, "success": exception is None}
        if exception is not None:
            event_attributes["error"] = str(exception)
        else:
            outcome = getattr(retry_state, "outcome", None)
            if outcome is not None:
                event_attributes["output"] = str(outcome)
        span.add_event("retry.end", event_attributes)
        return None

    # Tracer calls are synchronous, so async callbacks simply delegate to their sync counterparts.
    async def on_chain_start_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_chain_start."""

        return self.on_chain_start(*args, **kwargs)

    async def on_chain_end_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_chain_end."""

        return self.on_chain_end(*args, **kwargs)

    async def on_chain_error_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_chain_error."""

        return self.on_chain_error(*args, **kwargs)

    async def on_llm_start_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_llm_start."""

        return self.on_llm_start(*args, **kwargs)

    async def on_llm_new_token_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_llm_new_token."""

        return self.on_llm_new_token(*args, **kwargs)

    async def on_llm_end_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_llm_end."""

        return self.on_llm_end(*args, **kwargs)

    async def on_llm_error_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_llm_error."""

        return self.on_llm_error(*args, **kwargs)

    async def on_tool_start_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_tool_start."""

        return self.on_tool_start(*args, **kwargs)

    async def on_tool_end_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_tool_end."""

        return self.on_tool_end(*args, **kwargs)

    async def on_tool_error_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_tool_error."""

        return self.on_tool_error(*args, **kwargs)

    async def on_retry_start_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_retry_start."""

        return self.on_retry_start(*args, **kwargs)

    async def on_retry_end_async(self, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper for on_retry_end."""

        return self.on_retry_end(*args, **kwargs)
