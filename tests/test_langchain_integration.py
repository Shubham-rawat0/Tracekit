import unittest
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
