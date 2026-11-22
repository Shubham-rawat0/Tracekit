import asyncio
import threading
import time
import unittest

from tracekit.streaming.manager import StreamManager


class TestStreamManager(unittest.TestCase):
    def test_multiple_subscribers_receive_events_in_order(self):
        manager = StreamManager(max_queue=100)
        first = []
        second = []
        done = threading.Event()
        lock = threading.Lock()

        def callback_one(event):
            with lock:
                first.append(event["sequence"])
            if len(first) == 3:
                done.set()

        def callback_two(event):
            with lock:
                second.append(event["sequence"])

        manager.subscribe(callback_one)
        manager.subscribe(callback_two)
        for sequence in range(3):
            self.assertTrue(
                manager.publish({"type": "span_start", "sequence": sequence})
            )
        self.assertTrue(done.wait(timeout=1.0))
        for _ in range(20):
            if len(second) == 3:
                break
            time.sleep(0.01)
        self.assertEqual(first, [0, 1, 2])
        self.assertEqual(second, [0, 1, 2])
        manager.close()

    def test_malformed_event_is_rejected(self):
        manager = StreamManager()
        for event in (None, [], {"trace_id": "missing-type"}, {"type": ""}):
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    manager.publish(event)
        manager.close()

    def test_subscriber_exception_does_not_affect_other_subscriber(self):
        manager = StreamManager()
        received = []
        finished = threading.Event()

        def broken(event):
            raise RuntimeError("subscriber failed")

        def healthy(event):
            received.append(event["type"])
            finished.set()

        manager.subscribe(broken)
        manager.subscribe(healthy)
        manager.publish({"type": "trace_end"})
        self.assertTrue(finished.wait(timeout=1.0))
        self.assertEqual(received, ["trace_end"])
        manager.close()

    def test_slow_subscriber_does_not_block_publish(self):
        manager = StreamManager(max_queue=10)
        started = threading.Event()
        release = threading.Event()

        def slow(event):
            started.set()
            release.wait(timeout=1.0)

        manager.subscribe(slow)
        manager.publish({"type": "span_start"})
        self.assertTrue(started.wait(timeout=1.0))
        started_at = time.monotonic()
        for sequence in range(20):
            manager.publish({"type": "span_end", "sequence": sequence})
        elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 0.5)
        release.set()
        manager.close()

    def test_async_callback_support(self):
        manager = StreamManager()
        received = []
        finished = threading.Event()

        async def callback(event):
            await asyncio.sleep(0.01)
            received.append(event["type"])
            finished.set()

        manager.subscribe(callback)
        manager.publish({"type": "trace_start"})
        self.assertTrue(finished.wait(timeout=1.0))
        self.assertEqual(received, ["trace_start"])
        manager.close()

    def test_drop_oldest_and_cleanup(self):
        manager = StreamManager(max_queue=2)
        blocked = threading.Event()
        release = threading.Event()

        def slow(event):
            blocked.set()
            release.wait(timeout=1.0)

        manager.subscribe(slow)
        for sequence in range(10):
            manager.publish({"type": "span_start", "sequence": sequence})
        self.assertTrue(blocked.wait(timeout=1.0))
        release.set()
        manager.close()
        self.assertFalse(manager.publish({"type": "trace_end"}))


if __name__ == "__main__":
    unittest.main()
