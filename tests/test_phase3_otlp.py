"""Tests for the OTLP exporter skeleton."""

from __future__ import annotations

import unittest

from tracekit.exporters.otlp import OTLPExporter


def sample_trace() -> dict:
    return {
        "trace_id": "a" * 32,
        "name": "root",
        "start_time": "2026-01-01T00:00:00+00:00",
    }


def sample_spans() -> list[dict]:
    return [
        {
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "parent_span_id": None,
            "name": "llm-call",
            "span_type": "llm",
            "status": "ok",
            "start_time": "2026-01-01T00:00:00+00:00",
            "end_time": "2026-01-01T00:00:01+00:00",
            "attributes": {"model": "m", "tokens": 7, "ok": True, "score": 0.5},
            "events": [
                {
                    "name": "llm.result",
                    "timestamp": "2026-01-01T00:00:00.500000+00:00",
                    "attributes": {"total_tokens": 7},
                }
            ],
        },
        {
            "trace_id": "a" * 32,
            "span_id": "c" * 16,
            "parent_span_id": "b" * 16,
            "name": "tool",
            "span_type": "tool",
            "status": "error",
            "error": "boom",
            "start_time": "2026-01-01T00:00:00+00:00",
            "attributes": {},
        },
    ]


class TestOTLPPayload(unittest.TestCase):
    def test_resource_scope_and_basic_span_mapping(self) -> None:
        exporter = OTLPExporter("http://collector", service_name="svc")
        payload = exporter.build_payload(sample_trace(), sample_spans())
        resource_span = payload["resourceSpans"][0]
        attrs = resource_span["resource"]["attributes"]
        self.assertEqual(attrs, [{"key": "service.name", "value": {"stringValue": "svc"}}])
        scope_span = resource_span["scopeSpans"][0]
        self.assertEqual(scope_span["scope"], {"name": "tracekit"})
        spans = scope_span["spans"]
        self.assertEqual(len(spans), 2)
        first = spans[0]
        self.assertEqual(first["traceId"], "a" * 32)
        self.assertEqual(first["spanId"], "b" * 16)
        self.assertEqual(first["kind"], "SPAN_KIND_CLIENT")
        self.assertEqual(first["status"], {"code": "STATUS_CODE_OK"})
        self.assertEqual(first["startTimeUnixNano"], 1767225600000000000)
        self.assertEqual(first["endTimeUnixNano"], 1767225601000000000)
        self.assertNotIn("parentSpanId", first)
        attrs = {item["key"]: item["value"] for item in first["attributes"]}
        self.assertEqual(attrs["model"], {"stringValue": "m"})
        self.assertEqual(attrs["tokens"], {"intValue": "7"})
        self.assertEqual(attrs["ok"], {"boolValue": True})
        self.assertEqual(attrs["score"], {"doubleValue": 0.5})

    def test_parent_status_and_events(self) -> None:
        exporter = OTLPExporter("http://collector")
        spans = exporter.build_payload(sample_trace(), sample_spans())[
            "resourceSpans"
        ][0]["scopeSpans"][0]["spans"]
        child = spans[1]
        self.assertEqual(child["parentSpanId"], "b" * 16)
        self.assertEqual(child["status"], {"code": "STATUS_CODE_ERROR"})
        first_event = spans[0]["events"][0]
        self.assertEqual(first_event["name"], "llm.result")
        self.assertEqual(first_event["timeUnixNano"], 1767225600500000000)
        self.assertEqual(
            first_event["attributes"],
            [{"key": "total_tokens", "value": {"intValue": "7"}}],
        )

    def test_complex_attributes_are_stringified(self) -> None:
        exporter = OTLPExporter("http://collector")
        trace = sample_trace()
        span = {
            "trace_id": "a" * 32,
            "span_id": "d" * 16,
            "name": "x",
            "attributes": {"nested": {"a": 1}, "none": None},
        }
        attrs = {
            item["key"]: item["value"]
            for item in exporter.build_payload(trace, [span])["resourceSpans"][0][
                "scopeSpans"
            ][0]["spans"][0]["attributes"]
        }
        self.assertIn("nested", attrs)
        self.assertIn("None", attrs["none"]["stringValue"])


class TestOTLPTransport(unittest.TestCase):
    def test_success_and_failure_with_injected_transport(self) -> None:
        calls = []

        def ok(endpoint, headers, payload):
            calls.append((endpoint, headers, payload))
            return 200

        exporter = OTLPExporter("http://collector", transport=ok)
        self.assertTrue(exporter.export_trace(sample_trace(), sample_spans()))
        self.assertEqual(calls[0][0], "http://collector")
        self.assertEqual(calls[0][1]["Content-Type"], "application/json")
        self.assertIn("resourceSpans", calls[0][2])

        self.assertFalse(
            OTLPExporter(
                "http://collector", transport=lambda *args: 500
            ).export_trace(sample_trace(), sample_spans())
        )
        self.assertFalse(
            OTLPExporter(
                "http://collector", transport=lambda *args: False
            ).export_trace(sample_trace(), sample_spans())
        )

    def test_transport_exception_returns_false(self) -> None:
        def boom(*args):
            raise OSError("network down")

        exporter = OTLPExporter("http://collector", transport=boom)
        self.assertFalse(exporter.export_trace(sample_trace(), sample_spans()))

    def test_export_traces_counts_successes(self) -> None:
        results = iter([200, 500, 201])
        exporter = OTLPExporter(
            "http://collector", transport=lambda *args: next(results)
        )
        exported = exporter.export_traces(
            [(sample_trace(), sample_spans())] * 3
        )
        self.assertEqual(exported, 2)


if __name__ == "__main__":
    unittest.main()
