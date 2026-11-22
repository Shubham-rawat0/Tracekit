from types import SimpleNamespace
from uuid import uuid4

import pytest
from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.tracer import Tracer
from tracekit.integrations.langchain import TracekitCallbackHandler


@pytest.fixture(autouse=True)
def deterministic_estimation(monkeypatch):
    monkeypatch.setenv("TRACEKIT_TOKEN_ESTIMATION_JITTER", "0")


def _response(text, usage=None, model="m"):
    generation = SimpleNamespace(
        text=text,
        generation_info=usage or {},
    )
    return SimpleNamespace(
        generations=[[generation]],
        llm_output={"model_name": model},
    )


def test_provider_usage_is_preferred_and_stored_on_span_and_trace():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    run_id = uuid4()

    with tracer.trace("provider") as trace:
        handler.on_llm_start(
            {"name": "llm"},
            ["hello world this should not be estimated"],
            run_id=run_id,
            invocation_params={"model": "m"},
        )
        span = handler._runs[str(run_id)]
        handler.on_llm_end(
            _response(
                "short",
                {
                    "usage_metadata": {
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "total_tokens": 5,
                    }
                },
            ),
            run_id=run_id,
        )

    assert span.input_tokens == 2
    assert span.output_tokens == 3
    assert span.total_tokens == 5
    assert span.token_usage_source == "provider"
    assert span.attributes["token_usage"]["prompt_tokens"] == 2
    assert span.attributes["token_usage"]["completion_tokens"] == 3
    assert trace.input_tokens == 2
    assert trace.output_tokens == 3
    assert trace.total_tokens == 5
    assert trace.token_count == 5
    assert trace.token_usage_source == "provider"


def test_provider_specific_metadata_aliases_are_extracted():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    run_id = uuid4()

    with tracer.trace("google"):
        handler.on_llm_start({"name": "llm"}, ["hi"], run_id=run_id)
        span = handler._runs[str(run_id)]
        handler.on_llm_end(
            _response(
                "hello",
                {
                    "prompt_token_count": 5,
                    "candidates_token_count": 6,
                    "total_token_count": 11,
                },
            ),
            run_id=run_id,
        )

    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (5, 6, 11)
    assert span.token_usage_source == "provider"


def test_missing_usage_estimates_input_and_output_independently():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    chain_id = uuid4()
    llm_id = uuid4()
    tool_id = uuid4()

    with tracer.trace("estimated") as trace:
        handler.on_chain_start({"name": "chain"}, {"q": "hi"}, run_id=chain_id)
        handler.on_llm_start(
            {"name": "llm"},
            ["abcd"],
            run_id=llm_id,
            parent_run_id=chain_id,
        )
        llm_span = handler._runs[str(llm_id)]
        handler.on_llm_end(_response("abcdefgh"), run_id=llm_id, parent_run_id=chain_id)
        handler.on_tool_start(
            {"name": "tool"}, "x", run_id=tool_id, parent_run_id=chain_id
        )
        tool_span = handler._runs[str(tool_id)]
        handler.on_tool_end({"ok": True}, run_id=tool_id, parent_run_id=chain_id)
        handler.on_chain_end({"done": True}, run_id=chain_id)
        chain_span = next(
            span for span in tracer._spans.values() if span.span_type == "chain"
        )

    assert (llm_span.input_tokens, llm_span.output_tokens) == (1, 2)
    assert llm_span.total_tokens == 3
    assert llm_span.token_usage_source == "estimated"
    assert tool_span.input_tokens is None
    assert chain_span.input_tokens is None
    assert (trace.input_tokens, trace.output_tokens, trace.total_tokens) == (1, 2, 3)
    assert trace.token_usage_source == "estimated"


def test_jitter_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("TRACEKIT_TOKEN_ESTIMATION_JITTER", "0.1")
    monkeypatch.setattr(
        "tracekit.core.tokens.random.uniform", lambda low, high: high
    )
    tracer = Tracer()
    trace = tracer.start_trace("jitter")
    span = tracer.start_llm_span("llm", prompt="a" * 40, trace=trace)

    tracer.record_llm_result(span, response="b" * 80)
    tracer.end_span(span)
    tracer.end_trace(trace)

    assert (span.input_tokens, span.output_tokens) == (11, 22)
    assert span.token_usage_source == "estimated"


def test_later_estimation_cannot_replace_provider_counts():
    tracer = Tracer()
    trace = tracer.start_trace("priority")
    span = tracer.start_llm_span("llm", prompt="abcd", trace=trace)

    tracer.record_llm_result(
        span,
        response="abcdefgh",
        token_usage={"input_tokens": 7, "output_tokens": 9, "total_tokens": 16},
    )
    tracer.record_llm_result(span, response="a different long response")

    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (7, 9, 16)
    assert span.token_usage_source == "provider"


def test_null_usage_metadata_falls_back_to_estimation():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    run_id = uuid4()

    with tracer.trace("null-usage"):
        handler.on_llm_start({"name": "llm"}, ["abcd"], run_id=run_id)
        span = handler._runs[str(run_id)]
        handler.on_llm_end(
            _response("abcdefgh", {"input_tokens": None, "output_tokens": None}),
            run_id=run_id,
        )

    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (1, 2, 3)
    assert span.token_usage_source == "estimated"


def test_partial_provider_usage_does_not_estimate_missing_side():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    run_id = uuid4()

    with tracer.trace("partial"):
        handler.on_llm_start({"name": "llm"}, ["some long prompt"], run_id=run_id)
        span = handler._runs[str(run_id)]
        handler.on_llm_end(
            _response("some long response", {"input_tokens": 4}),
            run_id=run_id,
        )

    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (4, 0, 4)
    assert span.token_usage_source == "provider"


def test_empty_input_and_output_produce_zero_estimated_tokens():
    tracer = Tracer()
    trace = tracer.start_trace("empty")
    span = tracer.start_llm_span("llm", prompt="", trace=trace)

    tracer.record_llm_result(span, response="")
    tracer.end_span(span)
    tracer.end_trace(trace)

    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (0, 0, 0)
    assert span.token_usage_source == "estimated"
    assert trace.total_tokens == 0


def test_multiple_and_nested_llm_spans_aggregate_once():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    chain_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()

    with tracer.trace("multiple") as trace:
        handler.on_chain_start({"name": "agent"}, {}, run_id=chain_id)
        handler.on_llm_start(
            {"name": "first"}, ["a"], run_id=first_id, parent_run_id=chain_id
        )
        first = handler._runs[str(first_id)]
        handler.on_llm_end(
            _response("b", {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}),
            run_id=first_id,
            parent_run_id=chain_id,
        )
        handler.on_llm_start(
            {"name": "second"}, ["c"], run_id=second_id, parent_run_id=chain_id
        )
        second = handler._runs[str(second_id)]
        handler.on_llm_end(
            _response("d", {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}),
            run_id=second_id,
            parent_run_id=chain_id,
        )
        tracer.record_llm_result(
            first,
            token_usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )
        handler.on_chain_end({"ok": True}, run_id=chain_id)

    assert first.parent_span_id == second.parent_span_id
    assert (trace.input_tokens, trace.output_tokens, trace.total_tokens) == (6, 8, 14)
    assert trace.token_usage_source == "provider"


def test_failed_llm_invocation_records_estimated_usage_without_breaking_lifecycle():
    tracer = Tracer()
    handler = TracekitCallbackHandler(tracer=tracer)
    run_id = uuid4()

    with tracer.trace("failed") as trace:
        handler.on_llm_start({"name": "llm"}, ["abcd"], run_id=run_id)
        span = handler._runs[str(run_id)]
        handler.on_llm_new_token("abcdefgh", run_id=run_id)
        handler.on_llm_error(RuntimeError("boom"), run_id=run_id)

    assert span.status == "error"
    assert span.error == "boom"
    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (1, 2, 3)
    assert span.token_usage_source == "estimated"
    assert trace.total_tokens == 3


def test_estimation_failure_does_not_break_llm_execution(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("estimator unavailable")

    monkeypatch.setattr("tracekit.core.tracer.estimate_token_usage", fail)
    tracer = Tracer()
    trace = tracer.start_trace("safe")
    span = tracer.start_llm_span("llm", prompt="abcd", trace=trace)

    result = tracer.record_llm_result(span, response="abcdefgh")
    tracer.end_span(span)
    tracer.end_trace(trace)

    assert result is span
    assert (span.input_tokens, span.output_tokens, span.total_tokens) == (0, 0, 0)
    assert span.token_usage_source == "estimated"


@pytest.mark.asyncio
async def test_token_columns_round_trip_through_sqlite(tmp_path):
    backend = SQLiteBackend(db_path=tmp_path / "tokens.db")
    await backend.initialize()
    try:
        await backend.insert_one(
            "traces",
            {
                "trace_id": "trace-tokens",
                "name": "tokens",
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
                "token_usage_source": "provider",
            },
        )
        await backend.insert_one(
            "spans",
            {
                "span_id": "span-tokens",
                "trace_id": "trace-tokens",
                "name": "llm",
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
                "token_usage_source": "provider",
            },
        )

        trace = await backend.find_one("traces", {"trace_id": "trace-tokens"})
        span = await backend.find_one("spans", {"span_id": "span-tokens"})
        assert trace is not None
        assert span is not None
        for field, value in (
            ("input_tokens", 3),
            ("output_tokens", 4),
            ("total_tokens", 7),
            ("token_usage_source", "provider"),
        ):
            assert trace[field] == value
            assert span[field] == value
    finally:
        await backend.close()
