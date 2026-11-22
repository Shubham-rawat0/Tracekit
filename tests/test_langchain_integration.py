import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from tracekit.core.tracer import Tracer
from tracekit.integrations.langchain import TracekitCallbackHandler


class TestLangChainIntegration(unittest.TestCase):
    def test_langchain_callback_flow(self):
        tracer = Tracer()
        cb = TracekitCallbackHandler(tracer=tracer)
        chain_id = uuid4()
        llm_id = uuid4()
        tool_id = uuid4()

        with tracer.trace("agent-run") as tr:
            cb.on_chain_start({"name": "agent"}, {"q": "hi"}, run_id=chain_id)
            cb.on_llm_start({"name": "chat"}, ["hi"], run_id=llm_id, parent_run_id=chain_id, invocation_params={"model": "m"})
            class R:
                generations = [[type("G", (), {"text": "hello"})()]]
                llm_output = {"token_usage": {"total_tokens": 7}, "model_name": "m"}
            cb.on_llm_end(R(), run_id=llm_id, parent_run_id=chain_id)
            cb.on_tool_start({"name": "search"}, "hi", run_id=tool_id, parent_run_id=chain_id, inputs={"q": "hi"})
            cb.on_tool_end({"res": 1}, run_id=tool_id, parent_run_id=chain_id)
            cb.on_chain_end({"a": "b"}, run_id=chain_id)

        replay = tracer.build_trace_replay(tr.trace_id)
        self.assertIsNotNone(replay)
        self.assertEqual([s["span_type"] for s in replay["spans"]], ["chain", "llm", "tool"])
        self.assertEqual(replay["spans"][1]["attributes"]["token_usage"]["total_tokens"], 7)

    def test_tool_callbacks_from_worker_threads_stay_in_one_trace(self):
        tracer = Tracer()
        cb = TracekitCallbackHandler(tracer=tracer)
        chain_id = uuid4()
        tool_ids = [uuid4() for _ in range(6)]

        with tracer.trace("agent-run") as tr:
            cb.on_chain_start({"name": "agent"}, {"q": "hi"}, run_id=chain_id)
            chain_span = cb._runs[str(chain_id)]

            def run_tool(item):
                index, run_id = item
                cb.on_tool_start(
                    {"name": f"tool-{index}"},
                    "x",
                    run_id=run_id,
                    parent_run_id=chain_id,
                )
                cb.on_tool_end("ok", run_id=run_id, parent_run_id=chain_id)

            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(run_tool, enumerate(tool_ids)))
            cb.on_chain_end({"a": "b"}, run_id=chain_id)

        self.assertEqual([t.name for t in tracer._traces.values()], ["agent-run"])
        self.assertTrue(all(s.trace_id == tr.trace_id for s in tracer._spans.values()))
        self.assertTrue(all(
            s.parent_span_id == chain_span.span_id
            for s in tracer._spans.values()
            if s.name.startswith("tool-")
        ))

    def test_top_level_tool_callbacks_share_duration_and_spans_on_error(self):
        tracer = Tracer()
        cb = TracekitCallbackHandler(tracer=tracer)
        first, second = uuid4(), uuid4()

        cb.on_tool_start({"name": "first"}, "a", run_id=first)
        cb.on_tool_end("ok", run_id=first)
        cb.on_tool_start({"name": "second"}, "b", run_id=second)
        cb.on_tool_error(RuntimeError("boom"), run_id=second)
        trace = next(iter(tracer._traces.values()))
        trace.start_time -= timedelta(seconds=40)
        cb._close_idle_session()

        self.assertEqual(len(tracer._traces), 1)
        self.assertEqual(trace.status, "error")
        self.assertGreaterEqual(trace.duration_ms, 40_000)
        self.assertEqual(
            sorted(span.name for span in tracer._spans.values()),
            ["first", "second"],
        )

    def test_langchain_captures_usage_metadata_from_message(self):
        tracer = Tracer()
        cb = TracekitCallbackHandler(tracer=tracer)
        llm_id = uuid4()

        with tracer.trace("tokens") as tr:
            cb.on_llm_start({"name": "chat"}, ["hi"], run_id=llm_id, invocation_params={"model": "m"})
            message = SimpleNamespace(
                content="hello",
                usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            )
            generation = SimpleNamespace(text="hello", message=message)
            response = SimpleNamespace(generations=[[generation]], llm_output={"model_name": "m"})
            cb.on_llm_end(response, run_id=llm_id)

        replay = tracer.build_trace_replay(tr.trace_id)
        self.assertIsNotNone(replay)
        usage = replay["spans"][0]["attributes"]["token_usage"]
        self.assertEqual(usage["prompt_tokens"], 3)
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 7)
        self.assertEqual(tr.token_count, 7)

    def test_langchain_normalizes_provider_token_counts(self):
        tracer = Tracer()
        cb = TracekitCallbackHandler(tracer=tracer)
        llm_id = uuid4()

        cb.on_llm_start({"name": "chat"}, ["hi"], run_id=llm_id)
        generation = SimpleNamespace(
            text="hello",
            generation_info={
                "prompt_token_count": 5,
                "candidates_token_count": 6,
                "total_token_count": 11,
            },
        )
        span = cb._runs[str(llm_id)]
        response = SimpleNamespace(generations=[[generation]], llm_output={})
        cb.on_llm_end(response, run_id=llm_id)

        usage = span.attributes["token_usage"]
        self.assertEqual(usage["prompt_tokens"], 5)
        self.assertEqual(usage["completion_tokens"], 6)
        self.assertEqual(usage["total_tokens"], 11)


if __name__ == "__main__":
    unittest.main()
