from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        # Accept space-separated timestamps as well as ISO "T" separators.
        text = value.strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    # Naive timestamps are assumed UTC so all buckets share one timeline.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Instrumentations disagree on span names/types, so LLM detection combines several heuristics.
_LLM_SPAN_TYPES = {
    "llm",
    "generation",
    "chat",
    "completion",
    "text_completion",
    "text_generation",
    "model_call",
    "openai",
    "anthropic",
    "bedrock",
    "gemini",
}
# Explicit non-LLM types take precedence over the name/attribute heuristics below.
_NON_LLM_SPAN_TYPES = {"tool", "retriever"}
_LLM_NAME_MARKERS = (
    "chat",
    "completion",
    "llm",
    "openai",
    "anthropic",
    "gemini",
    "invoke_model",
    "generate",
)
# Token keys vary by SDK casing/spelling; they are normalized before comparisons.
_TOKEN_INPUT_KEYS = {
    "input_tokens",
    "prompt_tokens",
    "input_token_count",
    "prompt_token_count",
    "prompt_eval_count",
    "inputtokens",
    "prompttokens",
    "prompttoken",
    "inputtoken",
}
_TOKEN_OUTPUT_KEYS = {
    "output_tokens",
    "completion_tokens",
    "output_token_count",
    "completion_token_count",
    "eval_count",
    "outputtokens",
    "completiontokens",
    "completiontoken",
    "outputtoken",
}
_TOKEN_TOTAL_KEYS = {
    "total_tokens",
    "total_token_count",
    "token_count",
    "totaltokens",
    "totaltoken",
}


def _span_type(span: dict[str, Any]) -> str:
    value = _first(span.get("span_type"), span.get("type"))
    if value is None:
        return "internal"
    return str(getattr(value, "value", value)).strip().lower()


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    return _dict(span.get("attributes"))


def _metadata_dict(span: dict[str, Any]) -> dict[str, Any]:
    return _dict(span.get("metadata"))


def _is_llm_span(span: dict[str, Any]) -> bool:
    span_type = _span_type(span)
    if span_type in _NON_LLM_SPAN_TYPES:
        return False
    if span_type in _LLM_SPAN_TYPES:
        return True
    name = str(span.get("name") or "").strip().lower()
    if any(marker in name for marker in _LLM_NAME_MARKERS):
        return True
    # Attributes/metadata are the last resort when type and name are ambiguous.
    for container in (_attributes(span), _metadata_dict(span)):
        for key, value in container.items():
            key_text = str(key).strip().lower()
            if key_text.startswith("gen_ai."):
                return True
            if key_text in {"llm.provider", "llm.model_name", "llm.request.model", "llm.response.model"}:
                return True
            if key_text in {"model", "model_name"} and isinstance(value, str) and value.strip():
                if span_type in {"", "internal", "unknown"} or span.get("input") is not None or span.get("output") is not None:
                    return True
    return False


def _metadata(trace: dict[str, Any]) -> dict[str, Any]:
    return _dict(trace.get("metadata"))


def _llm_spans(spans: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [span for span in spans if _is_llm_span(span)]


def _duration_ms(trace: dict[str, Any]) -> float | None:
    return _number(
        _first(
            trace.get("duration_ms"),
            trace.get("latency_ms"),
            trace.get("latency"),
            _metadata(trace).get("latency_ms"),
        )
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Interpolate between adjacent ranks rather than rounding to the nearest sample.
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _token_key(text: str) -> str:
    # Strip punctuation and case so "prompt_tokens" and "promptTokens" collapse.
    return "".join(ch for ch in str(text).strip().lower() if ch.isalnum())


def _token_bucket(key: str) -> str | None:
    raw = str(key).strip().lower()
    normalized = _token_key(raw)
    input_keys = {_token_key(k) for k in _TOKEN_INPUT_KEYS}
    output_keys = {_token_key(k) for k in _TOKEN_OUTPUT_KEYS}
    total_keys = {_token_key(k) for k in _TOKEN_TOTAL_KEYS}
    if normalized in input_keys:
        return "input_tokens"
    if normalized in output_keys:
        return "output_tokens"
    if normalized in total_keys:
        return "total_tokens"
    if raw.startswith("gen_ai.usage.") or normalized.startswith("genaiusage"):
        suffix = raw.rsplit(".", 1)[-1]
        suffix_key = _token_key(suffix)
        if suffix_key in input_keys:
            return "input_tokens"
        if suffix_key in output_keys:
            return "output_tokens"
        if suffix_key in total_keys:
            return "total_tokens"
    return None


def _extract_token_usage(source: Any, _seen: set[int] | None = None) -> dict[str, int]:
    # Usage may be nested anywhere; seen ids prevent cycles in self-referential payloads.
    if _seen is None:
        _seen = set()
    if isinstance(source, dict):
        source_id = id(source)
        if source_id in _seen:
            return {}
        _seen.add(source_id)
        found: dict[str, int] = {}
        for key, value in source.items():
            bucket = _token_bucket(str(key))
            if bucket is not None:
                number = _integer(value)
                if number is not None:
                    found[bucket] = number
                    continue
            if isinstance(value, (dict, list, tuple)):
                nested = _extract_token_usage(value, _seen)
                for nested_key, nested_value in nested.items():
                    # First nested occurrence wins for a given bucket.
                    found.setdefault(nested_key, nested_value)
        return found
    if isinstance(source, (list, tuple)):
        source_id = id(source)
        if source_id in _seen:
            return {}
        _seen.add(source_id)
        found = {}
        for item in source:
            nested = _extract_token_usage(item, _seen)
            for nested_key, nested_value in nested.items():
                found.setdefault(nested_key, nested_value)
        return found
    return {}


def _merge_tokens(target: dict[str, Any], source: Any) -> bool:
    usage = _extract_token_usage(source)
    if not usage:
        return False
    # Later sources overwrite earlier ones when they report the same bucket.
    for key, value in usage.items():
        target[key] = value
        target[f"{key}_available"] = True
    return True


def extract_fields(trace: dict[str, Any], spans: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = _metadata(trace)
    llms = _llm_spans(spans)
    first_llm = llms[0] if llms else {}
    last_llm = llms[-1] if llms else {}
    first_attrs = _attributes(first_llm)
    last_attrs = _attributes(last_llm)

    tokens: dict[str, Any] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "input_tokens_available": False,
        "output_tokens_available": False,
        "total_tokens_available": False,
    }
    token_found = False
    # Probe trace, metadata and LLM spans for usage; the last source with a bucket wins.
    token_sources: list[Any] = [
        trace,
        _dict(trace.get("attributes")),
        {"token_count": trace.get("token_count")},
        {"total_tokens": metadata.get("total_tokens")},
        metadata.get("token_usage"),
        metadata.get("usage"),
        metadata.get("usage_metadata"),
        metadata.get("llm.usage"),
    ]
    for span in llms:
        attrs = _attributes(span)
        span_meta = _metadata_dict(span)
        token_sources.extend(
            [
                span,
                attrs,
                span_meta,
                attrs.get("token_usage"),
                attrs.get("usage"),
                attrs.get("usage_metadata"),
                attrs.get("llm.usage"),
                span_meta.get("token_usage"),
                span_meta.get("usage"),
                span_meta.get("usage_metadata"),
                span_meta.get("llm.usage"),
            ]
        )
    for source in token_sources:
        if _merge_tokens(tokens, source):
            token_found = True

    # Synthesize a total when only input/output counts were recorded.
    if (
        tokens["total_tokens"] is None
        and tokens["input_tokens"] is not None
        and tokens["output_tokens"] is not None
    ):
        tokens["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
        tokens["total_tokens_available"] = True
        token_found = True

    cost_found = False
    cost_usd: float | None = None
    # Prefer a trace-level cost; otherwise sum costs recorded on individual spans.
    trace_cost = _number(_first(metadata.get("total_cost_usd"), trace.get("cost_usd")))
    if trace_cost is not None:
        cost_usd = trace_cost
        cost_found = True
    else:
        for span in spans:
            attrs = _attributes(span)
            span_cost = _number(
                _first(attrs.get("cost_usd"), _dict(attrs.get("cost_details")).get("total_cost"))
            )
            if span_cost is not None:
                cost_usd = (cost_usd or 0.0) + span_cost
                cost_found = True

    # Errors surface as exception-named events or events carrying exception.* attributes.
    error_events: list[dict[str, Any]] = []
    for container in [trace, *spans]:
        for event in _list(container.get("events")):
            item = _dict(event)
            if str(item.get("name") or "").lower() == "exception":
                error_events.append(item)
            elif any(str(key).startswith("exception.") for key in _dict(item.get("attributes"))):
                error_events.append(item)

    exception_type: str | None = None
    exception_message: str | None = None
    stacktrace: str | None = None
    if error_events:
        # The most recent error event supplies type, message and stacktrace.
        event_attrs = _dict(error_events[-1].get("attributes"))
        exception_type = _text(
            _first(event_attrs.get("exception.type"), trace.get("error_type"))
        )
        exception_message = _text(
            _first(event_attrs.get("exception.message"), trace.get("error"))
        )
        stacktrace = _text(event_attrs.get("exception.stacktrace"))

    model = _text(
        _first(
            trace.get("model_name"),
            metadata.get("model"),
            metadata.get("model_name"),
            first_attrs.get("model"),
            last_attrs.get("model"),
            # Gemini stores model in ls_model_name (LangChain convention)
            _first(
                first_attrs.get("metadata", {}).get("ls_model_name"),
                last_attrs.get("metadata", {}).get("ls_model_name"),
            ),
            # Also check invocation_params.model as fallback
            _first(
                first_attrs.get("invocation_params", {}).get("model"),
                last_attrs.get("invocation_params", {}).get("model"),
            ),
        )
    )
    provider = _text(
        _first(
            metadata.get("provider"),
            metadata.get("llm_provider"),
            first_attrs.get("provider"),
            last_attrs.get("provider"),
        )
    )

    input_value = _first(trace.get("prompt"), metadata.get("prompt"), first_llm.get("input"))
    output_value = _first(trace.get("response"), metadata.get("response"), last_llm.get("output"))
    parameters_source = _first(
        first_attrs.get("generation_params"),
        first_attrs.get("invocation_params"),
        metadata.get("generation_params"),
        metadata.get("invocation_params"),
    )
    parameters = _dict(parameters_source)
    # Only known generation settings are exposed; arbitrary params are dropped.
    allowed_parameters = (
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_output_tokens",
        "stop",
        "frequency_penalty",
        "presence_penalty",
    )
    generation_params = {
        key: parameters[key] for key in allowed_parameters if key in parameters
    }

    return {
        "duration_ms": _duration_ms(trace),
        "model": model,
        "provider": provider,
        "input": input_value,
        "output": output_value,
        "tokens": tokens,
        "tokens_available": token_found,
        "cost_usd": cost_usd,
        "cost_available": cost_found,
        "error_type": _text(
            _first(trace.get("error_type"), exception_type, _first(
                *[_text(_dict(span.get("error_type")) or span.get("error_type")) for span in spans]
            ))
        ),
        "error_message": _text(_first(trace.get("error"), exception_message)),
        "stacktrace": stacktrace,
        "generation_params": generation_params,
        "generation_params_available": bool(generation_params),
        "llm_count": len(llms),
    }


def build_context(traces: list[dict[str, Any]], spans_by_trace: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for trace in traces:
        trace_id = _text(trace.get("trace_id"))
        spans = spans_by_trace.get(trace_id or "", [])
        fields = extract_fields(trace, spans)
        context.append({"trace": trace, "spans": spans, "fields": fields})
    return context


def summary(context: list[dict[str, Any]]) -> dict[str, Any]:
    traces = [item["trace"] for item in context]
    all_spans = [span for item in context for span in item["spans"]]
    fields = [item["fields"] for item in context]

    # Latency stats skip traces without a usable duration rather than counting zero.
    durations = [f["duration_ms"] for f in fields if f["duration_ms"] is not None]
    # A trace counts as failed when status is error or an error field is set.
    failed = sum(
        1
        for trace in traces
        if str(trace.get("status") or "").lower() == "error" or trace.get("error") is not None
    )
    total_traces = len(traces)

    # Metric sums include only traces that actually reported the value.
    token_input = [f["tokens"]["input_tokens"] for f in fields if f["tokens"]["input_tokens_available"]]
    token_output = [f["tokens"]["output_tokens"] for f in fields if f["tokens"]["output_tokens_available"]]
    token_total = [f["tokens"]["total_tokens"] for f in fields if f["tokens"]["total_tokens_available"]]
    costs = [f["cost_usd"] for f in fields if f["cost_available"] and f["cost_usd"] is not None]

    # Availability flags track presence of data, not whether the summed value is nonzero.
    return {
        "total_traces": total_traces,
        "total_spans": len(all_spans),
        "total_llm_calls": sum(1 for span in all_spans if _is_llm_span(span)),
        "failed_traces": failed,
        "error_rate": (failed / total_traces) if total_traces else 0.0,
        "avg_latency_ms": (sum(durations) / len(durations)) if durations else None,
        "p50_latency_ms": _percentile(durations, 0.5),
        "p95_latency_ms": _percentile(durations, 0.95),
        "p99_latency_ms": _percentile(durations, 0.99),
        "input_tokens": sum(token_input) if token_input else None,
        "output_tokens": sum(token_output) if token_output else None,
        "total_tokens": sum(token_total) if token_total else None,
        "total_cost_usd": sum(costs) if costs else None,
        "tokens_available": bool(token_total or token_input or token_output),
        "cost_available": bool(costs),
        "traces_with_model": sum(1 for f in fields if f["model"] is not None),
        "traces_with_tokens": sum(1 for f in fields if f["tokens_available"]),
        "traces_with_cost": sum(1 for f in fields if f["cost_available"]),
        "traces_with_error_type": sum(1 for f in fields if f["error_type"] is not None),
    }


def timeseries(context: list[dict[str, Any]], bucket: str = "hour") -> list[dict[str, Any]]:
    # Traces are aligned to absolute UTC hour/day boundaries for aggregation.
    interval = timedelta(days=1) if bucket == "day" else timedelta(hours=1)
    buckets: dict[datetime, dict[str, Any]] = {}
    for item in context:
        started = _parse_time(item["trace"].get("start_time"))
        if started is None:
            continue
        key = started.replace(
            minute=0 if bucket == "hour" else started.minute,
            second=0,
            microsecond=0,
        ) if bucket == "hour" else started.replace(hour=0, minute=0, second=0, microsecond=0)
        entry = buckets.setdefault(
            key,
            {
                "timestamp": key.isoformat(),
                "traces": 0,
                "failed_traces": 0,
                "latencies": [],
                "tokens": [],
                "costs": [],
            },
        )
        entry["traces"] += 1
        fields = item["fields"]
        trace = item["trace"]
        if str(trace.get("status") or "").lower() == "error" or trace.get("error") is not None:
            entry["failed_traces"] += 1
        if fields["duration_ms"] is not None:
            entry["latencies"].append(fields["duration_ms"])
        if fields["tokens"]["total_tokens_available"] and fields["tokens"]["total_tokens"] is not None:
            entry["tokens"].append(fields["tokens"]["total_tokens"])
        if fields["cost_available"] and fields["cost_usd"] is not None:
            entry["costs"].append(fields["cost_usd"])

    if buckets:
        # Fill empty intervals so charts get a continuous axis, capped to bound response size.
        current = min(buckets)
        last = max(buckets)
        while current <= last and len(buckets) < 1000:
            buckets.setdefault(
                current,
                {
                    "timestamp": current.isoformat(),
                    "traces": 0,
                    "failed_traces": 0,
                    "latencies": [],
                    "tokens": [],
                    "costs": [],
                },
            )
            current += interval

    result: list[dict[str, Any]] = []
    for key in sorted(buckets):
        entry = buckets[key]
        result.append(
            {
                # Missing metrics stay null (not 0) so the UI can omit those series.
                "timestamp": entry["timestamp"],
                "traces": entry["traces"],
                "failed_traces": entry["failed_traces"],
                "error_rate": entry["failed_traces"] / entry["traces"] if entry["traces"] else 0.0,
                "avg_latency_ms": (
                    sum(entry["latencies"]) / len(entry["latencies"])
                    if entry["latencies"]
                    else None
                ),
                "tokens": sum(entry["tokens"]) if entry["tokens"] else None,
                "cost_usd": sum(entry["costs"]) if entry["costs"] else None,
            }
        )
    return result


def models(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Traces without a detected model form their own (None) bucket.
    grouped: dict[str | None, dict[str, Any]] = {}
    for item in context:
        model = item["fields"]["model"]
        entry = grouped.setdefault(
            model,
            {
                "model": model,
                "providers": set(),
                "traces": 0,
                "failed_traces": 0,
                "latencies": [],
                "tokens": [],
                "costs": [],
            },
        )
        entry["traces"] += 1
        if item["fields"]["provider"]:
            entry["providers"].add(item["fields"]["provider"])
        trace = item["trace"]
        if str(trace.get("status") or "").lower() == "error" or trace.get("error") is not None:
            entry["failed_traces"] += 1
        fields = item["fields"]
        if fields["duration_ms"] is not None:
            entry["latencies"].append(fields["duration_ms"])
        if fields["tokens"]["total_tokens_available"] and fields["tokens"]["total_tokens"] is not None:
            entry["tokens"].append(fields["tokens"]["total_tokens"])
        if fields["cost_available"] and fields["cost_usd"] is not None:
            entry["costs"].append(fields["cost_usd"])

    result: list[dict[str, Any]] = []
    for entry in grouped.values():
        result.append(
            {
                "model": entry["model"],
                "providers": sorted(entry["providers"]),
                "traces": entry["traces"],
                "failed_traces": entry["failed_traces"],
                "avg_latency_ms": (
                    sum(entry["latencies"]) / len(entry["latencies"])
                    if entry["latencies"]
                    else None
                ),
                "tokens": sum(entry["tokens"]) if entry["tokens"] else None,
                "cost_usd": sum(entry["costs"]) if entry["costs"] else None,
            }
        )
    # Unknown models sort last, then models are ranked by trace volume.
    result.sort(
        key=lambda item: (item["model"] is None, -item["traces"], item["model"] or "")
    )
    return result


def errors(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Group by type plus a truncated message to keep the number of groups bounded.
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in context:
        fields = item["fields"]
        trace = item["trace"]
        # A trace with only failed spans still contributes to the error group.
        if str(trace.get("status") or "").lower() != "error" and trace.get("error") is None:
            failed_spans = [
                span for span in item["spans"]
                if str(span.get("status") or "").lower() == "error" or span.get("error") is not None
            ]
            if not failed_spans:
                continue
        else:
            failed_spans = []
        error_type = fields["error_type"] or "Error"
        message = fields["error_message"] or ""
        key = (error_type, message[:200])
        entry = grouped.setdefault(
            key,
            {
                "error_type": error_type,
                "message": message,
                "count": 0,
                "trace_ids": set(),
                "span_ids": set(),
                "projects": set(),
                "environments": set(),
                "models": set(),
                "first_seen": None,
                "last_seen": None,
                "stacktrace": fields["stacktrace"],
            },
        )
        entry["count"] += 1
        trace_id = _text(trace.get("trace_id"))
        if trace_id:
            entry["trace_ids"].add(trace_id)
        for span in failed_spans:
            span_id = _text(span.get("span_id"))
            if span_id:
                entry["span_ids"].add(span_id)
        for target, value in (
            ("projects", trace.get("project_id")),
            ("environments", trace.get("environment")),
            ("models", fields["model"]),
        ):
            if value is not None:
                entry[target].add(str(value))
        started = _parse_time(trace.get("start_time"))
        if started is not None:
            # UTC-normalized ISO strings compare lexically, giving first/last seen.
            stamp = started.isoformat()
            if entry["first_seen"] is None or stamp < entry["first_seen"]:
                entry["first_seen"] = stamp
            if entry["last_seen"] is None or stamp > entry["last_seen"]:
                entry["last_seen"] = stamp
        if entry["stacktrace"] is None and fields["stacktrace"] is not None:
            entry["stacktrace"] = fields["stacktrace"]

    result: list[dict[str, Any]] = []
    for entry in grouped.values():
        result.append(
            {
                "error_type": entry["error_type"],
                "message": entry["message"],
                "count": entry["count"],
                "sample_trace_id": next(iter(entry["trace_ids"]), None),
                "trace_ids": sorted(entry["trace_ids"]),
                "span_ids": sorted(entry["span_ids"]),
                "projects": sorted(entry["projects"]),
                "environments": sorted(entry["environments"]),
                "models": sorted(entry["models"]),
                "first_seen": entry["first_seen"],
                "last_seen": entry["last_seen"],
                "stacktrace": entry["stacktrace"],
            }
        )
    result.sort(key=lambda item: (-item["count"], item["error_type"]))
    return result


# Traces and spans are ranked independently; limit applies to each list.
def slowest(
    context: list[dict[str, Any]], limit: int = 10
) -> dict[str, list[dict[str, Any]]]:
    trace_rows: list[dict[str, Any]] = []
    span_rows: list[dict[str, Any]] = []
    for item in context:
        trace = item["trace"]
        fields = item["fields"]
        if fields["duration_ms"] is not None:
            trace_rows.append(
                {
                    "trace_id": trace.get("trace_id"),
                    "name": trace.get("name"),
                    "duration_ms": fields["duration_ms"],
                    "status": trace.get("status"),
                    "project_id": trace.get("project_id"),
                    "environment": trace.get("environment"),
                    "model": fields["model"],
                }
            )
        for span in item["spans"]:
            duration = _number(span.get("duration_ms"))
            if duration is None:
                continue
            span_rows.append(
                {
                    "trace_id": span.get("trace_id"),
                    "span_id": span.get("span_id"),
                    "name": span.get("name"),
                    "span_type": _span_type(span),
                    "duration_ms": duration,
                    "status": span.get("status"),
                    "error": span.get("error"),
                }
            )
    trace_rows.sort(key=lambda row: row["duration_ms"], reverse=True)
    span_rows.sort(key=lambda row: row["duration_ms"], reverse=True)
    return {"traces": trace_rows[:limit], "spans": span_rows[:limit]}


# Only LLM spans with recorded input are eligible for replay.
def build_replay_spec(
    trace: dict[str, Any], spans: list[dict[str, Any]]
) -> dict[str, Any]:
    llms = _llm_spans(spans)
    entries: list[dict[str, Any]] = []
    for span in llms:
        if span.get("input") is None:
            continue
        attrs = _attributes(span)
        span_fields = extract_fields(trace, [span])
        entries.append(
            {
                "span_id": span.get("span_id"),
                "name": span.get("name"),
                "model": span_fields["model"] or attrs.get("model"),
                "provider": span_fields["provider"] or attrs.get("provider"),
                "input": span.get("input"),
                "output": span.get("output"),
                "parameters": span_fields["generation_params"],
                "parameters_available": span_fields["generation_params_available"],
                "token_usage": (
                    {
                        "input_tokens": span_fields["tokens"]["input_tokens"],
                        "output_tokens": span_fields["tokens"]["output_tokens"],
                        "total_tokens": span_fields["tokens"]["total_tokens"],
                    }
                    if span_fields["tokens_available"]
                    else None
                ),
                "token_usage_available": span_fields["tokens_available"],
                "latency_ms": _number(span.get("duration_ms")) or _number(attrs.get("latency_ms")),
                "status": span.get("status"),
                "error": span.get("error"),
            }
        )
    return {
        "trace_id": trace.get("trace_id"),
        "eligible": bool(entries),
        "reason": None if entries else "No LLM span with recorded input was found in this trace.",
        "llm_spans": entries,
        "selected": entries[0] if entries else None,
        "warning": (
            "Replay re-executes a recorded request with the supplied input and configuration. "
            "It does not deterministically reproduce the original execution context."
        ),
    }


# Flatten trace plus derived LLM fields into a single CSV/JSONL row.
def export_row(
    trace: dict[str, Any], spans: list[dict[str, Any]]
) -> dict[str, Any]:
    fields = extract_fields(trace, spans)
    return {
        "trace_id": trace.get("trace_id"),
        "name": trace.get("name"),
        "status": trace.get("status"),
        "project_id": trace.get("project_id"),
        "environment": trace.get("environment"),
        "start_time": trace.get("start_time"),
        "end_time": trace.get("end_time"),
        "duration_ms": fields["duration_ms"],
        "error_type": fields["error_type"],
        "error": fields["error_message"],
        "model": fields["model"],
        "provider": fields["provider"],
        "span_count": len(spans),
        "llm_calls": fields["llm_count"],
        "input_tokens": fields["tokens"]["input_tokens"],
        "output_tokens": fields["tokens"]["output_tokens"],
        "total_tokens": fields["tokens"]["total_tokens"],
        "cost_usd": fields["cost_usd"],
        "prompt": fields["input"],
        "response": fields["output"],
    }
