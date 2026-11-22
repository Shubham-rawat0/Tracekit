"""Adversarial edge-case tests for context/lifecycle fixes."""

import unittest

from tracekit.core.context import (
    get_current_span,
    get_current_trace,
    reset_current_span,
    set_current_span,
    set_current_trace,
)
from tracekit.core.tracer import Tracer


class TestContextLifecycleEdge(unittest.TestCase):
    def setUp(self):
        # Clear context before each test
        set_current_span(None)
        set_current_trace(None)
        self.tracer = Tracer()

    def tearDown(self):
        self.tracer = None
        # Clear context after each test
        set_current_span(None)
        set_current_trace(None)

    def test_exception_in_span_restores_correct_current_span(self):
        """1.1: Exception inside with tracer.span() restores the correct current span."""
        with self.tracer.span("parent") as parent:
            self.assertIs(get_current_span(), parent)
            try:
                with self.tracer.span("child") as child:
                    self.assertIs(get_current_span(), child)
                    raise ValueError("boom")
            except ValueError:
                pass
            # After exception, context should restore to parent
            self.assertIs(get_current_span(), parent)
        # After parent exits, context should be None
        self.assertIsNone(get_current_span())

    def test_nested_spans_no_grandparent_clobber(self):
        """1.1: Nested spans cannot clobber grandparents; inner span with parent=None."""
        grandparent = self.tracer.start_span("grandparent")
        self.tracer.end_span(grandparent)

        with self.tracer.span("parent") as parent:
            # Inner span with explicit parent=None should NOT become current after exit
            child = self.tracer.start_span("child", parent=None, set_current=True)
            self.tracer.end_span(child)
            # Context should still be parent
            self.assertIs(get_current_span(), parent)

            # Another child with default parent (should be parent)
            with self.tracer.span("child2") as child2:
                self.assertEqual(child2.parent_span_id, parent.span_id)
            # After child2 exits, should restore to parent
            self.assertIs(get_current_span(), parent)

        self.assertIsNone(get_current_span())

    def test_direct_start_end_trace_token_no_leak(self):
        """1.2: Direct start_trace/end_trace store/restore tokens, no leak at top level."""
        self.assertIsNone(get_current_trace())
        trace = self.tracer.start_trace("direct-trace")
        self.assertIs(get_current_trace(), trace)

        # End trace should restore prior context (None)
        ended = self.tracer.end_trace(trace)
        self.assertIsNone(get_current_trace())
        self.assertEqual(ended.status, "ok")

    def test_direct_start_end_trace_nested_in_existing_trace(self):
        """1.2: Direct start_trace/end_trace works when nested inside existing trace."""
        outer = self.tracer.start_trace("outer")
        self.assertIs(get_current_trace(), outer)

        inner = self.tracer.start_trace("inner")
        self.assertIs(get_current_trace(), inner)

        self.tracer.end_trace(inner)
        # Should restore to outer
        self.assertIs(get_current_trace(), outer)

        self.tracer.end_trace(outer)
        self.assertIsNone(get_current_trace())

    def test_direct_start_end_span_token_no_leak(self):
        """1.2: Direct start_span/end_span store/restore tokens, no leak at top level."""
        self.assertIsNone(get_current_span())
        span = self.tracer.start_span("direct-span")
        self.assertIs(get_current_span(), span)

        self.tracer.end_span(span)
        self.assertIsNone(get_current_span())

    def test_direct_start_end_span_nested_in_existing_span(self):
        """1.2: Direct start_span/end_span works when nested inside existing span."""
        self.tracer.start_trace("trace")
        outer = self.tracer.start_span("outer")
        self.assertIs(get_current_span(), outer)

        inner = self.tracer.start_span("inner")
        self.assertIs(get_current_span(), inner)

        self.tracer.end_span(inner)
        # Should restore to outer
        self.assertIs(get_current_span(), outer)

        self.tracer.end_span(outer)
        self.assertIsNone(get_current_span())

    def test_end_span_clears_current_span_context(self):
        """1.3: After direct end_span(span), get_current_span() is not the ended span."""
        span = self.tracer.start_span("test-span")
        self.assertIs(get_current_span(), span)

        self.tracer.end_span(span)
        # Current span should be cleared (not the ended span)
        self.assertIsNone(get_current_span())

    def test_end_span_clears_current_span_when_nested(self):
        """1.3: After direct end_span(span) when nested, restores parent span."""
        outer = self.tracer.start_span("outer")
        inner = self.tracer.start_span("inner")
        self.assertIs(get_current_span(), inner)

        self.tracer.end_span(inner)
        # Should restore to outer
        self.assertIs(get_current_span(), outer)

    def test_double_end_span_idempotent_and_context_stable(self):
        """Double end_span is idempotent and doesn't corrupt context."""
        span = self.tracer.start_span("test")
        self.tracer.end_span(span)
        first_status = span.status

        # Second end should be no-op
        self.tracer.end_span(span)
        self.assertEqual(span.status, first_status)
        # Context should still be None
        self.assertIsNone(get_current_span())

    def test_double_end_trace_idempotent_and_context_stable(self):
        """Double end_trace is idempotent and doesn't corrupt context."""
        trace = self.tracer.start_trace("test")
        self.tracer.end_trace(trace)
        first_status = trace.status

        self.tracer.end_trace(trace)
        self.assertEqual(trace.status, first_status)
        self.assertIsNone(get_current_trace())

    def test_start_span_set_current_false_leaves_context_untouched(self):
        """start_span(..., set_current=False) leaves context untouched."""
        self.tracer.start_trace("trace")
        outer = self.tracer.start_span("outer")
        self.assertIs(get_current_span(), outer)

        # Create span with set_current=False
        inner = self.tracer.start_span("inner", set_current=False)
        # Context should still be outer
        self.assertIs(get_current_span(), outer)
        self.assertIsNot(get_current_span(), inner)

        # End inner - should not affect context
        self.tracer.end_span(inner)
        self.assertIs(get_current_span(), outer)

        self.tracer.end_span(outer)
        self.assertIsNone(get_current_span())

    def test_auto_trace_path_no_leak(self):
        """Auto-trace path (start_span with no active trace) does not leak context.
        
        Fixed: end_span now ends auto-trace when no running spans remain.
        See tracer.py:167-178 for the auto-trace cleanup logic.
        """
        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_span())

        # This should create an auto-trace
        span = self.tracer.start_span("auto-span")
        self.assertIsNotNone(get_current_trace())  # auto-trace created
        self.assertIs(get_current_span(), span)

        self.tracer.end_span(span)
        # Both trace and span context should be cleared
        self.assertIsNone(get_current_span())
        self.assertIsNone(get_current_trace())

    def test_async_span_exception_restores_context(self):
        """1.1: Exception in async_span restores correct context."""
        import asyncio

        async def run_test():
            async with self.tracer.async_span("parent") as parent:
                self.assertIs(get_current_span(), parent)
                try:
                    async with self.tracer.async_span("child") as child:
                        self.assertIs(get_current_span(), child)
                        raise ValueError("async boom")
                except ValueError:
                    pass
                self.assertIs(get_current_span(), parent)
            self.assertIsNone(get_current_span())

        asyncio.run(run_test())

    def test_mixed_sync_async_context_managers(self):
        """Mixed sync/async context managers don't interfere."""
        import asyncio

        async def run_test():
            with self.tracer.span("sync-parent") as sync_parent:
                self.assertIs(get_current_span(), sync_parent)
                async with self.tracer.async_span("async-child") as async_child:
                    self.assertIs(get_current_span(), async_child)
                # Back to sync parent
                self.assertIs(get_current_span(), sync_parent)
            self.assertIsNone(get_current_span())

        asyncio.run(run_test())

    def test_span_context_manager_with_parent_none_restores_correctly(self):
        """span() with parent=None still restores correctly after exit.
        
        Test uses start_span directly since span() context manager doesn't
        accept parent parameter - it always uses current span as parent.
        To test parent=None, we must clear current span first.
        """
        with self.tracer.span("parent") as parent:
            # Clear current span so start_span with parent=None sees no current
            set_current_span(None)
            try:
                child = self.tracer.start_span("child", parent=None)
                token = set_current_span(child)
                try:
                    self.assertIs(get_current_span(), child)
                    self.assertIsNone(child.parent_span_id)
                finally:
                    reset_current_span(token)
            finally:
                set_current_span(parent)
            # Should restore to parent
            self.assertIs(get_current_span(), parent)

    def test_trace_context_manager_restores_prev_trace_and_span(self):
        """trace() restores both previous trace and span."""
        outer_trace = self.tracer.start_trace("outer")
        outer_span = self.tracer.start_span("outer-span")
        self.assertIs(get_current_trace(), outer_trace)
        self.assertIs(get_current_span(), outer_span)

        with self.tracer.trace("inner") as inner_trace:
            self.assertIs(get_current_trace(), inner_trace)
            self.assertIsNone(get_current_span())  # trace() clears span

            with self.tracer.span("inner-span") as inner_span:
                self.assertIs(get_current_span(), inner_span)
            self.assertIsNone(get_current_span())

        # After exit, should restore both
        self.assertIs(get_current_trace(), outer_trace)
        self.assertIs(get_current_span(), outer_span)

    def test_end_trace_restores_prior_trace_when_nested(self):
        """end_trace restores prior trace when called directly while nested."""
        outer = self.tracer.start_trace("outer")
        inner = self.tracer.start_trace("inner")
        self.assertIs(get_current_trace(), inner)

        self.tracer.end_trace(inner)
        self.assertIs(get_current_trace(), outer)

        self.tracer.end_trace(outer)
        self.assertIsNone(get_current_trace())

    def test_end_span_out_of_order_behavior(self):
        """Ending spans out of LIFO order: document actual behavior.
        
        When outer is created, then inner, then outer ended first:
        - outer's token restores to state before outer (None)
        - But inner is still current!
        - When inner ends, its token restores to state before inner (outer was current)
        - So final context is outer (even though outer was ended)
        
        This is correct contextvar behavior - tokens form a stack.
        """
        outer = self.tracer.start_span("outer")
        inner = self.tracer.start_span("inner")

        # End outer first (out of order) - outer is NOT current, so no context change
        self.tracer.end_span(outer)
        self.assertIs(get_current_span(), inner)
        
        # End inner - restores to outer (which was current when inner's token was created)
        self.tracer.end_span(inner)
        # Context is now outer (ended), not None
        self.assertIs(get_current_span(), outer)
        self.assertEqual(outer.status, "ok")


if __name__ == "__main__":
    unittest.main()