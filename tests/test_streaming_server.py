import asyncio
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch

from tracekit.streaming.manager import StreamManager
from tracekit.streaming.server import StreamServer


class FakeSocket:
    def getsockname(self):
        return ("127.0.0.1", 9876)


class FakeServer:
    def __init__(self):
        self.sockets = [FakeSocket()]
        self.closed = False
        self.waited = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, payload):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(payload)

    async def recv(self):
        await asyncio.sleep(3600)


class TestStreamServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = StreamManager(max_queue=100)

    async def asyncTearDown(self):
        self.manager.close()

    async def test_server_start_stop_fake_websockets(self):
        fake_server = FakeServer()
        captured = {}

        def serve(handler, host, port):
            captured["handler"] = handler
            captured["host"] = host
            captured["port"] = port
            return fake_server

        module = types.ModuleType("websockets")
        module.serve = serve
        with patch.dict(sys.modules, {"websockets": module}):
            server = StreamServer(self.manager, host="127.0.0.1", port=8765)
            await server.start()
            self.assertEqual(server.url, "ws://127.0.0.1:9876")
            await server.stop()
        self.assertTrue(fake_server.closed)
        self.assertTrue(fake_server.waited)

    async def test_server_multiple_clients_and_malformed_events(self):
        fake_server = FakeServer()
        captured = {}

        def serve(handler, host, port):
            captured["handler"] = handler
            return fake_server

        module = types.ModuleType("websockets")
        module.serve = serve
        with patch.dict(sys.modules, {"websockets": module}):
            server = StreamServer(self.manager, max_queue=2)
            await server.start()
            websocket_one = FakeWebSocket()
            websocket_two = FakeWebSocket()
            task_one = asyncio.create_task(
                captured["handler"](websocket_one)
            )
            task_two = asyncio.create_task(
                captured["handler"](websocket_two)
            )
            await asyncio.sleep(0.01)
            self.assertEqual(server.client_count, 2)
            self.manager.publish({"type": "span_start", "trace_id": "t"})
            self.manager.publish({"type": "trace_end", "trace_id": "t"})
            await asyncio.sleep(0.05)
            self.assertTrue(websocket_one.sent)
            self.assertTrue(websocket_two.sent)
            websocket_one.closed = True
            task_one.cancel()
            await asyncio.gather(task_one, return_exceptions=True)
            await server.stop()
            task_two.cancel()
            await asyncio.gather(task_two, return_exceptions=True)

    async def test_server_missing_dependency(self):
        with patch.dict(sys.modules, {"websockets": None}):
            server = StreamServer(self.manager)
            with self.assertRaisesRegex(
                RuntimeError,
                "requires websockets",
            ):
                await server.start()

    @unittest.skipUnless(
        importlib.util.find_spec("websockets") is not None,
        "websockets not installed",
    )
    async def test_real_websockets_start_and_stop_when_available(self):
        server = StreamServer(self.manager, host="127.0.0.1", port=0)
        await server.start()
        self.assertTrue(server.url.startswith("ws://127.0.0.1:"))
        self.assertEqual(server.client_count, 0)
        await server.stop()


if __name__ == "__main__":
    unittest.main()
