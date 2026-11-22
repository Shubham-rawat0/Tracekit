"""OTLP/HTTP JSON exporter with batching and bounded retries."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tracekit.core.reliability import BoundedBatchQueue, ExportMetrics, bounded_backoff

Transport = Callable[[str, dict[str, str], dict[str, Any]], Any]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        # fromisoformat does not accept the ISO "Z" suffix, so normalize it to an offset.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _to_nanos(value: Any) -> int | None:
    dt = _parse_time(value)
    if dt is None:
        return None
    # Naive timestamps are assumed UTC, otherwise the same value shifts with host timezone.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _otlp_value(value: Any) -> dict[str, Any]:
    # bool must be checked before int because bool is an int subclass.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # OTLP JSON encodes 64-bit intValue as a decimal string to avoid precision loss.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    # AnyValue has no structured type for arbitrary containers, so flatten them to JSON text.
    if isinstance(value, (dict, list, tuple)):
        return {"stringValue": json.dumps(value, default=str, sort_keys=True)}
    return {"stringValue": str(value)}


def _otlp_attributes(attributes: Any) -> list[dict[str, Any]]:
    if not isinstance(attributes, dict):
        return []
    return [
        {"key": str(key), "value": _otlp_value(value)}
        for key, value in attributes.items()
    ]


def _span_kind(span: dict[str, Any]) -> str:
    # Tracekit-specific types collapse to OTLP client/internal kinds; unknown types default internal.
    span_type = str(span.get("span_type") or span.get("type") or "internal").lower()
    kinds = {
        "llm": "SPAN_KIND_CLIENT",
        "tool": "SPAN_KIND_CLIENT",
        "http": "SPAN_KIND_CLIENT",
        "database": "SPAN_KIND_CLIENT",
        "retriever": "SPAN_KIND_CLIENT",
        "embedding": "SPAN_KIND_CLIENT",
        "agent": "SPAN_KIND_INTERNAL",
        "chain": "SPAN_KIND_INTERNAL",
    }
    return kinds.get(span_type, "SPAN_KIND_INTERNAL")


def _status(span: dict[str, Any]) -> dict[str, Any]:
    status = str(span.get("status") or "").lower()
    # An error flag and a textual error status both map to the single OTLP error code.
    is_error = bool(span.get("error")) or status in {"error", "failed", "failure"}
    return {
        "code": "STATUS_CODE_ERROR" if is_error else "STATUS_CODE_OK"
    }


class OTLPExporter:
    def __init__(
        self,
        endpoint: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        service_name: str = "tracekit",
        transport: Transport | None = None,
        resource_attributes: dict[str, Any] | None = None,
        batch_size: int = 100,
        max_queue_size: int = 1000,
        max_attempts: int = 3,
        backoff_base: float = 0.1,
        backoff_max: float = 5.0,
    ) -> None:
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self.service_name = service_name
        self.resource_attributes = dict(resource_attributes or {})
        self.batch_size = max(1, int(batch_size))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(0.0, float(backoff_max))
        self._transport = transport or self._urllib_transport
        self._queue = BoundedBatchQueue(max_queue_size)
        self.metrics = ExportMetrics()
        self._closed = False

    def _urllib_transport(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> int:
        data = json.dumps(payload, default=str).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return int(getattr(response, "status", response.getcode()))

    def _span_payload(
        self,
        span: dict[str, Any],
        fallback_start: Any = None,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Trace-level start/ids are fallbacks for spans missing their own values.
        start = _to_nanos(span.get("start_time") or fallback_start)
        end = _to_nanos(span.get("end_time"))
        attributes = dict(span.get("attributes") or {})
        # Tracekit metadata is copied into attributes so it survives export to an OTLP backend.
        attributes.update(
            {
                "tracekit.span.type": str(
                    span.get("span_type") or span.get("type") or "internal"
                ),
                "tracekit.sampled": bool(
                    (span.get("attributes") or {}).get(
                        "sampled",
                        (trace or {}).get("metadata", {}).get("sampled", True),
                    )
                ),
            }
        )
        # OTLP JSON uses camelCase fields; internal snake_case spans must be converted first.
        payload: dict[str, Any] = {
            "traceId": str(span.get("trace_id") or (trace or {}).get("trace_id") or ""),
            "spanId": str(span.get("span_id") or ""),
            "name": str(span.get("name") or "span"),
            "kind": _span_kind(span),
            "attributes": _otlp_attributes(attributes),
            "status": _status(span),
        }
        duration = span.get("duration_ms")
        if duration is not None:
            attributes["tracekit.duration_ms"] = duration
            # Rebuild attributes: the earlier conversion happened before duration_ms was added.
            payload["attributes"] = _otlp_attributes(attributes)
        parent_id = span.get("parent_span_id")
        if parent_id:
            payload["parentSpanId"] = str(parent_id)
        if start is not None:
            payload["startTimeUnixNano"] = start
        if end is not None:
            payload["endTimeUnixNano"] = end
        events = []
        exception_flag = False
        for event in span.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_payload: dict[str, Any] = {
                "name": str(event.get("name") or "event"),
                "attributes": _otlp_attributes(event.get("attributes")),
            }
            time_nanos = _to_nanos(event.get("timestamp"))
            if time_nanos is not None:
                event_payload["timeUnixNano"] = time_nanos
            event_name = str(event.get("name") or "")
            event_attrs = event.get("attributes") or {}
            if event_name == "exception" or "exception.type" in event_attrs:
                exception_flag = True
            events.append(event_payload)
        # OTLP events have no "escaped" field, so flag it on the span when exceptions occurred.
        if exception_flag:
            payload["attributes"].append(
                {
                    "key": "exception.escaped",
                    "value": _otlp_value(False),
                }
            )
        if events:
            payload["events"] = events
        return payload

    def build_payload(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        trace_start = trace.get("start_time")
        # service.name is an OTLP resource requirement; explicit resource_attributes can override it.
        resource_attrs: dict[str, Any] = {
            "service.name": self.service_name,
            **self.resource_attributes,
        }
        metadata = trace.get("metadata")
        if isinstance(metadata, dict):
            sampled = metadata.get("sampled")
            if sampled is not None:
                resource_attrs["tracekit.trace.sampled"] = bool(sampled)
        trace_attributes = trace.get("attributes")
        if isinstance(trace_attributes, dict):
            # Copy trace attributes onto the resource so backend filters see them on every span.
            resource_attrs.update(trace_attributes)
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": _otlp_attributes(resource_attrs)
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "tracekit"},
                            "spans": [
                                self._span_payload(
                                    span,
                                    fallback_start=trace_start,
                                    trace=trace,
                                )
                                for span in spans
                            ],
                        }
                    ],
                }
            ]
        }

    def _send_once(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> bool:
        payload = self.build_payload(trace, spans)
        # Caller-supplied headers (auth, proxies) win over the default JSON content type.
        headers = {"Content-Type": "application/json", **self.headers}
        started = time.perf_counter()
        try:
            result = self._transport(self.endpoint, headers, payload)
        except Exception:
            self.metrics.record_failure(
                (time.perf_counter() - started) * 1000.0
            )
            return False
        # Injected transports may report success as bool or as an HTTP status code.
        success = result is True or (
            isinstance(result, int) and 200 <= result < 300
        )
        if success:
            self.metrics.record_success(
                (time.perf_counter() - started) * 1000.0
            )
        else:
            self.metrics.record_failure(
                (time.perf_counter() - started) * 1000.0
            )
        return success

    def export_trace(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> bool:
        if self._closed:
            return False
        for attempt in range(self.max_attempts):
            if self._send_once(trace, spans):
                return True
            if attempt + 1 < self.max_attempts:
                self.metrics.record_retry()
                time.sleep(
                    bounded_backoff(
                        attempt,
                        base=self.backoff_base,
                        maximum=self.backoff_max,
                    )
                )
        return False

    def export_traces(
        self,
        items: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]],
    ) -> int:
        if self._closed:
            return 0
        exported = 0
        for trace, spans in items:
            if self.export_trace(trace, spans):
                exported += 1
        return exported

    def enqueue(self, trace: dict[str, Any], spans: list[dict[str, Any]]) -> bool:
        if self._closed:
            return False
        if self._queue.put((trace, spans)):
            return True
        # The bounded queue rejects new work when full, so overflow is counted as dropped.
        self.metrics.record_dropped(1)
        return False

    def flush(self) -> int:
        exported = 0
        while True:
            batches = self._queue.drain(self.batch_size)
            if not batches:
                break
            for trace, spans in batches:
                if self.export_trace(trace, spans):
                    exported += 1
        return exported

    async def export_trace_async(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> bool:
        # Blocking socket I/O runs in a worker thread to avoid stalling the event loop.
        return await asyncio.to_thread(self.export_trace, trace, spans)

    async def export_traces_async(
        self,
        items: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]],
    ) -> int:
        return await asyncio.to_thread(self.export_traces, items)

    def health_check(self) -> dict[str, Any]:
        return {
            "healthy": not self._closed,
            "closed": self._closed,
            "queued": self._queue.size,
            "metrics": self.metrics.snapshot(),
        }

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            # Flush is best-effort: anything still queued is dropped on close.
            queued = self._queue.size
            if queued:
                self.metrics.record_dropped(queued)
            self._queue.drain()
            self._closed = True
