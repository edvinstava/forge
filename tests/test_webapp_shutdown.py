"""Ctrl+C must actually stop `forge web`. The web UI holds SSE streams open
(EventSource auto-reconnects, heartbeats keep them alive), and uvicorn's
graceful shutdown waits for open connections — so shutdown must (a) close all
bus subscriptions so tailing streams end immediately, and (b) bound the wait
for anything else still streaming (an in-flight turn)."""
import signal
import socket
import threading
import time

import pytest

from forge.config import Config
from forge.eventbus import EventBus
from forge.store import Store
from forge.webapp import create_app, make_server

uvicorn = pytest.importorskip("uvicorn")


class FeedManager:
    from forge.providers import ClaudeProvider
    provider = ClaudeProvider()

    def __init__(self, store):
        self.store = store
        self.bus = EventBus()


def _app(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = FeedManager(store)
    return create_app(cfg, store, mgr), store, mgr


def test_make_server_exit_closes_bus_and_bounds_graceful_wait(tmp_path):
    app, store, mgr = _app(tmp_path)
    server = make_server(app, mgr.bus, "127.0.0.1", 0)
    assert server.config.timeout_graceful_shutdown  # backstop for turn streams
    sub = mgr.bus.subscribe("r1")
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit
    assert sub.closed


def test_server_shuts_down_with_open_sse_connection(tmp_path):
    app, store, mgr = _app(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    server = make_server(app, mgr.bus, "127.0.0.1", 0)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server never started"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    # Attach a live SSE client (tail mode) and read the replay so the stream
    # is inside its blocking tail wait — the state Ctrl+C used to hang on.
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET /api/sessions/r1/events?since=0&tail=1 HTTP/1.1\r\n"
              b"Host: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n")
    assert b"200" in s.recv(4096)

    server.handle_exit(signal.SIGINT, None)
    t.join(timeout=8)
    s.close()
    assert not t.is_alive(), "server still waiting on the open SSE connection"
