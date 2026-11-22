"""Local dashboard HTTP server for tracekit."""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.db import TraceStore


def _build_handler(loop, backend, store, token, static_dir, bind_host):
    from tracekit.dashboard.api import make_handler

    return make_handler(loop, backend, store, token, static_dir, bind_host)


def start_dashboard_server(db_path, host="127.0.0.1", port=0, token="test-token"):
    # Loopback by default; port 0 lets the OS pick a free port for embedded use.
    loop = asyncio.new_event_loop()
    # Store/backend coroutines run here; synchronous HTTP handlers marshal work onto this loop.
    backend = SQLiteBackend(db_path=db_path)
    store = TraceStore(backend)
    try:
        loop.run_until_complete(backend.initialize())
        # Resolve assets relative to this module, not the process working directory.
        static_dir = Path(__file__).resolve().parent / "static"
        handler_class = _build_handler(loop, backend, store, token, static_dir, host)
        server = ThreadingHTTPServer((host, port), handler_class)
    except OSError:
        # A bind failure must release the initialized backend and loop before re-raising.
        try:
            loop.run_until_complete(backend.close())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        raise
    server.daemon_threads = True
    # Stash resources on the server so stop_dashboard_server can find them later.
    setattr(server, "dashboard_backend", backend)
    setattr(server, "dashboard_loop", loop)
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    # Daemon threads let an embedding process exit without explicitly stopping the server.
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    setattr(server, "dashboard_http_thread", http_thread)
    return server, loop, loop_thread


def stop_dashboard_server(server, loop, loop_thread):
    # Best-effort teardown: each resource is closed independently so one failure cannot abort the rest.
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    try:
        # Stop the loop from its own thread, then join both worker threads.
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    if loop_thread is not None:
        try:
            loop_thread.join(timeout=5)
        except Exception:
            pass
    try:
        http_thread = getattr(server, "dashboard_http_thread", None)
        if http_thread is not None and http_thread.is_alive():
            http_thread.join(timeout=5)
    except Exception:
        pass
    try:
        backend = getattr(server, "dashboard_backend", None)
        if backend is not None:
            loop.run_until_complete(backend.close())
    except Exception:
        pass
    try:
        loop.close()
    except Exception:
        pass


def run_dashboard(db_path, host, port, token):
    server, loop, loop_thread = start_dashboard_server(db_path, host, port, token)
    try:
        # Block until Ctrl-C; daemon threads keep serving in the background.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_dashboard_server(server, loop, loop_thread)
    return 0
