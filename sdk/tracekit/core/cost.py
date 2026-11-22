"""Configurable model pricing registry and USD cost aggregation helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_PRICING_SOURCE = "tracekit-builtin"
DEFAULT_PRICING_VERSION = "2026-11-17"
CUSTOM_PRICING_SOURCE = "custom"
USD = "USD"


class UnknownModelError(LookupError):
    """Raised when no pricing is registered for a requested model."""


class InvalidTokenCountError(ValueError):
    """Raised when a token count is not a non-negative integer."""


@dataclass(frozen=True)
class ModelPricing:
    model: str
    input_per_1k: float
    output_per_1k: float
    source: str = DEFAULT_PRICING_SOURCE
    version: str = DEFAULT_PRICING_VERSION
    currency: str = USD

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        for name in ("input_per_1k", "output_per_1k"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if float(value) < 0:
                raise ValueError(f"{name} must be non-negative")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("currency must be a non-empty string")
        # Frozen fields can only be normalized through object.__setattr__ during validation.
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "input_per_1k", float(self.input_per_1k))
        object.__setattr__(self, "output_per_1k", float(self.output_per_1k))


# Built-in rates are USD per 1K tokens and versioned so costs can be traced to a pricing snapshot.
_REFERENCE_RATES: tuple[tuple[str, float, float], ...] = (
    ("gpt-4o", 0.0025, 0.01),
    ("gpt-4o-mini", 0.00015, 0.0006),
    ("gpt-3.5-turbo", 0.0005, 0.0015),
    ("claude-3-5-sonnet", 0.003, 0.015),
    ("claude-3-haiku", 0.00025, 0.00125),
)

DEFAULT_PRICING: dict[str, ModelPricing] = {
    model: ModelPricing(
        model=model,
        input_per_1k=input_per_1k,
        output_per_1k=output_per_1k,
    )
    for model, input_per_1k, output_per_1k in _REFERENCE_RATES
}


def _key(model: Any) -> str:
    if not isinstance(model, str) or not model.strip():
        raise UnknownModelError("model must be a non-empty string")
    return model.strip().lower()


class PricingRegistry:
    """Mutable model pricing registry with explicit source/version metadata."""

    def __init__(
        self,
        custom_pricing: Mapping[str, ModelPricing | tuple[float, float]] | None = None,
        *,
        include_defaults: bool = True,
        source: str = CUSTOM_PRICING_SOURCE,
        version: str = DEFAULT_PRICING_VERSION,
    ) -> None:
        self.source = source
        self.version = version
        self._pricing: dict[str, ModelPricing] = {}
        if include_defaults:
            for model, pricing in DEFAULT_PRICING.items():
                self._pricing[model] = pricing
        for model, custom in (custom_pricing or {}).items():
            # Full ModelPricing entries keep their own provenance; raw tuples inherit this registry's.
            if isinstance(custom, ModelPricing):
                self.register(
                    custom.model,
                    custom.input_per_1k,
                    custom.output_per_1k,
                    source=custom.source,
                    version=custom.version,
                    currency=custom.currency,
                )
            else:
                input_per_1k, output_per_1k = custom
                self.register(
                    model,
                    input_per_1k,
                    output_per_1k,
                    source=source,
                    version=version,
                )

    def register(
        self,
        model: str,
        input_per_1k: float,
        output_per_1k: float,
        *,
        source: str = CUSTOM_PRICING_SOURCE,
        version: str = DEFAULT_PRICING_VERSION,
        currency: str = USD,
    ) -> ModelPricing:
        pricing = ModelPricing(
            model=model,
            input_per_1k=input_per_1k,
            output_per_1k=output_per_1k,
            source=source,
            version=version,
            currency=currency,
        )
        # Keys are lowercased for lookup, while pricing.model preserves the caller's original casing.
        self._pricing[_key(model)] = pricing
        return pricing

    def unregister(self, model: str) -> None:
        self._pricing.pop(_key(model), None)

    def get(self, model: str) -> ModelPricing:
        try:
            # Translating KeyError keeps unknown pricing distinct from malformed token input.
            return self._pricing[_key(model)]
        except KeyError:
            raise UnknownModelError(f"no pricing registered for model {model!r}") from None

    def supports(self, model: str) -> bool:
        try:
            return _key(model) in self._pricing
        except UnknownModelError:
            return False

    def models(self) -> tuple[str, ...]:
        return tuple(sorted(pricing.model for pricing in self._pricing.values()))

    def entries(self) -> tuple[ModelPricing, ...]:
        return tuple(sorted(self._pricing.values(), key=lambda pricing: pricing.model))


_DEFAULT_REGISTRY = PricingRegistry()


def default_registry() -> PricingRegistry:
    return _DEFAULT_REGISTRY


def register_pricing(
    model: str,
    input_per_1k: float,
    output_per_1k: float,
    *,
    source: str = CUSTOM_PRICING_SOURCE,
    version: str = DEFAULT_PRICING_VERSION,
    currency: str = USD,
    registry: PricingRegistry | None = None,
) -> ModelPricing:
    target = registry or default_registry()
    return target.register(
        model,
        input_per_1k,
        output_per_1k,
        source=source,
        version=version,
        currency=currency,
    )


def _normalize_tokens(value: Any, *, field_name: str) -> int:
    if value is None:
        return 0
    # bool is an int subclass, so it must be rejected before the int branch.
    if isinstance(value, bool):
        raise InvalidTokenCountError(f"{field_name} must be a non-negative integer")
    if isinstance(value, int):
        tokens = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise InvalidTokenCountError(f"{field_name} must be a whole number")
        tokens = int(value)
    elif isinstance(value, str):
        try:
            tokens = int(value.strip())
        except ValueError:
            raise InvalidTokenCountError(f"{field_name} must be a non-negative integer") from None
    else:
        raise InvalidTokenCountError(f"{field_name} must be a non-negative integer")
    if tokens < 0:
        raise InvalidTokenCountError(f"{field_name} must be non-negative")
    return tokens


def _cost_from_pricing(pricing: ModelPricing, input_tokens: int, output_tokens: int) -> float:
    cost = (
        input_tokens / 1000.0 * pricing.input_per_1k
        + output_tokens / 1000.0 * pricing.output_per_1k
    )
    # Rounding curbs floating-point drift when costs are summed later.
    return round(cost, 12)


def calculate_cost(
    model: str,
    input_tokens: Any = None,
    output_tokens: Any = None,
    *,
    registry: PricingRegistry | None = None,
) -> float:
    # Omitting the registry uses the shared global one, so global registrations affect all callers.
    target = registry or default_registry()
    pricing = target.get(model)
    return _cost_from_pricing(
        pricing,
        _normalize_tokens(input_tokens, field_name="input_tokens"),
        _normalize_tokens(output_tokens, field_name="output_tokens"),
    )


def try_calculate_cost(
    model: str,
    input_tokens: Any = None,
    output_tokens: Any = None,
    *,
    registry: PricingRegistry | None = None,
) -> float | None:
    # Both unknown models and bad token counts degrade to None so cost annotation never breaks instrumentation.
    try:
        return calculate_cost(model, input_tokens, output_tokens, registry=registry)
    except (UnknownModelError, InvalidTokenCountError):
        return None


def set_span_cost(
    span: Any,
    model: str,
    input_tokens: Any = None,
    output_tokens: Any = None,
    *,
    registry: PricingRegistry | None = None,
    overwrite: bool = True,
) -> float:
    target = registry or default_registry()
    pricing = target.get(model)
    inputs = _normalize_tokens(input_tokens, field_name="input_tokens")
    outputs = _normalize_tokens(output_tokens, field_name="output_tokens")
    cost = _cost_from_pricing(pricing, inputs, outputs)
    attributes = span.attributes
    if overwrite or "cost_usd" not in attributes:
        attributes["cost_usd"] = cost
    # Provenance is always refreshed even when overwrite=False, so details match current rates.
    attributes["cost_details"] = {
        "model": pricing.model,
        "input_tokens": inputs,
        "output_tokens": outputs,
        "input_per_1k": pricing.input_per_1k,
        "output_per_1k": pricing.output_per_1k,
        "source": pricing.source,
        "version": pricing.version,
        "currency": pricing.currency,
    }
    return cost


def span_cost(span: Any) -> float | None:
    value = getattr(span, "attributes", {}).get("cost_usd")
    # Booleans and non-numeric values are treated as missing rather than raising.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def sum_span_costs(spans: Iterable[Any]) -> float:
    # Spans without a valid cost are skipped, so partial instrumentation understates rather than fails.
    costs = (cost for span in spans if (cost := span_cost(span)) is not None)
    return round(sum(costs), 12)


def set_trace_total_cost(
    trace: Any,
    spans: Iterable[Any],
    *,
    overwrite: bool = True,
) -> float:
    total = sum_span_costs(spans)
    if overwrite or "total_cost_usd" not in trace.metadata:
        trace.metadata["total_cost_usd"] = total
    return total


def aggregate_trace_cost(
    trace: Any,
    spans: Iterable[Any],
    *,
    overwrite: bool = True,
) -> float:
    return set_trace_total_cost(trace, spans, overwrite=overwrite)


def add_trace_cost(trace: Any, amount: Any) -> float:
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise TypeError("amount must be a number")
    current = trace.metadata.get("total_cost_usd", 0.0)
    # A malformed existing total is treated as zero instead of propagating bad metadata.
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        current = 0.0
    total = round(float(current) + float(amount), 12)
    trace.metadata["total_cost_usd"] = total
    return total


def add_span_cost_to_trace(trace: Any, span: Any) -> float:
    cost = span_cost(span)
    # A span with no recorded cost leaves the trace total unchanged.
    if cost is None:
        return float(trace.metadata.get("total_cost_usd") or 0.0)
    return add_trace_cost(trace, cost)
