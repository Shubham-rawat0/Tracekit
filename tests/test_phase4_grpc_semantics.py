import sys
import types
from unittest.mock import patch

from tracekit.exporters.grpc import OTLPGrpcExporter


class FakeAnyValue:
    def __init__(self):
        self.bool_value = None
        self.int_value = None
        self.double_value = None
        self.string_value = None


class FakeKeyValue:
    def __init__(self, key="", value=None):
        self.key = key
        self.value = value or FakeAnyValue()

    def CopyFrom(self, other):
        self.key = other.key
        self.value = other.value


class FakeRepeated:
    def __init__(self, factory):
        self.factory = factory
        self.items = []

    def add(self, *args, **kwargs):
        item = self.factory(*args, **kwargs)
        self.items.append(item)
        return item


class FakeStatus:
    def __init__(self):
        self.code = 0
        self.message = ""


class FakeEvent:
    def __init__(self):
        self.name = ""
        self.time_unix_nano = 0
        self.attributes = FakeRepeated(FakeKeyValue)


class FakeSpan:
    def __init__(self):
        self.trace_id = b""
        self.span_id = b""
        self.parent_span_id = b""
        self.name = ""
        self.kind = 0
        self.start_time_unix_nano = 0
        self.end_time_unix_nano = 0
        self.attributes = FakeRepeated(FakeKeyValue)
        self.events = FakeRepeated(FakeEvent)
        self.status = FakeStatus()


class FakeScope:
    def __init__(self):
        self.name = ""
        self.version = ""


class FakeScopeSpan:
    def __init__(self):
        self.scope = FakeScope()
        self.spans = FakeRepeated(FakeSpan)


class FakeResource:
    def __init__(self):
        self.attributes = FakeRepeated(FakeKeyValue)


class FakeResourceSpan:
    def __init__(self):
        self.resource = FakeResource()
        self.scope_spans = FakeRepeated(FakeScopeSpan)


class FakeRequest:
    def __init__(self):
        self.resource_spans = FakeRepeated(FakeResourceSpan)


class FakeChannel:
    def __init__(self, target, secure=False):
        self.target = target
        self.secure = secure
        self.closed = False

    def close(self):
        self.closed = True


class FakeStub:
    calls = []

    def __init__(self, channel):
        self.channel = channel

    def Export(self, request, metadata=None, timeout=None):
        self.calls.append((request, metadata, timeout))
        return object()


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _fake_dependencies():
    service_pb2 = _module(
        "trace_service_pb2",
        ExportTraceServiceRequest=FakeRequest,
    )
    service_pb2_grpc = _module("trace_service_pb2_grpc", TraceServiceStub=FakeStub)
    common_pb2 = _module(
        "common_pb2",
        AnyValue=FakeAnyValue,
        KeyValue=FakeKeyValue,
    )
    status = _module("status", STATUS_CODE_OK=1, STATUS_CODE_ERROR=2)
    trace_pb2 = _module(
        "trace_pb2",
        SPAN_KIND_INTERNAL=0,
        SPAN_KIND_CLIENT=3,
        Status=status,
    )
    channels = []
    grpc = _module("grpc")

    def insecure_channel(target):
        channel = FakeChannel(target)
        channels.append(channel)
        return channel

    def secure_channel(target, credentials):
        channel = FakeChannel(target, secure=True)
        channels.append(channel)
        return channel

    grpc.insecure_channel = insecure_channel
    grpc.secure_channel = secure_channel
    grpc.ssl_channel_credentials = lambda: ("ssl",)
    service_module = _module(
        "opentelemetry.proto.collector.trace.v1",
        trace_service_pb2=service_pb2,
        trace_service_pb2_grpc=service_pb2_grpc,
    )
    common_module = _module("opentelemetry.proto.common.v1", common_pb2=common_pb2)
    trace_module = _module("opentelemetry.proto.trace.v1", trace_pb2=trace_pb2)
    modules = {
        "grpc": grpc,
        "opentelemetry": _module("opentelemetry"),
        "opentelemetry.proto": _module("opentelemetry.proto"),
        "opentelemetry.proto.collector": _module("opentelemetry.proto.collector"),
        "opentelemetry.proto.collector.trace": _module("opentelemetry.proto.collector.trace"),
        "opentelemetry.proto.collector.trace.v1": service_module,
        "opentelemetry.proto.common": _module("opentelemetry.proto.common"),
        "opentelemetry.proto.common.v1": common_module,
        "opentelemetry.proto.trace": _module("opentelemetry.proto.trace"),
        "opentelemetry.proto.trace.v1": trace_module,
    }
    parent = modules["opentelemetry"]
    parent.proto = modules["opentelemetry.proto"]
    parent.proto.collector = modules["opentelemetry.proto.collector"]
    parent.proto.collector.trace = modules["opentelemetry.proto.collector.trace"]
    parent.proto.collector.trace.v1 = service_module
    parent.proto.common = modules["opentelemetry.proto.common"]
    parent.proto.common.v1 = common_module
    parent.proto.trace = modules["opentelemetry.proto.trace"]
    parent.proto.trace.v1 = trace_module
    FakeStub.calls = []
    return modules, channels


def _trace():
    return {
        "trace_id": "a" * 32,
        "status": "ok",
        "start_time": "2026-01-01T00:00:00Z",
        "metadata": {"sampled": True},
        "attributes": {"deployment.environment": "test"},
    }


def _spans():
    return [
        {
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "parent_span_id": "c" * 16,
            "name": "llm",
            "span_type": "llm",
            "kind": "SPAN_KIND_CLIENT",
            "status": {"code": "STATUS_CODE_ERROR", "message": "failed"},
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:00:01Z",
            "attributes": {"model": 7, "score": 0.5, "cached": True},
            "events": [
                {
                    "name": "exception",
                    "timestamp": "2026-01-01T00:00:00.500000Z",
                    "attributes": {"exception.type": "RuntimeError"},
                },
                "ignored",
            ],
        }
    ]


def test_grpc_export_serializes_protobuf_request():
    modules, channels = _fake_dependencies()
    with patch.dict(sys.modules, modules):
        exporter = OTLPGrpcExporter(
            endpoint="collector:4317",
            headers={"Authorization": "Bearer token"},
            service_name="demo",
            resource_attributes={"service.version": "1.0"},
        )
        assert exporter.export_trace(_trace(), _spans()) is True
        request, metadata, timeout = FakeStub.calls[0]

    resource_attrs = {
        attribute.key: attribute.value for attribute in request.resource_spans.items[0].resource.attributes.items
    }
    assert resource_attrs["service.name"].string_value == "demo"
    assert resource_attrs["service.version"].string_value == "1.0"
    assert resource_attrs["tracekit.trace.sampled"].bool_value is True
    assert resource_attrs["deployment.environment"].string_value == "test"

    proto = request.resource_spans.items[0].scope_spans.items[0].spans.items[0]
    assert proto.trace_id == bytes.fromhex("a" * 32)
    assert proto.span_id == bytes.fromhex("b" * 16)
    assert proto.parent_span_id == bytes.fromhex("c" * 16)
    assert proto.kind == 3
    assert proto.start_time_unix_nano > 0
    assert proto.end_time_unix_nano > proto.start_time_unix_nano
    assert proto.status.code == 2
    span_attrs = {attribute.key: attribute.value for attribute in proto.attributes.items}
    assert span_attrs["model"].int_value == 7
    assert span_attrs["score"].double_value == 0.5
    assert span_attrs["cached"].bool_value is True
    assert span_attrs["tracekit.span.type"].string_value == "llm"
    assert proto.events.items[0].name == "exception"
    assert proto.events.items[0].time_unix_nano > 0
    assert metadata == (("authorization", "Bearer token"),)
    assert timeout == 10.0
    assert channels[0].target == "collector:4317"
    assert channels[0].secure is False

    exporter.close()
    assert channels[0].closed is True


def test_grpc_export_supports_secure_channel_and_short_ids():
    modules, channels = _fake_dependencies()
    with patch.dict(sys.modules, modules):
        exporter = OTLPGrpcExporter(endpoint="https://collector:4317", insecure=False)
        assert exporter.export_trace(
            {"trace_id": "short", "attributes": {}},
            [
                {
                    "trace_id": "short",
                    "span_id": "tiny",
                    "name": "work",
                    "start_time": "not-a-time",
                    "end_time": None,
                }
            ],
        )

    assert channels[0].secure is True
    exporter.close()
