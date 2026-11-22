import unittest
from unittest.mock import patch

from tracekit.core.reliability import BoundedBatchQueue, ExportMetrics, bounded_backoff
from tracekit.exporters.grpc import OTLPGrpcExporter
from tracekit.exporters.otlp import OTLPExporter


def sample_trace() -> dict:
    return {
        "trace_id": "a" * 32,
        "name": "checkout",
        "status": "ok",
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:02+00:00",
        "duration_ms": 2000.0,
        "attributes": {"deployment.environment": "test"},
        "metadata": {"sampled": True},
    }


def sample_spans() -> list[dict]:
    return [
        {
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "parent_span_id": None,
            "name": "llm-call",
            "span_type": "llm",
            "status": "error",
            "error": "model unavailable",
            "start_time": "2026-01-01T00:00:00+00:00",
            "end_time": "2026-01-01T00:00:01+00:00",
            "duration_ms": 1000.0,
            "attributes": {"model": "test-model", "sampled": True},
            "events": [
                {
                    "name": "exception",
                    "timestamp": "2026-01-01T00:00:00.500000+00:00",
                    "attributes": {
                        "exception.type": "RuntimeError",
                        "exception.message": "model unavailable",
                        "exception.stacktrace": "RuntimeError: model unavailable",
                    },
                }
            ],
        }
    ]


class TestOTLPSemanticPayload(unittest.TestCase):
    def test_payload_includes_semantic_attributes_status_events_and_resource(self):
        exporter = OTLPExporter(
            "http://collector",
            service_name="checkout-service",
            resource_attributes={"service.version": "1.2.3"},
        )
        payload = exporter.build_payload(sample_trace(), sample_spans())
        resource = payload["resourceSpans"][0]["resource"]
        resource_attrs = {
            item["key"]: item["value"] for item in resource["attributes"]
        }
        self.assertEqual(
            resource_attrs["service.name"],
            {"stringValue": "checkout-service"},
        )
        self.assertEqual(
            resource_attrs["service.version"],
            {"stringValue": "1.2.3"},
        )
        self.assertEqual(
            resource_attrs["deployment.environment"],
            {"stringValue": "test"},
        )
        self.assertIn("tracekit.trace.sampled", resource_attrs)

        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(span["traceId"], "a" * 32)
        self.assertEqual(span["spanId"], "b" * 16)
        self.assertEqual(span["kind"], "SPAN_KIND_CLIENT")
        self.assertEqual(span["status"]["code"], "STATUS_CODE_ERROR")
        attrs = {item["key"]: item["value"] for item in span["attributes"]}
        self.assertEqual(attrs["tracekit.span.type"], {"stringValue": "llm"})
        self.assertEqual(attrs["tracekit.sampled"], {"boolValue": True})
        self.assertEqual(attrs["tracekit.duration_ms"], {"doubleValue": 1000.0})
        self.assertEqual(attrs["exception.escaped"], {"boolValue": False})
        self.assertEqual(span["events"][0]["name"], "exception")

    def test_http_success_non2xx_and_exception_metrics(self):
        ok_calls = []

        def ok(endpoint, headers, payload):
            ok_calls.append((endpoint, headers, payload))
            return 202

        exporter = OTLPExporter(
            "https://collector/v1/traces",
            headers={"Authorization": "Bearer secret"},
            transport=ok,
            max_attempts=1,
        )
        self.assertTrue(exporter.export_trace(sample_trace(), sample_spans()))
        self.assertEqual(ok_calls[0][1]["Authorization"], "Bearer secret")
        self.assertEqual(exporter.metrics.success, 1)

        for transport in (lambda *args: 500, lambda *args: (_ for _ in ()).throw(OSError("down"))):
            failed = OTLPExporter(
                "http://collector",
                transport=transport,
                max_attempts=2,
                backoff_base=0,
            )
            self.assertFalse(failed.export_trace(sample_trace(), sample_spans()))
            self.assertGreaterEqual(failed.metrics.failure, 1)
            self.assertEqual(failed.metrics.retry, 1)


class TestReliability(unittest.TestCase):
    def test_bounded_backoff_and_metrics(self):
        self.assertEqual(bounded_backoff(-1), 0.0)
        self.assertEqual(bounded_backoff(0, base=0.1, maximum=5), 0.1)
        self.assertEqual(bounded_backoff(2, base=0.1, maximum=5), 0.4)
        self.assertEqual(bounded_backoff(100, base=0.1, maximum=5), 5.0)

        metrics = ExportMetrics()
        metrics.record_success(12.5)
        metrics.record_failure(50)
        metrics.record_retry()
        metrics.record_dropped(2)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["failure"], 1)
        self.assertEqual(snapshot["retry"], 1)
        self.assertEqual(snapshot["dropped"], 2)
        self.assertEqual(snapshot["latency_ms_average"], 31.25)
        self.assertEqual(snapshot["latency_ms_max"], 50.0)

    def test_queue_batch_drop_health_and_close(self):
        queue = BoundedBatchQueue(max_items=2)
        self.assertTrue(queue.put("first"))
        self.assertTrue(queue.put("second"))
        self.assertFalse(queue.put("third"))
        self.assertEqual(queue.drain(1), ["first"])
        self.assertEqual(queue.drain(), ["second"])

        exporter = OTLPExporter(
            "http://collector",
            transport=lambda *args: 200,
            batch_size=2,
            max_queue_size=3,
            max_attempts=1,
        )
        self.assertTrue(exporter.enqueue(sample_trace(), sample_spans()))
        self.assertEqual(exporter.flush(), 1)
        self.assertTrue(exporter.health_check()["healthy"])
        self.assertEqual(exporter.health_check()["queued"], 0)
        exporter.close()
        self.assertFalse(exporter.health_check()["healthy"])
        self.assertFalse(exporter.enqueue(sample_trace(), sample_spans()))

    def test_queue_drops_when_full(self):
        exporter = OTLPExporter(
            "http://collector",
            transport=lambda *args: 200,
            max_queue_size=1,
            max_attempts=1,
        )
        self.assertTrue(exporter.enqueue(sample_trace(), sample_spans()))
        self.assertFalse(exporter.enqueue(sample_trace(), sample_spans()))
        self.assertEqual(exporter.metrics.dropped, 1)
        exporter.close()


class TestOTLPGrpc(unittest.TestCase):
    def test_missing_dependencies_raise_clear_runtime_error(self):
        exporter = OTLPGrpcExporter(
            endpoint="localhost:4317",
            transport=None,
        )
        real_import = __import__

        def import_blocker(name, *args, **kwargs):
            if name == "grpc" or name.startswith("opentelemetry.proto"):
                raise ImportError("missing optional dependency")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_blocker):
            with self.assertRaisesRegex(RuntimeError, "OTLP/gRPC dependencies are missing"):
                exporter.export_trace(sample_trace(), sample_spans())

    def test_injected_transport_payload_health_batch_and_close(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return True

        exporter = OTLPGrpcExporter(
            transport=transport,
            max_attempts=1,
            batch_size=2,
        )
        self.assertTrue(exporter.enqueue(sample_trace(), sample_spans()))
        self.assertEqual(exporter.flush(), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["resource"]["service.name"], "tracekit")
        self.assertTrue(exporter.health_check()["healthy"])
        exporter.close()
        self.assertFalse(exporter.health_check()["healthy"])


class TestOTLPAsync(unittest.IsolatedAsyncioTestCase):
    async def test_async_http_export_returns_success(self):
        exporter = OTLPExporter(
            "http://collector",
            transport=lambda *args: 200,
            max_attempts=1,
        )
        self.assertTrue(
            await exporter.export_trace_async(sample_trace(), sample_spans())
        )

    async def test_async_grpc_export_with_fake_transport(self):
        exporter = OTLPGrpcExporter(
            transport=lambda payload: True,
            max_attempts=1,
        )
        self.assertTrue(
            await exporter.export_trace_async(sample_trace(), sample_spans())
        )


if __name__ == "__main__":
    unittest.main()
