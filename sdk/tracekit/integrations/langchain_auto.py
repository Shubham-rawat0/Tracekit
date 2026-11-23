"""Auto-instrumentation for LangChain."""

from __future__ import annotations

import inspect
from typing import Any

from ..core.tracer import Tracer
from .langchain import (
    TracekitCallbackHandler,
    _callbacks_with_handler,
    _reset_run_options,
    _set_run_options,
)

__all__ = ["trace_langchain", "untrace_langchain"]

_PATCHED = False
_ORIGINAL_INITS: dict[Any, Any] = {}
_ORIGINAL_METHODS: dict[tuple[Any, str], Any] = {}
_LANGCHAIN_IMPORT_ERROR: ImportError | None = None
_TRACER: Tracer | None = None
_HANDLER: TracekitCallbackHandler | None = None
_BaseLLM: Any = None
_BaseChatModel: Any = None
_BaseChain: Any = None
_ClassicChain: Any = None
_BaseTool: Any = None

try:
    __import__("langchain_core")
except ImportError as e:
    _LANGCHAIN_IMPORT_ERROR = e
else:
    try:
        from langchain_core.language_models.llms import BaseLLM

        _BaseLLM = BaseLLM
    except ImportError:
        _BaseLLM = None

    try:
        from langchain_core.language_models.chat_models import BaseChatModel

        _BaseChatModel = BaseChatModel
    except ImportError:
        _BaseChatModel = None

    try:
        from langchain_core.chains.base import BaseChain

        _BaseChain = BaseChain
    except ImportError:
        _BaseChain = None

    try:
        from langchain_classic.chains.base import Chain

        _ClassicChain = Chain
    except ImportError:
        _ClassicChain = None

    try:
        from langchain_core.tools.base import BaseTool

        _BaseTool = BaseTool
    except ImportError:
        _BaseTool = None


_RUN_OPTION_KEYS = ("allow_failure", "mark_final")


def _pop_run_options(kwargs: dict[str, Any]) -> dict[str, bool]:
    """Extract TraceKit run flags so LangChain never sees them."""

    return {key: bool(kwargs.pop(key)) for key in _RUN_OPTION_KEYS if key in kwargs}


def _make_patched_tool_method(original: Any, handler: TracekitCallbackHandler) -> Any:
    if inspect.iscoroutinefunction(original):

        async def patched_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            self.callbacks = _callbacks_with_handler(
                getattr(self, "callbacks", None), handler
            )
            options = _pop_run_options(kwargs)
            token = _set_run_options(options) if options else None
            try:
                return await original(self, *args, **kwargs)
            finally:
                if token is not None:
                    _reset_run_options(token)

        return patched_async

    def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        self.callbacks = _callbacks_with_handler(
            getattr(self, "callbacks", None), handler
        )
        options = _pop_run_options(kwargs)
        token = _set_run_options(options) if options else None
        try:
            return original(self, *args, **kwargs)
        finally:
            if token is not None:
                _reset_run_options(token)

    return patched


def _instrument_tool_runs(handler: TracekitCallbackHandler) -> None:
    if _BaseTool is None:
        return
    for method_name in ("run", "arun"):
        original = getattr(_BaseTool, method_name, None)
        if original is None:
            continue
        _ORIGINAL_METHODS[(_BaseTool, method_name)] = original
        setattr(
            _BaseTool,
            method_name,
            _make_patched_tool_method(original, handler),
        )


def trace_langchain(
    tracer: Tracer | None = None,
    flush: bool | None = None,
    auto_flush: bool | None = None,
    allow_failure: bool = True,
    mark_final: bool = False,
    **tracer_kwargs: Any,
) -> Tracer:
    """Instrument LangChain constructors so new instances are traced automatically.

    ``flush`` is a friendly alias for ``auto_flush``. Passing both is allowed
    only when they agree. When ``tracer`` is supplied it owns all construction
    options, so ``flush``, ``auto_flush``, and other ``Tracer`` keyword
    arguments are rejected.
    """
    global _PATCHED, _TRACER, _HANDLER

    if (
        flush is not None
        and auto_flush is not None
        and bool(flush) != bool(auto_flush)
    ):
        raise ValueError(
            "flush and auto_flush disagree; pass the same value for both "
            "or provide only one of them"
        )

    if tracer is not None:
        conflicting = []
        if flush is not None:
            conflicting.append("flush")
        if auto_flush is not None:
            conflicting.append("auto_flush")
        if tracer_kwargs:
            conflicting.extend(sorted(tracer_kwargs))
        if conflicting:
            raise ValueError(
                "tracer= already owns its configuration; remove: "
                + ", ".join(conflicting)
            )

    # idempotent guard: repeated calls must not stack patches on the same classes.
    if _PATCHED:
        assert _TRACER is not None
        return _TRACER

    if _LANGCHAIN_IMPORT_ERROR is not None:
        raise ImportError(
            "langchain-core is required for auto-instrumentation. "
            "Install with: pip install tracekit[langchain]"
        ) from _LANGCHAIN_IMPORT_ERROR

    if tracer is None:
        if flush is not None:
            resolved_auto_flush = bool(flush)
        elif auto_flush is not None:
            resolved_auto_flush = bool(auto_flush)
        else:
            resolved_auto_flush = False
        tracer = Tracer(auto_flush=resolved_auto_flush, **tracer_kwargs)

    handler = TracekitCallbackHandler(
        tracer=tracer,
        allow_failure=allow_failure,
        mark_final=mark_final,
    )

    for cls in (_BaseLLM, _BaseChatModel, _BaseChain, _ClassicChain, _BaseTool):
        if cls is None:
            continue

        original_init = cls.__init__
        # Keep the untouched __init__ so untrace_langchain can restore it exactly.
        _ORIGINAL_INITS[cls] = original_init

        # Patching constructors covers instances created after instrumentation, not just existing ones.
        def make_patched_init(original: Any) -> Any:
            """Build a constructor patch bound to one class's original init."""

            # Factory closes over this class's original to avoid loop-variable late binding.
            def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
                """Inject the TraceKit callback handler into a new LangChain instance."""

                original(self, *args, **kwargs)

                existing = kwargs.get("callbacks")
                if existing is None:
                    existing = getattr(self, "callbacks", None)

                callbacks = _callbacks_with_handler(existing, handler)
                # Set both attribute and kwargs: callback consumers may read either path.
                self.callbacks = callbacks
                kwargs["callbacks"] = callbacks

            return patched_init

        cls.__init__ = make_patched_init(original_init)

    _instrument_tool_runs(handler)

    _TRACER = tracer
    _HANDLER = handler
    _PATCHED = True
    return tracer


def untrace_langchain() -> None:
    """Restore original LangChain constructors and reset instrumentation."""
    global _PATCHED, _TRACER, _HANDLER

    if _HANDLER is not None:
        _HANDLER.close()

    for cls, original_init in _ORIGINAL_INITS.items():
        cls.__init__ = original_init

    _ORIGINAL_INITS.clear()

    for (cls, method_name), original in _ORIGINAL_METHODS.items():
        setattr(cls, method_name, original)

    _ORIGINAL_METHODS.clear()
    _TRACER = None
    _HANDLER = None
    _PATCHED = False
