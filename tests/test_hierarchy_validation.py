import unittest

from tracekit.core.trace import warn_on_hierarchy_violations


class TestHierarchyValidation(unittest.TestCase):
    def test_valid_hierarchy_does_not_warn(self):
        spans = [
            {
                "trace_id": "t1",
                "span_id": "root",
                "parent_span_id": None,
                "name": "root",
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-01-01T00:00:10Z",
            },
            {
                "trace_id": "t1",
                "span_id": "child",
                "parent_span_id": "root",
                "name": "child",
                "start_time": "2026-01-01T00:00:01Z",
                "end_time": "2026-01-01T00:00:09Z",
            },
        ]
        with self.assertNoLogs("tracekit.core.trace", level="WARNING"):
            warn_on_hierarchy_violations(spans)

    def test_child_outside_parent_logs_warning(self):
        spans = [
            {
                "trace_id": "t1",
                "span_id": "root",
                "parent_span_id": None,
                "name": "root",
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-01-01T00:00:05Z",
            },
            {
                "trace_id": "t1",
                "span_id": "child",
                "parent_span_id": "root",
                "name": "child",
                "start_time": "2026-01-01T00:00:01Z",
                "end_time": "2026-01-01T00:00:09Z",
            },
        ]
        with self.assertLogs("tracekit.core.trace", level="WARNING") as captured:
            warn_on_hierarchy_violations(spans)
        self.assertTrue(any("not contained within parent root" in line for line in captured.output))

    def test_parent_ids_are_compared_as_strings(self):
        spans = [
            {
                "trace_id": 1,
                "span_id": 100,
                "parent_span_id": None,
                "name": "root",
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-01-01T00:00:10Z",
            },
            {
                "trace_id": 1,
                "span_id": "200",
                "parent_span_id": "100",
                "name": "child",
                "start_time": "2026-01-01T00:00:01Z",
                "end_time": "2026-01-01T00:00:09Z",
            },
        ]
        with self.assertNoLogs("tracekit.core.trace", level="WARNING"):
            warn_on_hierarchy_violations(spans)
