import asyncio
import sys
import unittest
from unittest.mock import patch

from tracekit.transport.webhook import WebhookTransport


class TestWebhookTransport(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_delivery_and_success(self):
        calls = []

        def transport(endpoint, headers, event):
            calls.append((endpoint, headers, event))
            return 204

        webhook = WebhookTransport(
            "https://example.test/events",
            max_queue=2,
            transport=transport,
        )
        await webhook.start()
        self.assertTrue(webhook.publish({"type": "trace_start"}))
        self.assertTrue(webhook.publish({"type": "trace_end"}))
        await asyncio.sleep(0.05)
        await webhook.stop()
        self.assertEqual(len(calls), 2)
        self.assertEqual(webhook.successes, 2)
        self.assertEqual(webhook.failures, 0)

    async def test_retry_and_failure_isolation(self):
        attempts = []

        def transport(endpoint, headers, event):
            attempts.append(event["type"])
            return 503

        webhook = WebhookTransport(
            "https://example.test/events",
            transport=transport,
            max_retries=2,
        )
        await webhook.start()
        webhook.publish({"type": "span_end"})
        await asyncio.sleep(0.05)
        await webhook.stop()
        self.assertEqual(attempts, ["span_end", "span_end", "span_end"])
        self.assertEqual(webhook.failures, 1)
        self.assertEqual(webhook.retries, 2)

    async def test_drop_oldest_when_queue_full(self):
        webhook = WebhookTransport(
            "https://example.test/events",
            max_queue=1,
            transport=lambda *args: 200,
        )
        await webhook.start()
        self.assertTrue(webhook.publish({"type": "trace_start"}))
        self.assertTrue(webhook.publish({"type": "trace_end"}))
        await asyncio.sleep(0.02)
        await webhook.stop()
        self.assertGreaterEqual(webhook.dropped, 0)

    async def test_missing_httpx_error(self):
        webhook = WebhookTransport("https://example.test/events")
        with patch.dict(sys.modules, {"httpx": None}):
            with self.assertRaisesRegex(RuntimeError, "requires httpx"):
                await webhook.start()


if __name__ == "__main__":
    unittest.main()
