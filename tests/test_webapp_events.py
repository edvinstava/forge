"""GET /api/sessions/{id}/events — the web app's live attach feed: replay the
bus buffer past `since`, then tail live events (heartbeats keep proxies from
reaping idle streams). Lets the web UI watch turns driven from Slack/CLI."""
import threading

from fastapi.testclient import TestClient

from forge.config import Config
from forge.eventbus import EventBus
from forge.events import TurnEvent
from forge.store import Store
from forge.webapp import create_app, bus_events


class FeedManager:
    from forge.providers import ClaudeProvider
    provider = ClaudeProvider()

    def __init__(self, store):
        self.store = store
        self.bus = EventBus()


def _client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = FeedManager(store)
    return TestClient(create_app(cfg, store, mgr)), store, mgr


def test_events_endpoint_replays_with_seq_and_origin(tmp_path):
    client, store, mgr = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    mgr.bus.publish("r1", TurnEvent("phase", {"name": "agent", "label": "Agent working"}),
                    origin="slack")
    mgr.bus.publish("r1", TurnEvent("done", {"message": "ok"}), origin="slack")
    r = client.get("/api/sessions/r1/events?since=0&tail=0")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: phase" in r.text and "event: done" in r.text
    assert '"seq": 1' in r.text and '"seq": 2' in r.text
    assert '"origin": "slack"' in r.text


def test_events_endpoint_since_filters_replay(tmp_path):
    client, store, mgr = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    mgr.bus.publish("r1", TurnEvent("phase", {"name": "a"}), origin="web")
    mgr.bus.publish("r1", TurnEvent("done", {"message": "ok"}), origin="web")
    r = client.get("/api/sessions/r1/events?since=1&tail=0")
    assert "event: phase" not in r.text and "event: done" in r.text


def test_events_endpoint_404_for_unknown_run(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/sessions/nope/events?tail=0").status_code == 404


def test_bus_events_tail_streams_live_then_closes(tmp_path):
    bus = EventBus()
    bus.publish("r", TurnEvent("phase", {"name": "a"}), origin="web")
    gen = bus_events(bus, "r", since=0, tail=True, heartbeat_secs=0.05)
    first = next(gen)
    assert "event: phase" in first and '"seq": 1' in first
    # live event published after the replay is delivered by the tail
    threading.Timer(0.01, lambda: bus.publish(
        "r", TurnEvent("done", {"message": "ok"}), origin="slack")).start()
    frames = []
    for f in gen:
        if f.startswith(":"):     # heartbeat while waiting
            continue
        frames.append(f)
        break
    assert "event: done" in frames[0] and '"origin": "slack"' in frames[0]
    gen.close()                   # client disconnect → subscription released


def test_bus_events_tail_only_skips_history(tmp_path):
    bus = EventBus()
    bus.publish("r", TurnEvent("phase", {"name": "old"}), origin="web")
    gen = bus_events(bus, "r", since=-1, tail=True, heartbeat_secs=0.02)
    threading.Timer(0.01, lambda: bus.publish(
        "r", TurnEvent("narration", {"text": "new"}), origin="web")).start()
    for f in gen:
        if f.startswith(":"):
            continue
        assert "old" not in f and "new" in f
        break
    gen.close()


def test_bus_events_emits_heartbeat_when_idle(tmp_path):
    bus = EventBus()
    gen = bus_events(bus, "r", since=0, tail=True, heartbeat_secs=0.01)
    assert next(gen).startswith(":")
    gen.close()


class OriginCapture(FeedManager):
    def __init__(self, store):
        super().__init__(store)
        self.origins = {}

    def can_start(self):
        return (True, "")

    def start(self, run_id, repo, source, origin="api"):
        self.origins["start"] = origin
        yield TurnEvent("url", {"web_url": "http://localhost:1"})

    def turn(self, run_id, prompt, model="auto", attachments=None, origin="api"):
        self.origins["turn"] = origin
        yield TurnEvent("done", {"message": "ok"})

    def plan_task(self, run_id, task, model="auto", attachments=None, origin="api"):
        self.origins["plan_task"] = origin
        yield TurnEvent("plan", {"goal": task})

    def respond_checkpoint(self, run_id, cid, action, body=None, model="auto",
                           origin="api"):
        self.origins["respond_checkpoint"] = origin
        yield TurnEvent("done", {"message": action})

    def wake(self, run_id, origin="api"):
        self.origins["wake"] = origin
        yield TurnEvent("phase", {"name": "wake"})


def test_web_routes_drive_flows_with_web_origin(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = OriginCapture(store)
    client = TestClient(create_app(cfg, store, mgr))
    store.create_run("r1", "o/r", "", "forge/x")
    client.post("/api/sessions", json={"repo": "o/r", "source": "github"})
    client.post("/api/sessions/r1/messages", json={"prompt": "x"})
    client.post("/api/sessions/r1/task", json={"task": "t"})
    client.post("/api/sessions/r1/checkpoints/1", json={"action": "approve"})
    client.post("/api/sessions/r1/wake")
    assert mgr.origins == {k: "web" for k in
                           ("start", "turn", "plan_task", "respond_checkpoint", "wake")}
