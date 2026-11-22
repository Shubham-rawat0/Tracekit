import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    get_trace_context,
    set_current_span,
    set_current_trace,
    set_trace_context,
)
from tracekit.core.db import TraceStore
from tracekit.core.decorators import span as span_decorator
from tracekit.core.span import Span, SpanType
from tracekit.core.tracer import Tracer
from tracekit.integrations.langchain import TracekitCallbackHandler


class TestSpanTypeReliability(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_span_type_is_used_by_tracer_decorator_and_langchain(self):
        tracer = Tracer()
        with tracer.trace("typed-spans") as trace:
            direct = tracer.start_span("direct", span_type="  llm ")
            self.assertIs(direct.span_type, SpanType.LLM)
            tracer.end_span(direct)

            llm = tracer.start_llm_span("llm")
            self.assertIs(llm.span_type, SpanType.LLM)
            tracer.end_span(llm)

            tool = tracer.start_tool_span("tool")
            self.assertIs(tool.span_type, SpanType.TOOL)
            tracer.end_span(tool)

            with tracer.span("context", span_type=" chain ") as context_span:
                self.assertIs(context_span.span_type, SpanType.CHAIN)

            @span_decorator("decorated", span_type=" agent ", tracer=tracer)
            def decorated():
                return "done"

            self.assertEqual(decorated(), "done")

            handler = TracekitCallbackHandler(tracer)
            run_id = uuid4()
            handler.on_chain_start({"name": "chain"}, {}, run_id=run_id)
            chain_span = handler._runs[str(run_id)]
            self.assertIs(chain_span.span_type, SpanType.CHAIN)
            handler.on_chain_end({}, run_id=run_id)

            llm_run_id = uuid4()
            handler.on_llm_start(
                {"name": "chat"},
                ["prompt"],
                run_id=llm_run_id,
                invocation_params={"model": "model"},
            )
            llm_span = handler._runs[str(llm_run_id)]
            self.assertIs(llm_span.span_type, SpanType.LLM)

            class LLMResponse:
                generations = [[type("Generation", (), {"text": "response"})()]]
                llm_output = {"token_usage": {"total_tokens": 2}}

            handler.on_llm_end(LLMResponse(), run_id=llm_run_id)

            tool_run_id = uuid4()
            handler.on_tool_start(
                {"name": "search"},
                "query",
                run_id=tool_run_id,
            )
            tool_span = handler._runs[str(tool_run_id)]
            self.assertIs(tool_span.span_type, SpanType.TOOL)
            handler.on_tool_end({"result": "found"}, run_id=tool_run_id)

        spans = tracer.get_spans_for_trace(trace.trace_id)
        decorator_span = next(span for span in spans if span.name == "decorated")
        self.assertIs(decorator_span.span_type, SpanType.AGENT)

    def test_span_type_validation_setter_and_serialization(self):
        tracer = Tracer()
        with self.assertRaises(ValueError):
            tracer.start_span("invalid", span_type="not-a-span-type")
        with self.assertRaises(ValueError):
            with tracer.span("invalid", span_type="not-a-span-type"):
                pass

        span = Span(name="round-trip", trace_id="trace-id", span_type=" LLM ")
        self.assertIs(span.span_type, SpanType.LLM)
        span.type = " tool "
        self.assertIs(span.type, SpanType.TOOL)

        serialized = span.to_dict()
        self.assertEqual(serialized["span_type"], "tool")
        restored = Span.from_dict(serialized)
        self.assertIs(restored.span_type, SpanType.TOOL)
        self.assertEqual(restored.to_dict()["span_type"], "tool")

    def test_end_trace_llm_aggregation_accepts_enum_type(self):
        tracer = Tracer()
        trace = tracer.start_trace("llm-aggregation")
        llm = tracer.start_llm_span(prompt="question")
        tracer.record_llm_result(llm, response="answer", total_tokens=7)
        tracer.end_span(llm)
        tracer.end_trace(trace)

        self.assertEqual(trace.metadata["prompt"], "question")
        self.assertEqual(trace.metadata["response"], "answer")
        self.assertEqual(trace.metadata["total_tokens"], 7)


class TestAsyncSpanTypeReliability(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    async def test_async_span_and_decorator_normalize_span_type(self):
        tracer = Tracer()
        async with tracer.async_trace("async-types") as trace:
            async with tracer.async_span("async-context", span_type=" embedding ") as sp:
                self.assertIs(sp.span_type, SpanType.EMBEDDING)

            @span_decorator("async-decorated", span_type=" retriever ", tracer=tracer)
            async def decorated():
                return "done"

            self.assertEqual(await decorated(), "done")

        spans = tracer.get_spans_for_trace(trace.trace_id)
        self.assertTrue(all(isinstance(sp.span_type, SpanType) for sp in spans))
        self.assertIn(SpanType.RETRIEVER, {sp.span_type for sp in spans})


class TestFlushReliability(unittest.IsolatedAsyncioTestCase):
    async def test_flush_twice_does_not_duplicate_sqlite_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = SQLiteBackend(Path(temp_dir) / "tracekit.db")
            await backend.initialize()
            try:
                tracer = Tracer(storage=backend)
                store = TraceStore(backend)
                with tracer.trace("flush-idempotent") as trace:
                    with tracer.span("persisted", span_type=SpanType.TOOL):
                        pass

                await tracer.flush()
                await tracer.flush()

                trace_cursor = await backend._conn.execute("SELECT COUNT(*) FROM traces")
                span_cursor = await backend._conn.execute("SELECT COUNT(*) FROM spans")
                trace_count = (await trace_cursor.fetchone())[0]
                span_count = (await span_cursor.fetchone())[0]
                self.assertEqual(trace_count, 1)
                self.assertEqual(span_count, 1)

                replay = await store.get_trace_replay(trace.trace_id)
                self.assertIsNotNone(replay)
                self.assertEqual(len(replay["spans"]), 1)
                self.assertEqual(replay["spans"][0]["span_type"], "tool")
            finally:
                await backend.close()


class TestRequireTraceReliability(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_require_trace_rejects_missing_trace_and_allows_overrides(self):
        strict = Tracer(require_trace=True)
        for start in (
            strict.start_span,
            strict.start_llm_span,
            strict.start_tool_span,
        ):
            with self.assertRaises(RuntimeError):
                start("requires-trace")
        self.assertIsNone(get_current_trace())

        default = Tracer()
        auto_span = default.start_span("auto")
        self.assertEqual(get_current_trace().name, "auto-trace")
        default.end_span(auto_span)
        self.assertIsNone(get_current_trace())

        for start in (
            strict.start_span,
            strict.start_llm_span,
            strict.start_tool_span,
        ):
            span = start("override", require_trace=False)
            self.assertEqual(get_current_trace().name, "auto-trace")
            strict.end_span(span)
            self.assertIsNone(get_current_trace())

        with strict.trace("explicit"):
            with strict.span("context"):
                pass


class TestTraceBaggageReliability(unittest.TestCase):
    def setUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def tearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    def test_start_end_trace_sets_and_restores_baggage(self):
        tracer = Tracer()
        outer = tracer.start_trace("outer")
        outer_baggage = get_trace_context()
        self.assertIsInstance(outer_baggage, dict)
        self.assertEqual(outer_baggage["trace_id"], outer.trace_id)

        inner = tracer.start_trace("inner")
        inner_baggage = get_trace_context()
        self.assertIsInstance(inner_baggage, dict)
        self.assertEqual(inner_baggage["trace_id"], inner.trace_id)

        tracer.end_trace(inner)
        self.assertEqual(get_trace_context(), outer_baggage)
        tracer.end_trace(outer)
        self.assertIsNone(get_trace_context())

    def test_start_end_trace_restores_previous_baggage(self):
        tracer = Tracer()
        previous_baggage = {"request_id": "request-123"}
        set_trace_context(previous_baggage)

        trace = tracer.start_trace("with-existing-baggage")
        current_baggage = get_trace_context()
        self.assertIsInstance(current_baggage, dict)
        self.assertEqual(current_baggage["trace_id"], trace.trace_id)

        tracer.end_trace(trace)
        self.assertEqual(get_trace_context(), previous_baggage)


if __name__ == "__main__":
    unittest.main()
