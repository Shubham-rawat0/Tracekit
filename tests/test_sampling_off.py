import unittest

from tracekit.core.context import set_current_span, set_current_trace, set_trace_context
from tracekit.core.propagation import SpanContext
from tracekit.core.sampling import AlwaysOffSampler, AlwaysOnSampler, RateLimitSampler
from tracekit.core.storage import StorageBackend
from tracekit.core.tracer import Tracer


class RecordingStorage(StorageBackend):
    def __init__(self):
        self.documents = []
        self._connected = False

    async def initialize(self):
        self._connected = True

    async def close(self):
        self._connected = False

    async def insert_one(self, collection, document):
        self.documents.append((collection, dict(document)))

    async def find_one(self, collection, filter):
        for name, document in self.documents:
            if name == collection and all(document.get(key) == value for key, value in filter.items()):
                return dict(document)
        return None

    async def find_many(self, collection, filter=None, sort=None, limit=None):
        documents = [
            dict(document)
            for name, document in self.documents
            if name == collection
            and all(document.get(key) == value for key, value in (filter or {}).items())
        ]
        if limit is not None:
            documents = documents[:limit]
        return documents

    async def count_documents(self, collection, filter=None):
        return len(await self.find_many(collection, filter=filter))

    async def create_index(self, collection, key_or_list, unique=False):
        return None

    @property
    def is_connected(self):
        return self._connected

    @property
    def storage_type(self):
        return "recording"

    @property
    def storage_detail(self):
        return "recording"


class TestSamplingOff(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)
        self.storage = RecordingStorage()

    async def asyncTearDown(self):
        set_current_trace(None)
        set_current_span(None)
        set_trace_context(None)

    async def _assert_trace_and_spans_not_recorded(self, tracer, trace):
        self.assertFalse(trace.metadata["sampled"])
        self.assertNotIn(trace.trace_id, tracer._traces)
        span = tracer.start_span("unsampled-child")
        self.assertFalse(span.attributes["sampled"])
        self.assertNotIn(span.span_id, tracer._spans)
        tracer.end_span(span)
        tracer.end_trace(trace)
        await tracer.flush()
        self.assertEqual(self.storage.documents, [])

    async def test_always_off_and_zero_rate_are_not_buffered_or_flushed(self):
        off_tracer = Tracer(storage=self.storage, sampler=AlwaysOffSampler())
        off_trace = off_tracer.start_trace("always-off")
        await self._assert_trace_and_spans_not_recorded(off_tracer, off_trace)

        zero_rate_tracer = Tracer(storage=self.storage, sampler=RateLimitSampler(0.0))
        zero_rate_trace = zero_rate_tracer.start_trace("zero-rate")
        await self._assert_trace_and_spans_not_recorded(zero_rate_tracer, zero_rate_trace)

    async def test_remote_unsampled_context_is_not_recorded(self):
        remote = SpanContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            sampled=False,
        )
        tracer = Tracer(storage=self.storage, sampler=AlwaysOnSampler())
        trace = tracer.start_trace("remote-unsampled", parent_context=remote)
        self.assertFalse(trace.metadata["sampled"])
        span = tracer.start_span("remote-child", parent_context=remote)
        self.assertFalse(span.attributes["sampled"])
        self.assertNotIn(trace.trace_id, tracer._traces)
        self.assertNotIn(span.span_id, tracer._spans)
        tracer.end_span(span)
        tracer.end_trace(trace)
        await tracer.flush()
        self.assertEqual(self.storage.documents, [])

    async def test_always_on_still_records_and_flushes(self):
        tracer = Tracer(storage=self.storage, sampler=AlwaysOnSampler())
        trace = tracer.start_trace("always-on")
        span = tracer.start_span("recorded-child")
        self.assertTrue(trace.metadata["sampled"])
        self.assertIn(trace.trace_id, tracer._traces)
        self.assertIn(span.span_id, tracer._spans)
        tracer.end_span(span)
        tracer.end_trace(trace)
        await tracer.flush()
        self.assertEqual([collection for collection, _ in self.storage.documents], ["traces", "spans"])


if __name__ == "__main__":
    unittest.main()
