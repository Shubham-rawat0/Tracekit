"""Reusable helpers for LLM token usage extraction, estimation, and aggregation."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Mapping
from typing import Any

from tracekit.core.config import load_config

TOKEN_USAGE_PROVIDER = "provider"
TOKEN_USAGE_ESTIMATED = "estimated"

_TOKEN_ALIASES = {
    "prompttokens": "input_tokens",
    "inputtokens": "input_tokens",
    "prompttokencount": "input_tokens",
    "inputtokencount": "input_tokens",
    "genaiusageinputtokens": "input_tokens",
    "promptevalcount": "input_tokens",
    "completiontokens": "output_tokens",
    "outputtokens": "output_tokens",
    "completiontokencount": "output_tokens",
    "outputtokencount": "output_tokens",
    "candidatestokencount": "output_tokens",
    "genaiusageoutputtokens": "output_tokens",
    "evalcount": "output_tokens",
    "totaltokens": "total_tokens",
    "totaltokencount": "total_tokens",
    "tokencount": "total_tokens",
    "genaiusagetotaltokens": "total_tokens",
}

_TOKEN_CONTAINER_NAMES = (
    "usage_metadata",
    "usage",
    "token_usage",
    "llm_output",
    "response_metadata",
    "generation_info",
    "message",
    "generations",
    "raw",
)

_CANONICAL_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _coerce_count(value: Any) -> int | None:
    """Return a non-negative integer count, or None for invalid values."""

    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 0 else None


def _normalized_key(key: Any) -> str:
    """Normalize provider key spellings for alias lookup."""

    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _extract(source: Any, usage: dict[str, int], seen: set[int]) -> None:
    """Walk provider metadata recursively and collect canonical token counts."""

    if source is None or isinstance(source, (str, bytes, int, float, bool)):
        return
    marker = id(source)
    if marker in seen:
        return
    seen.add(marker)

    if isinstance(source, Mapping):
        items: Iterable[tuple[Any, Any]] = source.items()
    elif isinstance(source, (list, tuple, set)):
        items = ((None, item) for item in source)
    else:
        named = []
        for name in _TOKEN_CONTAINER_NAMES:
            if hasattr(source, name):
                named.append((name, getattr(source, name)))
        raw = getattr(source, "__dict__", None)
        if isinstance(raw, dict):
            named.extend(raw.items())
        items = named

    for key, value in items:
        destination = _TOKEN_ALIASES.get(_normalized_key(key)) if key is not None else None
        if destination is not None and not isinstance(value, (Mapping, list, tuple, set)):
            count = _coerce_count(value)
            if count is not None:
                usage.setdefault(destination, count)
            continue
        _extract(value, usage, seen)


def extract_token_usage(source: Any) -> dict[str, int]:
    """Extract canonical token counts from provider response metadata."""

    usage: dict[str, int] = {}
    _extract(source, usage, set())
    if "total_tokens" not in usage:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = input_tokens + output_tokens
    return {key: usage[key] for key in _CANONICAL_KEYS if key in usage}


def normalize_provider_usage(
    *,
    input_tokens: Any = None,
    output_tokens: Any = None,
    total_tokens: Any = None,
    sources: tuple[Any, ...] = (),
) -> dict[str, int]:
    """Combine explicit counts and nested metadata into canonical provider usage."""

    usage: dict[str, int] = {}
    for source in sources:
        for key, value in extract_token_usage(source).items():
            usage.setdefault(key, value)
    for key, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
    ):
        count = _coerce_count(value)
        if count is not None:
            usage[key] = count
    if "total_tokens" not in usage and "input_tokens" in usage and "output_tokens" in usage:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return {key: usage[key] for key in _CANONICAL_KEYS if key in usage}


def finalize_provider_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Fill absent partial counts with zero and attach the canonical source."""

    input_tokens = _coerce_count(usage.get("input_tokens")) or 0
    output_tokens = _coerce_count(usage.get("output_tokens")) or 0
    total_tokens = _coerce_count(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "token_usage_source": TOKEN_USAGE_PROVIDER,
    }


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if not value:
            return ""
        return json.dumps(value, default=str, sort_keys=True)
    if isinstance(value, (list, tuple, set)):
        if not value:
            return ""
        return "\n".join(_coerce_text(item) for item in value)
    return str(value)


def _jitter_amount(jitter: float | None) -> float:
    if jitter is None:
        jitter = float(load_config(dotenv_path=None).token_estimation_jitter)
    try:
        return min(1.0, max(0.0, float(jitter)))
    except (TypeError, ValueError):
        return 0.10


def estimate_text_tokens(value: Any, jitter: float | None = None) -> int:
    """Estimate token count as len(text) / 4 with optional symmetric jitter."""

    text = _coerce_text(value)
    if not text:
        return 0
    base = len(text) / 4
    amount = _jitter_amount(jitter)
    factor = 1.0 if amount == 0 else random.uniform(1.0 - amount, 1.0 + amount)
    return max(0, int(round(base * factor)))


def estimate_token_usage(
    input_value: Any,
    output_value: Any,
    jitter: float | None = None,
) -> dict[str, Any]:
    """Estimate input and output tokens independently."""

    input_tokens = estimate_text_tokens(input_value, jitter)
    output_tokens = estimate_text_tokens(output_value, jitter)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "token_usage_source": TOKEN_USAGE_ESTIMATED,
    }


def is_llm_span(span: Any) -> bool:
    """Return True for LLM-shaped spans without requiring a SpanType import."""

    value = getattr(span, "span_type", None)
    text = str(getattr(value, "value", value)).lower()
    return text in {"llm", "generation", "chat", "completion"}


def aggregate_token_usage(usages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-span token usage without estimating or re-reading text."""

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    sources: set[str] = set()
    found = False
    for usage in usages:
        if not usage:
            continue
        found = True
        input_tokens += _coerce_count(usage.get("input_tokens")) or 0
        output_tokens += _coerce_count(usage.get("output_tokens")) or 0
        total_tokens += _coerce_count(usage.get("total_tokens")) or 0
        source = usage.get("token_usage_source")
        if source in {TOKEN_USAGE_PROVIDER, TOKEN_USAGE_ESTIMATED}:
            sources.add(str(source))
    if not found:
        return {}
    if TOKEN_USAGE_PROVIDER in sources:
        source = TOKEN_USAGE_PROVIDER
    elif TOKEN_USAGE_ESTIMATED in sources:
        source = TOKEN_USAGE_ESTIMATED
    else:
        source = TOKEN_USAGE_PROVIDER
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "token_usage_source": source,
    }
