"""Optional OTLP/gRPC exporter."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tracekit.core.reliability import BoundedBatchQueue, ExportMetrics, bounded_backoff

GrpcTransport = Callable[[dict[str, Any]], Any]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _to_nanos(value: Any) -> int:
    parsed = _parse_time(value)
    if parsed is None:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _bytes_id(value: Any, size: int) -> bytes:
    # OTLP proto IDs are raw bytes (16 for trace, 8 for span), and UUIDs arrive hyphenated.
    text = str(value or "").replace("-", "")
    if len(text) >= size * 2:
        try:
            return bytes.fromhex(text[: size * 2])
        except ValueError:
            pass
    # Non-hex ids are hashed to a fixed width so malformed input cannot change proto field sizes.
    return hashlib.sha256(text.encode("utf-8")).digest()[:size]


def _enum(container: Any, name: Any, default_name: str) -> Any:
    candidate = getattr(container, str(name), None)
    if isinstance(candidate, int):
        return candidate
    # Resolve enum values by generated proto name; unknown names fall back to 0 across proto versions.
    fallback = getattr(container, default_name, 0)
    return fallback if isinstance(fallback, int) else 0


class OTLPGrpcExporter:
    def __init__(
        self,
        endpoint: str = "localhost:4317",
        headers: dict[str, str] | None = None,
        transport: GrpcTransport | None = None,
        timeout: float = 10.0,
        service_name: str = "tracekit",
        resource_attributes: dict[str, Any] | None = None,
        batch_size: int = 100,
        max_queue_size: int = 1000,
        max_attempts: int = 3,
        backoff_base: float = 0.1,
        backoff_max: float = 5.0,
        insecure: bool = True,
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
        self.insecure = insecure
        self._transport = transport
        self._queue = BoundedBatchQueue(max_queue_size)
        self.metrics = ExportMetrics()
        self._closed = False
        self._grpc: Any = None
        self._protobuf: tuple[Any, Any, Any, Any] | None = None
        self._channel: Any = None

    def _load_optional_dependencies(self) -> None:
        # gRPC and protobuf stay optional; an injected transport bypasses these imports entirely.
        if self._transport is not None:
            return
        try:
            import grpc
            from opentelemetry.proto.collector.trace.v1 import (
                trace_service_pb2,
                trace_service_pb2_grpc,
            )
            from opentelemetry.proto.common.v1 import common_pb2
            from opentelemetry.proto.trace.v1 import trace_pb2
        except ImportError as exc:
            raise RuntimeError(
                "OTLP/gRPC dependencies are missing; install grpcio "
                "and the OpenTelemetry protobuf packages"
            ) from exc
        self._grpc = grpc
        self._protobuf = (trace_service_pb2, trace_service_pb2_grpc, trace_pb2, common_pb2)

    def build_payload(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        resource_attrs: dict[str, Any] = {
            "service.name": self.service_name,
            **self.resource_attributes,
        }
        metadata = trace.get("metadata")
        if isinstance(metadata, dict) and "sampled" in metadata:
            resource_attrs["tracekit.trace.sampled"] = bool(metadata["sampled"])
        if isinstance(trace.get("attributes"), dict):
            resource_attrs.update(trace["attributes"])
        # Keep raw spans here; conversion to protobuf happens at send time, unlike the JSON exporter.
        return {
            "resource": resource_attrs,
            "trace": trace,
            "spans": spans,
        }

    def _proto_value(self, common_pb2: Any, value: Any) -> Any:
        attribute = common_pb2.AnyValue()
        # bool is an int subclass, so it must be matched before int_value.
        if isinstance(value, bool):
            attribute.bool_value = value
        elif isinstance(value, int):
            attribute.int_value = int(value)
        elif isinstance(value, float):
            attribute.double_value = float(value)
        else:
            attribute.string_value = str(value)
        return attribute

    def _proto_attributes(self, common_pb2: Any, attributes: Any) -> list[Any]:
        if not isinstance(attributes, dict):
            return []
        return [
            common_pb2.KeyValue(key=str(key), value=self._proto_value(common_pb2, value))
            for key, value in attributes.items()
        ]

    def _grpc_export(self, payload: dict[str, Any]) -> bool:
        if self._protobuf is None or self._grpc is None:
            raise RuntimeError("OTLP/gRPC exporter is not initialized")
        service_pb2, service_pb2_grpc, trace_pb2, common_pb2 = self._protobuf
        request = service_pb2.ExportTraceServiceRequest()
        resource_spans = request.resource_spans.add()
        # Resource attributes become KeyValue protos; service.name is already merged in build_payload.
        for key, value in payload.get("resource", {}).items():
            resource_spans.resource.attributes.add(
                key=str(key), value=self._proto_value(common_pb2, value)
            )
        scope_spans = resource_spans.scope_spans.add()
        scope_spans.scope.name = "tracekit"
        scope_spans.scope.version = "0.2.0"
        trace_data = payload.get("trace") or {}

        for span in payload.get("spans") or []:
            proto = scope_spans.spans.add()
            # Protobuf fields need raw bytes, while internal spans carry hex string ids.
            trace_id = span.get("trace_id") or trace_data.get("trace_id")
            proto.trace_id = _bytes_id(trace_id, 16)
            proto.span_id = _bytes_id(span.get("span_id"), 8)
            if span.get("parent_span_id"):
                proto.parent_span_id = _bytes_id(span.get("parent_span_id"), 8)
            proto.name = str(span.get("name") or "span")
            proto.kind = _enum(
                trace_pb2,
                span.get("kind") or span.get("span_type") or span.get("type"),
                "SPAN_KIND_INTERNAL",
            )
            proto.start_time_unix_nano = int(
                span.get("startTimeUnixNano") or _to_nanos(span.get("start_time"))
            )
            proto.end_time_unix_nano = int(
                span.get("endTimeUnixNano") or _to_nanos(span.get("end_time"))
            )
            attributes: dict[str, Any] = dict(span.get("attributes") or {})
            attributes["tracekit.span.type"] = str(
                span.get("span_type") or span.get("type") or "internal"
            )
            # CopyFrom avoids aliasing the temporary AnyValue instance across attributes.
            for attribute in self._proto_attributes(common_pb2, attributes):
                proto.attributes.add().CopyFrom(attribute)
            for event in span.get("events") or []:
                if not isinstance(event, dict):
                    continue
                proto_event = proto.events.add()
                proto_event.name = str(event.get("name") or "event")
                proto_event.time_unix_nano = _to_nanos(event.get("timestamp"))
                for attribute in self._proto_attributes(
                    common_pb2, event.get("attributes") or {}
                ):
                    proto_event.attributes.add().CopyFrom(attribute)
            status = span.get("status") or {}
            status_code = status.get("code") if isinstance(status, dict) else "STATUS_CODE_OK"
            proto.status.code = _enum(trace_pb2.Status, status_code, "STATUS_CODE_OK")

        # gRPC takes a host:port target, not a URL with a scheme.
        target = self.endpoint.replace("https://", "").replace("http://", "")
        # Reuse one channel across exports; creating one per request leaks connections.
        if self._channel is None:
            if self.insecure:
                self._channel = self._grpc.insecure_channel(target)
            else:
                self._channel = self._grpc.secure_channel(
                    target, self._grpc.ssl_channel_credentials()
                )
        stub = service_pb2_grpc.TraceServiceStub(self._channel)
        # gRPC metadata keys must be lowercase strings, hence the normalization of caller headers.
        metadata = tuple((str(key).lower(), str(value)) for key, value in self.headers.items())
        stub.Export(request, metadata=metadata, timeout=self.timeout)
        return True

    def _send_once(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> bool:
        payload = self.build_payload(trace, spans)
        started = time.perf_counter()
        try:
            if self._transport is not None:
                result = self._transport(payload)
            else:
                self._load_optional_dependencies()
                result = self._grpc_export(payload)
        # RuntimeError means missing deps or an uninitialized exporter, not a retryable send failure.
        except RuntimeError:
            raise
        except Exception:
            self.metrics.record_failure((time.perf_counter() - started) * 1000.0)
            return False
        success = result is True or (isinstance(result, int) and 200 <= result < 300)
        if success:
            self.metrics.record_success((time.perf_counter() - started) * 1000.0)
        else:
            self.metrics.record_failure((time.perf_counter() - started) * 1000.0)
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
        # Bounded queue rejects overflow rather than blocking the producer.
        self.metrics.record_dropped(1)
        return False

    def flush(self) -> int:
        exported = 0
        while True:
            batch = self._queue.drain(self.batch_size)
            if not batch:
                break
            for trace, spans in batch:
                if self.export_trace(trace, spans):
                    exported += 1
        return exported

    async def export_trace_async(
        self,
        trace: dict[str, Any],
        spans: list[dict[str, Any]],
    ) -> bool:
        return await asyncio.to_thread(self.export_trace, trace, spans)

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
            # Flush is best-effort; remaining items are dropped before releasing the channel.
            queued = self._queue.size
            if queued:
                self.metrics.record_dropped(queued)
            self._queue.drain()
            if self._channel is not None:
                close = getattr(self._channel, "close", None)
                if close is not None:
                    close()
                self._channel = None
            self._closed = True


OTLPGRPCExporter = OTLPGrpcExporter
