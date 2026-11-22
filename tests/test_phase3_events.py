import tempfile
import unittest
from pathlib import Path

from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.db import TraceStore
from tracekit.core.event import Event
from tracekit.core.span import Span
from tracekit.core.trace import Trace
from tracekit.core.tracer import Tracer


class TestSpanEvents(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_add_event_accepts_name_and_event_and_does_not_restart_span(self):
        span = Span(name="work", trace_id="trace-1")

        named_event = span.add_event("queued", {"attempt": 1})
        self.assertIsInstance(named_event, Event)
        self.assertEqual(named_event.trace_id, span.trace_id)
        self.assertEqual(named_event.span_id, span.span_id)
        self.assertEqual(named_event.attributes, {"attempt": 1})

        provided_event = Event(name="finished", attributes={"ok": True})
        returned_event = span.add_event(provided_event)
        self.assertIs(returned_event, provided_event)
        self.assertEqual(provided_event.trace_id, span.trace_id)
        self.assertEqual(provided_event.span_id, span.span_id)
        self.assertEqual(len(span.events), 2)

        span.end(status="ok")
        ended_at = span.end_time
        error = span.add_event("after-end")
        self.assertIs(error, span.events[-1])
        self.assertEqual(span.status, "ok")
        self.assertEqual(span.end_time, ended_at)

    def test_record_exception_marks_error_without_ending_span(self):
        span = Span(name="work", trace_id="trace-1")
        event = span.record_exception(ValueError("bad input"), {"component": "parser"})

        self.assertEqual(event.name, "exception")
        self.assertEqual(event.attributes["exception.type"], "ValueError")
        self.assertEqual(event.attributes["exception.message"], "bad input")
        self.assertEqual(event.attributes["component"], "parser")
        self.assertEqual(span.status, "error")
        self.assertEqual(span.error, "bad input")
        self.assertIsNone(span.end_time)

        span.error = "original error"
        span.record_exception(RuntimeError("later error"))
        self.assertEqual(span.error, "original error")

    def test_event_serialization_round_trip_and_trace_replay(self):
        tracer = Tracer()
        with tracer.trace("event-replay") as trace:
            with tracer.span("worker") as span:
                span.add_event("started", {"step": 1})
                span.add_event(Event(name="progress", attributes={"step": 2}))

        replay = tracer.build_trace_replay(trace.trace_id)
        self.assertEqual(len(replay["spans"]), 1)
        self.assertEqual(
            [event["name"] for event in replay["spans"][0]["events"]],
            ["started", "progress"],
        )

        restored = Span.from_dict(span.to_dict())
        self.assertEqual(len(restored.events), 2)
        self.assertTrue(all(isinstance(event, Event) for event in restored.events))
        self.assertEqual(restored.events[0].to_dict(), span.events[0].to_dict())

    def test_trace_events_are_stored_as_metadata_dicts(self):
        trace = Trace(name="trace-events")
        event = trace.add_event("checkpoint", {"stage": "load"})
        self.assertEqual(event.trace_id, trace.trace_id)
        self.assertIsNone(event.span_id)
        self.assertEqual(trace.metadata["events"], [event.to_dict()])
        self.assertFalse(hasattr(trace, "events"))

        exception_event = trace.record_exception(KeyError("missing"), {"source": "test"})
        self.assertEqual(exception_event.name, "exception")
        self.assertEqual(exception_event.attributes["exception.type"], "KeyError")
        self.assertEqual(exception_event.attributes["exception.message"], "'missing'")
        self.assertEqual(trace.status, "error")
        self.assertEqual(len(trace.metadata["events"]), 2)


class TestSQLiteSpanEventPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_span_events_persist_in_sqlite_and_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = SQLiteBackend(Path(temp_dir) / "events.db")
            await backend.initialize()
            try:
                tracer = Tracer(storage=backend)
                trace = tracer.start_trace("sqlite-events")
                span = tracer.start_span("worker")
                span.add_event("persisted", {"source": "unit-test"})
                tracer.end_span(span)
                tracer.end_trace(trace)
                await tracer.flush()

                replay = await TraceStore(backend).get_trace_replay(trace.trace_id)
                self.assertIsNotNone(replay)
                self.assertEqual(len(replay["spans"]), 1)
                events = replay["spans"][0]["events"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["name"], "persisted")
                self.assertEqual(events[0]["attributes"], {"source": "unit-test"})
            finally:
                await backend.close()


if __name__ == "__main__":
    unittest.main()
