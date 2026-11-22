import unittest
from types import SimpleNamespace
from uuid import uuid4

from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.tracer import Tracer
from tracekit.integrations.langchain import TracekitCallbackHandler


class _FakeLLM:
    def __init__(self, callbacks=None):
        self.callbacks = list(callbacks or [])


class TestLangChainGaps(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._clear_context()

    async def asyncTearDown(self):
        self._clear_context()

    @staticmethod
    def _clear_context():
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    async def test_async_callbacks_delegate_to_sync_handlers(self):
        tracer = Tracer()
        handler = TracekitCallbackHandler(tracer=tracer)
        chain_id = uuid4()
        llm_id = uuid4()
        tool_id = uuid4()

        class Response:
            generations = [[SimpleNamespace(text="done")]]
            llm_output = {"token_usage": {"total_tokens": 4}, "model_name": "m"}

        await handler.on_chain_start_async({"name": "chain"}, {"q": "hi"}, run_id=chain_id)
        await handler.on_llm_start_async(
            {"name": "llm"},
            ["hi"],
            run_id=llm_id,
            parent_run_id=chain_id,
            invocation_params={"model": "m"},
        )
        await handler.on_llm_new_token_async("done", run_id=llm_id)
        await handler.on_llm_end_async(Response(), run_id=llm_id, parent_run_id=chain_id)
        await handler.on_tool_start_async(
            {"name": "tool"}, "x", run_id=tool_id, parent_run_id=chain_id, inputs={"q": "x"}
        )
        await handler.on_tool_end_async({"ok": True}, run_id=tool_id, parent_run_id=chain_id)
        await handler.on_chain_end_async({"a": "b"}, run_id=chain_id)

        self.assertNotIn(str(chain_id), handler._runs)
        self.assertNotIn(str(llm_id), handler._runs)
        self.assertNotIn(str(tool_id), handler._runs)

    async def test_streaming_tokens_are_recorded(self):
        tracer = Tracer()
        handler = TracekitCallbackHandler(tracer=tracer)
        run_id = uuid4()
        handler.on_llm_start({"name": "llm"}, ["hi"], run_id=run_id)
        handler.on_llm_new_token("Hel", run_id=run_id)
        handler.on_llm_new_token("lo", run_id=run_id)

        span = handler._runs[str(run_id)]
        self.assertEqual(span.attributes["streaming_tokens"], ["Hel", "lo"])
        self.assertEqual(span.attributes["streaming_output"], "Hello")
        event_names = [event.name for event in span.events]
        self.assertEqual(event_names.count("llm.new_token"), 2)

    async def test_wrap_is_opt_in_and_restorable(self):
        handler = TracekitCallbackHandler()
        llm = _FakeLLM(["existing"])

        wrapped = handler.wrap(llm)
        self.assertIs(wrapped.target, llm)
        self.assertEqual(llm.callbacks, ["existing", handler])

        with handler.wrap(llm) as scoped:
            self.assertIs(scoped, llm)
            self.assertEqual(llm.callbacks.count(handler), 1)

        self.assertEqual(llm.callbacks, ["existing", handler])
        wrapped.__exit__(None, None, None)
        self.assertEqual(llm.callbacks, ["existing"])

        bare = _FakeLLM()
        del bare.callbacks
        scoped = handler.wrap(bare)
        self.assertEqual(bare.callbacks, [handler])
        scoped.__exit__(None, None, None)
        self.assertFalse(hasattr(bare, "callbacks"))

    async def test_retry_events_are_recorded(self):
        tracer = Tracer()
        handler = TracekitCallbackHandler(tracer=tracer)
        run_id = uuid4()
        handler.on_chain_start({"name": "chain"}, {}, run_id=run_id)
        retry_state = SimpleNamespace(attempt=2, exception=RuntimeError("rate limit"))

        handler.on_retry_start(retry_state, run_id=run_id)
        handler.on_retry_end(retry_state, run_id=run_id)

        span = handler._runs[str(run_id)]
        event_names = [event.name for event in span.events]
        self.assertIn("retry", event_names)
        self.assertIn("retry.end", event_names)
        self.assertEqual(span.attributes["retry_count"], 1)
        retry_end = [event for event in span.events if event.name == "retry.end"][0]
        self.assertFalse(retry_end.attributes["success"])
        self.assertIn("rate limit", retry_end.attributes["error"])


if __name__ == "__main__":
    unittest.main()
