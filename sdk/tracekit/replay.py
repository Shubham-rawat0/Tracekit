from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ReplayRequest:
    trace_id: str
    span_id: str | None = None
    prompt: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayResult:
    output: Any = None
    status: str = "ok"
    model: str | None = None
    provider: str | None = None
    token_usage: dict[str, Any] | None = None
    latency_ms: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


ReplayHandler = Callable[[ReplayRequest], ReplayResult | dict[str, Any] | Awaitable[Any]]

# Handlers are process-global: registration affects every replay executed in this interpreter.
_HANDLERS: dict[str, ReplayHandler] = {}


def register_replay_handler(provider: str, handler: ReplayHandler) -> None:
    # Normalizing names makes lookups case-insensitive; empty/None providers become the "*" catch-all.
    key = str(provider or "*").strip().lower()
    if not callable(handler):
        raise TypeError("replay handler must be callable")
    _HANDLERS[key] = handler


def unregister_replay_handler(provider: str) -> bool:
    return _HANDLERS.pop(str(provider or "*").strip().lower(), None) is not None


def list_replay_providers() -> list[str]:
    return sorted(_HANDLERS)


def clear_replay_handlers() -> None:
    _HANDLERS.clear()


# Extra mapping keys are dropped so loose handler dicts still fit the fixed result shape.
def _coerce_result(value: Any) -> ReplayResult:
    if isinstance(value, ReplayResult):
        return value
    if not isinstance(value, dict):
        raise TypeError("replay handler must return a ReplayResult or mapping")
    allowed = {key for key in ReplayResult.__dataclass_fields__}
    return ReplayResult(**{key: item for key, item in value.items() if key in allowed})


async def execute_replay(request: ReplayRequest) -> ReplayResult:
    provider = str(request.provider or "*").strip().lower()
    # A provider-specific handler wins; the "*" handler covers providers without their own registration.
    handler = _HANDLERS.get(provider) or _HANDLERS.get("*")
    if handler is None:
        raise LookupError(f"no replay handler registered for provider {request.provider or 'unknown'!r}")
    result = handler(request)
    # Sync and async handlers share one entry point by awaiting only awaitable results.
    if inspect.isawaitable(result):
        result = await result
    return _coerce_result(result)
