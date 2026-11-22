"""Tests for Phase 2 cost, event-policy, and automatic-flush helpers."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from tracekit.core.cost import (
    InvalidTokenCountError,
    ModelPricing,
    PricingRegistry,
    UnknownModelError,
    add_span_cost_to_trace,
    add_trace_cost,
    aggregate_trace_cost,
    calculate_cost,
    default_registry,
    register_pricing,
    set_span_cost,
    set_trace_total_cost,
    span_cost,
    sum_span_costs,
    try_calculate_cost,
)
from tracekit.core.event import Event
from tracekit.core.events import (
    EventCollector,
    EventPolicy,
    cache_hit_event,
    cache_miss_event,
    db_query_complete_event,
    db_query_start_event,
    exception_event,
    llm_token_chunk_event,
    record_event,
    record_exception,
    record_span_event,
    record_trace_event,
    tool_retry_event,
)
from tracekit.core.flush_manager import AutomaticFlushManager
from tracekit.core.span import Span
from tracekit.core.trace import Trace

FIXED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0)


class TestPricingRegistry(unittest.TestCase):
    def test_separate_input_output_pricing_and_metadata(self):
        registry = PricingRegistry(include_defaults=False)
        pricing = registry.register(
            "my-model", 0.002, 0.01, source="vendor-sheet", version="v2"
        )
        self.assertEqual(pricing.input_per_1k, 0.002)
        self.assertEqual(pricing.output_per_1k, 0.01)
        self.assertEqual(pricing.source, "vendor-sheet")
        self.assertEqual(pricing.version, "v2")
        self.assertEqual(calculate_cost("my-model", 1000, 2000, registry=registry), 0.022)

    def test_builtin_reference_catalog_is_versioned(self):
        registry = default_registry()
        self.assertTrue(registry.supports("gpt-4o"))
        self.assertAlmostEqual(calculate_cost("gpt-4o", 1000, 1000), 0.0125)
        self.assertEqual(registry.get("gpt-4o").source, "tracekit-builtin")
        self.assertTrue(registry.get("gpt-4o").version)

    def test_missing_token_counts_are_zero(self):
        registry = PricingRegistry(include_defaults=False)
        registry.register("m", 0.01, 0.02)
        self.assertEqual(calculate_cost("m", registry=registry), 0.0)
        self.assertAlmostEqual(calculate_cost("m", 1000, registry=registry), 0.01)
        self.assertAlmostEqual(calculate_cost("m", output_tokens=1000, registry=registry), 0.02)

    def test_unknown_model_is_never_silently_free(self):
        registry = PricingRegistry(include_defaults=False)
        with self.assertRaises(UnknownModelError):
            calculate_cost("mystery-model", 100, 100, registry=registry)
        self.assertFalse(registry.supports("mystery-model"))
        self.assertIsNone(try_calculate_cost("mystery-model", 100, 100, registry=registry))
        span = Span(name="llm", trace_id="t")
        before = dict(span.attributes)
        with self.assertRaises(UnknownModelError):
            set_span_cost(span, "mystery-model", 100, 100, registry=registry)
        self.assertEqual(span.attributes, before)

    def test_custom_pricing_overrides_builtin_case_insensitively(self):
        registry = PricingRegistry(
            custom_pricing={"gpt-4o": (0.1, 0.2)},
            source="team-sheet",
            version="v7",
        )
        self.assertEqual(calculate_cost("GPT-4O", 1000, 1000, registry=registry), 0.3)
        entry = registry.get("gpt-4o")
        self.assertEqual((entry.source, entry.version), ("team-sheet", "v7"))
        self.assertEqual((registry.source, registry.version), ("team-sheet", "v7"))

    def test_custom_model_pricing_object_preserves_its_metadata(self):
        custom = ModelPricing("custom", 0.5, 0.25, source="contract", version="2026-Q4")
        registry = PricingRegistry(custom_pricing={"custom": custom}, include_defaults=False)
        self.assertEqual(registry.get("custom"), custom)
        self.assertAlmostEqual(calculate_cost("custom", 1000, 1000, registry=registry), 0.75)

    def test_unregister_and_discovery(self):
        registry = PricingRegistry(include_defaults=False)
        registry.register("a-model", 0.1, 0.2)
        registry.register("b-model", 0.3, 0.4)
        self.assertEqual(registry.models(), ("a-model", "b-model"))
        self.assertEqual(len(registry.entries()), 2)
        registry.unregister("A-MODEL")
        self.assertFalse(registry.supports("a-model"))

    def test_invalid_pricing_and_token_counts(self):
        with self.assertRaises(ValueError):
            ModelPricing("m", -0.1, 0.2)
        with self.assertRaises(ValueError):
            ModelPricing("m", 0.1, 0.2, source="")
        with self.assertRaises(InvalidTokenCountError):
            calculate_cost("gpt-4o", -1)
        with self.assertRaises(InvalidTokenCountError):
            calculate_cost("gpt-4o", input_tokens="abc")
        with self.assertRaises(InvalidTokenCountError):
            calculate_cost("gpt-4o", input_tokens=1.5)
        self.assertIsNone(try_calculate_cost("gpt-4o", "abc"))

    def test_register_pricing_on_default_registry(self):
        register_pricing(
            "unit-test-model",
            0.001,
            0.003,
            source="unit-test",
            version="1",
        )
        span = Span(name="llm", trace_id="t")
        self.assertAlmostEqual(set_span_cost(span, "unit-test-model", 1000, 1000), 0.004)


class TestCostHelpers(unittest.TestCase):
    def setUp(self):
        self.registry = PricingRegistry(include_defaults=False)
        self.registry.register("m", 0.01, 0.02, source="unit", version="v3")

    def test_set_span_cost_preserves_existing_attributes(self):
        span = Span(name="llm", trace_id="t", attributes={"model": "m", "keep": 1})
        cost = set_span_cost(span, "m", 500, 500, registry=self.registry)
        self.assertEqual(span.attributes["keep"], 1)
        self.assertEqual(span.attributes["model"], "m")
        self.assertEqual(span.attributes["cost_usd"], cost)
        self.assertEqual(span_cost(span), cost)
        details = span.attributes["cost_details"]
        self.assertEqual(details["source"], "unit")
        self.assertEqual(details["version"], "v3")
        self.assertEqual(details["input_tokens"], 500)
        self.assertEqual(details["output_tokens"], 500)
        self.assertEqual(details["currency"], "USD")

    def test_set_span_cost_can_keep_existing_cost(self):
        span = Span(name="llm", trace_id="t", attributes={"cost_usd": 9.0})
        cost = set_span_cost(span, "m", 1000, 0, registry=self.registry, overwrite=False)
        self.assertEqual(span.attributes["cost_usd"], 9.0)
        self.assertEqual(cost, 0.01)

    def test_trace_aggregation_preserves_metadata_and_is_idempotent(self):
        trace = Trace(name="t", metadata={"request_id": "r1", "sampled": True})
        spans = []
        for index in range(3):
            span = Span(name=f"s{index}", trace_id=trace.trace_id)
            set_span_cost(span, "m", 100, 100, registry=self.registry)
            spans.append(span)
        total = set_trace_total_cost(trace, spans)
        self.assertAlmostEqual(total, 0.009)
        self.assertEqual(trace.metadata["request_id"], "r1")
        self.assertTrue(trace.metadata["sampled"])
        self.assertEqual(trace.metadata["total_cost_usd"], total)
        self.assertEqual(aggregate_trace_cost(trace, spans), total)

    def test_incremental_cost_helpers(self):
        trace = Trace(name="t", metadata={"other": "kept"})
        self.assertAlmostEqual(add_trace_cost(trace, 0.25), 0.25)
        span = Span(name="s", trace_id="t", attributes={"cost_usd": 0.5})
        self.assertAlmostEqual(add_span_cost_to_trace(trace, span), 0.75)
        self.assertEqual(trace.metadata["other"], "kept")
        with self.assertRaises(TypeError):
            add_trace_cost(trace, "0.1")

    def test_sum_span_costs_skips_missing_or_invalid_values(self):
        spans = [Span(name=str(index), trace_id="t") for index in range(3)]
        spans[0].attributes["cost_usd"] = 0.1
        spans[1].attributes["cost_usd"] = "not-a-number"
        self.assertAlmostEqual(sum_span_costs(spans), 0.1)


class TestEventPolicies(unittest.TestCase):
    def setUp(self):
        self.policy = EventPolicy(clock=lambda: FIXED_TIME)

    def test_llm_token_chunk_event(self):
        event = llm_token_chunk_event(
            2,
            token_count=7,
            model="m",
            is_final=True,
            trace_id="t",
            span_id="s",
            policy=self.policy,
        )
        self.assertEqual(event.name, "llm.token_chunk")
        self.assertEqual(event.attributes["llm.chunk_index"], 2)
        self.assertEqual(event.attributes["llm.token_count"], 7)
        self.assertEqual(event.attributes["llm.model"], "m")
        self.assertTrue(event.attributes["llm.is_final"])
        self.assertEqual((event.trace_id, event.span_id), ("t", "s"))
        self.assertEqual(event.timestamp, FIXED_TIME)
        self.assertTrue(event.event_id)

    def test_tool_retry_and_database_events(self):
        retry = tool_retry_event(
            2,
            tool_name="search",
            reason="timeout",
            next_delay_ms=50,
            policy=self.policy,
        )
        self.assertEqual(retry.name, "tool.retry")
        self.assertEqual(retry.attributes["tool.attempt"], 2)
        self.assertEqual(retry.attributes["tool.name"], "search")
        self.assertEqual(retry.attributes["tool.retry_reason"], "timeout")
        self.assertEqual(retry.attributes["tool.next_delay_ms"], 50.0)

        start = db_query_start_event("q1", operation="select", table="users", policy=self.policy)
        complete = db_query_complete_event(
            "q1", duration_ms=12.5, row_count=3, success=True, policy=self.policy
        )
        self.assertEqual(start.name, "db.query.start")
        self.assertEqual(complete.name, "db.query.complete")
        self.assertEqual(start.attributes["db.table"], "users")
        self.assertEqual(complete.attributes["db.duration_ms"], 12.5)
        self.assertEqual(complete.attributes["db.row_count"], 3)
        self.assertTrue(complete.attributes["db.success"])

    def test_cache_hit_miss_and_exception_events(self):
        hit = cache_hit_event(cache_name="semantic", key_prefix="abc", policy=self.policy)
        miss = cache_miss_event(cache_name="semantic", policy=self.policy)
        self.assertEqual(hit.name, "cache.hit")
        self.assertEqual(miss.name, "cache.miss")
        self.assertTrue(hit.attributes["cache.hit"])
        self.assertFalse(miss.attributes["cache.hit"])
        self.assertEqual(hit.attributes["cache.key_prefix"], "abc")

        event = exception_event(
            ValueError("secret detail"), context={"tool": "search"}, policy=self.policy
        )
        self.assertEqual(event.name, "exception")
        self.assertEqual(event.attributes["exception.type"], "ValueError")
        self.assertEqual(event.attributes["tool"], "search")
        self.assertNotIn("exception.message", event.attributes)

    def test_no_sensitive_payloads_by_default(self):
        event = llm_token_chunk_event(
            0,
            payload="secret",
            api_key="sk-secret",
            prompt="private prompt",
            body="raw body",
            safe_note="ok",
            policy=self.policy,
        )
        for key in ("payload", "api_key", "prompt", "body"):
            self.assertNotIn(key, event.attributes)
        self.assertEqual(event.attributes["safe_note"], "ok")

        permissive = EventPolicy(capture_payloads=True, clock=lambda: FIXED_TIME)
        captured = llm_token_chunk_event(0, payload="secret", policy=permissive)
        self.assertEqual(captured.attributes["payload"], "secret")

    def test_attribute_and_exception_message_truncation(self):
        policy = EventPolicy(max_attribute_length=10, include_exception_message=True)
        event = tool_retry_event(1, reason="x" * 50, policy=policy)
        self.assertEqual(len(event.attributes["tool.retry_reason"]), 13)
        message = exception_event(RuntimeError("y" * 50), policy=policy)
        self.assertEqual(len(message.attributes["exception.message"]), 13)

    def test_collector_stable_order_and_capture_limit(self):
        policy = EventPolicy(max_events=3, clock=lambda: FIXED_TIME)
        collector = EventCollector(policy)
        collector.extend(llm_token_chunk_event(index, policy=policy) for index in range(5))
        self.assertEqual(len(collector), 3)
        self.assertEqual(
            [event.attributes["llm.chunk_index"] for event in collector.events], [2, 3, 4]
        )
        self.assertEqual(
            [event.attributes["event.seq"] for event in collector.ordered()], [2, 3, 4]
        )
        collector.clear()
        self.assertEqual(len(collector), 0)

    def test_record_span_and_trace_events(self):
        span = Span(name="s", trace_id="t", attributes={"keep": 1})
        event = llm_token_chunk_event(0, policy=self.policy)
        record_span_event(span, event, policy=self.policy)
        self.assertEqual(span.events, [event])
        self.assertEqual(span.attributes["keep"], 1)
        self.assertEqual(event.attributes["event.seq"], 0)

        trace = Trace(name="t", metadata={"request_id": "r"})
        trace_event = llm_token_chunk_event(1, policy=self.policy)
        record_trace_event(trace, trace_event, policy=self.policy)
        self.assertEqual(trace.metadata["request_id"], "r")
        stored = trace.metadata["events"][0]
        self.assertEqual(stored["name"], "llm.token_chunk")
        self.assertEqual(stored["attributes"]["event.seq"], 0)
        self.assertIsInstance(trace_event, Event)

    def test_record_event_dispatch_and_exception_helper(self):
        span = Span(name="s", trace_id="t")
        event = llm_token_chunk_event(0, policy=self.policy)
        self.assertIs(record_event(span, event, policy=self.policy), event)
        self.assertEqual(span.events, [event])

        trace = Trace(name="t")
        recorded = record_exception(trace, RuntimeError("boom"), policy=self.policy)
        self.assertEqual(recorded.name, "exception")
        self.assertEqual(trace.metadata["events"][0]["attributes"]["exception.type"], "RuntimeError")

    def test_event_policy_validation(self):
        with self.assertRaises(ValueError):
            EventPolicy(max_events=-1)
        with self.assertRaises(ValueError):
            EventPolicy(max_attribute_length=0)


class TickingSleep:
    def __init__(self):
        self.calls = 0

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        await asyncio.sleep(0)


class TestAutomaticFlushManager(unittest.IsolatedAsyncioTestCase):
    async def test_threshold_flush_resets_pending(self):
        calls = []
        manager = AutomaticFlushManager(
            lambda: calls.append(1),
            flush_interval_seconds=None,
            max_spans_in_memory=3,
            logger=Mock(),
        )
        self.assertFalse(manager.notify_pending(1))
        self.assertFalse(manager.notify_pending(1))
        self.assertTrue(manager.notify_pending(1))
        self.assertTrue(manager.threshold_reached)
        self.assertTrue(manager.maybe_flush())
        await _wait_until(lambda: manager.successful_flushes == 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.pending, 0)
        self.assertFalse(manager.maybe_flush())

    async def test_failure_preserves_pending_and_logs(self):
        state = {"fail": True, "calls": 0}

        async def flush() -> None:
            state["calls"] += 1
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("disk down")

        logger = Mock()
        manager = AutomaticFlushManager(
            flush,
            flush_interval_seconds=None,
            max_spans_in_memory=2,
            logger=logger,
        )
        manager.notify_pending(2)
        self.assertFalse(await manager.flush_now())
        self.assertEqual(manager.pending, 2)
        self.assertEqual(manager.failure_count, 1)
        self.assertIsInstance(manager.last_error, RuntimeError)
        logger.exception.assert_called_once()
        self.assertTrue(await manager.flush_now())
        self.assertEqual(manager.pending, 0)
        self.assertEqual(manager.successful_flushes, 1)
        self.assertEqual(state["calls"], 2)

    async def test_single_flight_prevents_concurrent_flushes(self):
        release = asyncio.Event()
        started = asyncio.Event()
        calls = []

        async def flush() -> None:
            calls.append(1)
            started.set()
            await release.wait()

        manager = AutomaticFlushManager(
            flush,
            flush_interval_seconds=None,
            max_spans_in_memory=1,
            logger=Mock(),
        )
        manager.notify_pending(5)
        first = asyncio.create_task(manager.flush_now())
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(await manager.flush_now())
        self.assertFalse(manager.maybe_flush())
        self.assertTrue(manager.is_flushing)
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertTrue(await asyncio.wait_for(first, 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.pending, 0)

    async def test_periodic_flush_and_clean_stop(self):
        sleep = TickingSleep()
        calls = []
        manager = AutomaticFlushManager(
            lambda: calls.append(1),
            flush_interval_seconds=30,
            max_spans_in_memory=10,
            sleep=sleep,
            logger=Mock(),
        )
        self.assertTrue(manager.start_loop())
        self.assertFalse(manager.start_loop())
        await _wait_until(lambda: manager.successful_flushes >= 2)
        self.assertGreaterEqual(manager.periodic_ticks, 2)
        self.assertEqual(sleep.calls, manager.periodic_ticks)
        self.assertTrue(manager.is_running)
        await manager.stop()
        self.assertFalse(manager.is_running)
        ticks_after_stop = manager.periodic_ticks
        flushes_after_stop = manager.successful_flushes
        await asyncio.sleep(0)
        self.assertEqual(manager.periodic_ticks, ticks_after_stop)
        self.assertEqual(manager.successful_flushes, flushes_after_stop)
        self.assertEqual(len(calls), flushes_after_stop)

    async def test_stop_cancels_inflight_flush_and_preserves_pending(self):
        started = asyncio.Event()

        async def flush() -> None:
            started.set()
            await asyncio.Event().wait()

        manager = AutomaticFlushManager(
            flush,
            flush_interval_seconds=None,
            max_spans_in_memory=1,
            logger=Mock(),
        )
        manager.notify_pending(1)
        task = asyncio.create_task(manager.flush_now())
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(manager.stop(), 1)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(manager.pending, 1)
        self.assertFalse(manager.is_flushing)

    async def test_records_added_during_flush_are_preserved(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def flush() -> None:
            started.set()
            await release.wait()
            manager.notify_pending(2)

        manager = AutomaticFlushManager(
            flush,
            flush_interval_seconds=None,
            max_spans_in_memory=1,
            logger=Mock(),
        )
        manager.notify_pending(1)
        task = asyncio.create_task(manager.flush_now())
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        self.assertTrue(await asyncio.wait_for(task, 1))
        self.assertEqual(manager.pending, 2)
        self.assertEqual(manager.successful_flushes, 1)


class TestAutomaticFlushManagerSync(unittest.TestCase):
    def test_no_loop_and_validation(self):
        manager = AutomaticFlushManager(
            lambda: None, flush_interval_seconds=None, max_spans_in_memory=1
        )
        self.assertFalse(manager.schedule_flush())
        self.assertFalse(manager.start_loop())
        with self.assertRaises(TypeError):
            AutomaticFlushManager("not callable")
        with self.assertRaises(ValueError):
            AutomaticFlushManager(lambda: None, flush_interval_seconds=-1)
        with self.assertRaises(ValueError):
            AutomaticFlushManager(lambda: None, max_spans_in_memory=-1)

    def test_manual_pending_bookkeeping(self):
        manager = AutomaticFlushManager(
            lambda: None, flush_interval_seconds=None, max_spans_in_memory=2
        )
        self.assertFalse(manager.notify_pending(0))
        manager.notify_pending(2)
        self.assertEqual(manager.pending, 2)
        manager.notify_flushed(1)
        self.assertEqual(manager.pending, 1)
        manager.clear_pending()
        self.assertEqual(manager.pending, 0)


if __name__ == "__main__":
    unittest.main()
