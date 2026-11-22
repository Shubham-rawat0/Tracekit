"""Auto-instrumentation for LangChain."""

from __future__ import annotations

from typing import Any

from ..core.tracer import Tracer
from .langchain import TracekitCallbackHandler, _callbacks_with_handler

__all__ = ["trace_langchain", "untrace_langchain"]

_PATCHED = False
_ORIGINAL_INITS: dict[Any, Any] = {}
_LANGCHAIN_IMPORT_ERROR: ImportError | None = None
_TRACER: Tracer | None = None
_BaseLLM: Any = None
_BaseChain: Any = None
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
        from langchain_core.chains.base import BaseChain

        _BaseChain = BaseChain
    except ImportError:
        _BaseChain = None

    try:
        from langchain_core.tools.base import BaseTool

        _BaseTool = BaseTool
    except ImportError:
        _BaseTool = None


def trace_langchain(
    tracer: Tracer | None = None,
    flush: bool | None = None,
    auto_flush: bool | None = None,
    **tracer_kwargs: Any,
) -> Tracer:
    """Instrument LangChain constructors so new instances are traced automatically.

    ``flush`` is a friendly alias for ``auto_flush``. Passing both is allowed
    only when they agree. When ``tracer`` is supplied it owns all construction
    options, so ``flush``, ``auto_flush``, and other ``Tracer`` keyword
    arguments are rejected.
    """
    global _PATCHED, _TRACER

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

    handler = TracekitCallbackHandler(tracer=tracer)

    for cls in (_BaseLLM, _BaseChain, _BaseTool):
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

    _TRACER = tracer
    _PATCHED = True
    return tracer


def untrace_langchain() -> None:
    """Restore original LangChain constructors and reset instrumentation."""
    global _PATCHED, _TRACER

    for cls, original_init in _ORIGINAL_INITS.items():
        cls.__init__ = original_init

    _ORIGINAL_INITS.clear()
    _TRACER = None
    _PATCHED = False
