"""LangChain callback handler integration for Tracekit."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

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

_extract_token_usage = extract_token_usage


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

    def __init__(self, tracer: Optional[Tracer] = None) -> None:
        """Create a callback handler that writes spans through the given tracer."""

        self.tracer = tracer or Tracer()
        # Maps LangChain run ids to active spans so end/error callbacks can close the right one.
        self._runs: Dict[str, Any] = {}

    def _key(self, run_id: UUID | str) -> str:
        """Normalize a LangChain run id for the active-run map."""

        return str(run_id)

    def _parent_span(self, parent_run_id: UUID | str | None) -> Any | None:
        """Resolve a LangChain parent run id to its TraceKit span."""

        # LangChain passes parent_run_id; resolving it here preserves nested span hierarchy.
        if parent_run_id is None:
            return None
        return self._runs.get(self._key(parent_run_id))

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
        span = self.tracer.start_span(
            name=str(name),
            span_type=SpanType.CHAIN,
            attributes=attrs,
            input=inputs,
            parent=self._parent_span(parent_run_id),
        )
        self._runs[self._key(run_id)] = span

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a chain span with the chain outputs."""

        span = self._runs.pop(self._key(run_id), None)
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

        span = self._runs.pop(self._key(run_id), None)
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
        span = self.tracer.start_llm_span(name=str(name), model=model, prompt=prompts, messages=prompts, attributes=attrs, parent=self._parent_span(parent_run_id))
        self._runs[self._key(run_id)] = span

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record LLM output, provider usage or estimates, then close the span."""

        span = self._runs.pop(self._key(run_id), None)
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

        span = self._runs.pop(self._key(run_id), None)
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
        span = self.tracer.start_tool_span(name=str(name), tool_name=str(name), tool_input=tool_input, attributes=attrs, parent=self._parent_span(parent_run_id))
        self._runs[self._key(run_id)] = span

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Record a tool result and close the tool span."""

        span = self._runs.pop(self._key(run_id), None)
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

        span = self._runs.pop(self._key(run_id), None)
        if span:
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
