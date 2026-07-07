from fastapi.testclient import TestClient
from forge.webapp import create_app
from forge.config import Config, Budget
from forge.store import Store


def test_refresh_proxy_writes_split_caddyfile(tmp_path, monkeypatch):
    from forge import webapp, proxy
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=1, max_wall_secs=10))
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "", "b")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")
    store.reserve_supabase("r1", 100, "demo")
    monkeypatch.setattr(proxy, "connect_networks", lambda ids: None)
    monkeypatch.setattr(proxy, "reload_proxy", lambda: None)

    webapp.refresh_proxy(store, cfg)

    caddyfile = (cfg.runs_dir / "Caddyfile").read_text()
    assert "run-r1.forge.localhost" in caddyfile
    assert "reverse_proxy @supabase http://host.docker.internal:54421" in caddyfile


class FakeManager:
    from forge.providers import ClaudeProvider
    provider = ClaudeProvider()
    def __init__(self, store, runs_dir=None):
        self.store = store; self.runs_dir = runs_dir
        self.turn_calls, self.task_calls = [], []
    def can_start(self): return (True, "")
    def diff(self, run_id): return ""
    def save_attachment(self, run_id, filename, data, mimetype=None):
        from forge import inbox
        return inbox.save(self.runs_dir, run_id, filename, data, mimetype=mimetype)
    def turn(self, run_id, prompt, model="auto", attachments=None, origin="api"):
        self.turn_calls.append((run_id, prompt, attachments)); yield from ()
    def plan_task(self, run_id, task, model="auto", policy=None, autonomous=False,
                  auto_draft=None, attachments=None, origin="api"):
        self.task_calls.append((run_id, task, attachments)); yield from ()


def _client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = FakeManager(store, runs_dir=cfg.runs_dir)
    return TestClient(create_app(cfg, store, mgr)), store, mgr


def test_list_sessions_empty(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_config_exposes_proxy_domain_and_port(tmp_path):
    # The web app derives the DNS-free local preview URL
    # (http://run-<id>.<domain>:<port>) from these, so it must learn them.
    client, _, _ = _client(tmp_path)
    body = client.get("/api/config").json()
    assert body["proxy_domain"] == "forge.localhost"
    assert body["proxy_port"] == 8088
    assert body["provider"] == "claude"
    assert "auto" in body["model_choices"]


def test_session_detail_includes_messages_not_tokens(tmp_path):
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    store.add_message("r1", "user", "hello")
    body = client.get("/api/sessions/r1").json()
    assert body["run_id"] == "r1"
    assert body["messages"][0]["content"] == "hello"
    assert "oauth_token" not in str(body) and "gh_token" not in str(body)


def test_diff_404_for_unknown_session(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/sessions/nope/diff").status_code == 404


def test_diff_ok_for_known_session(tmp_path):
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    assert client.get("/api/sessions/r1/diff").json() == {"diff": ""}


def test_verify_404_for_unknown_session(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/sessions/nope/verify").status_code == 404


def test_verify_returns_tristate_and_output(tmp_path):
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    store.add_message("r1", "assistant", "done", meta={
        "verify_ok": False, "diff_files": 2,
        "verify_failed": ["test"], "verify_output": "FAIL src/x.test.ts",
        "model": "opus"})
    body = client.get("/api/sessions/r1/verify").json()
    assert body["verify_ok"] is False
    assert body["diff_files"] == 2
    assert body["verify_failed"] == ["test"]
    assert "FAIL" in body["verify_output"]
    assert body["model"] == "opus"


def test_verify_ok_none_when_no_assistant_message(tmp_path):
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    body = client.get("/api/sessions/r1/verify").json()
    assert body["verify_ok"] is None and body["verify_failed"] == []


# --- Task 8: image attachment upload endpoint + threading ---

def test_upload_attachment_roundtrip(tmp_path):
    client, store, mgr = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    r = client.post("/api/sessions/r1/attachments?name=bug.png",
                    content=b"\x89PNG", headers={"Content-Type": "image/png"})
    assert r.status_code == 200
    name = r.json()["name"]
    assert name.endswith("bug.png")


def test_upload_404_unknown_session(tmp_path):
    client, _, _ = _client(tmp_path)
    r = client.post("/api/sessions/nope/attachments?name=a.png",
                    content=b"x", headers={"Content-Type": "image/png"})
    assert r.status_code == 404


def test_upload_415_non_image(tmp_path):
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    r = client.post("/api/sessions/r1/attachments?name=a.txt",
                    content=b"x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_upload_413_oversize(tmp_path):
    from forge import inbox
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    r = client.post("/api/sessions/r1/attachments?name=a.png",
                    content=b"x" * (inbox.MAX_BYTES + 1),
                    headers={"Content-Type": "image/png"})
    assert r.status_code == 413


def test_message_post_threads_attachments(tmp_path):
    client, store, mgr = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    client.post("/api/sessions/r1/messages",
                json={"prompt": "fix", "attachments": ["1-a.png"]})
    assert mgr.turn_calls == [("r1", "fix", ["1-a.png"])]


def test_task_post_threads_attachments(tmp_path):
    client, store, mgr = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    client.post("/api/sessions/r1/task",
                json={"task": "build", "attachments": ["1-a.png"]})
    assert mgr.task_calls == [("r1", "build", ["1-a.png"])]


def test_message_passes_model_choice_to_manager(tmp_path):
    # The model picked in the UI must reach manager.turn().
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    seen = {}

    class ModelManager(StreamManager):
        def turn(self, run_id, prompt, model="auto", attachments=None, origin="api"):
            seen["model"] = model
            yield TurnEvent("done", {"message": "ok"})

    client = TestClient(create_app(cfg, store, ModelManager(store)))
    with client.stream("POST", "/api/sessions/r1/messages",
                       json={"prompt": "x", "model": "haiku"}) as r:
        "".join(r.iter_text())
    assert seen["model"] == "haiku"


# --- Task 12: SSE streaming tests ---
from forge.session import TurnEvent


class StreamManager(FakeManager):
    def can_start(self): return (True, "")
    def start(self, run_id, repo, source, origin="api"):
        yield TurnEvent("phase", {"name": "clone"})
        yield TurnEvent("url", {"web_url": "http://localhost:5599"})
    def turn(self, run_id, prompt, model="auto", attachments=None, origin="api"):
        yield TurnEvent("model", {"choice": model, "resolved": "sonnet"})
        yield TurnEvent("narration", {"text": "editing"})
        yield TurnEvent("done", {"message": "ok", "diff_files": 1})


def _stream_client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    return TestClient(create_app(cfg, store, StreamManager(store)))


def test_start_streams_sse(tmp_path):
    client = _stream_client(tmp_path)
    with client.stream("POST", "/api/sessions", json={"repo": "o/r", "source": "github"}) as r:
        body = "".join(chunk for chunk in r.iter_text())
    # The first frame must be the synthetic `session` event carrying the run_id
    # the client uses to bucket subsequent provisioning events.
    assert body.index("event: session") < body.index("event: url")
    assert '"run_id"' in body
    assert "event: url" in body
    assert "http://localhost:5599" in body


def test_message_streams_sse(tmp_path):
    client = _stream_client(tmp_path)
    with client.stream("POST", "/api/sessions/r1/messages", json={"prompt": "x"}) as r:
        body = "".join(chunk for chunk in r.iter_text())
    assert "event: narration" in body and "event: done" in body


def test_start_at_cap_returns_409(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    class Full(StreamManager):
        def can_start(self): return (False, "max reached")
    client = TestClient(create_app(cfg, store, Full(store)))
    r = client.post("/api/sessions", json={"repo": "o/r", "source": "github"})
    assert r.status_code == 409


# --- Task 13: action endpoints ---
class ActionManager(StreamManager):
    def open_pr(self, run_id): return {"ok": True, "pr_url": "u", "draft": True}
    def stop(self, run_id): return None
    def end(self, run_id, reason="manual"): return None


def test_pr_stop_delete(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    client = TestClient(create_app(cfg, store, ActionManager(store)))
    assert client.post("/api/sessions/r1/pr").json()["pr_url"] == "u"
    assert client.post("/api/sessions/r1/stop").json()["stopped"] is True
    assert client.delete("/api/sessions/r1").json()["ended"] is True


def test_pr_returns_400_when_not_ok(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    class BadPrManager(ActionManager):
        def open_pr(self, run_id): return {"ok": False, "reason": "no_changes"}
    client = TestClient(create_app(cfg, store, BadPrManager(store)))
    r = client.post("/api/sessions/r1/pr")
    assert r.status_code == 400
    assert r.json()["reason"] == "no_changes"


# --- sleep / wake endpoints ---
class LifecycleManager(StreamManager):
    def __init__(self, store):
        super().__init__(store)
        self.slept = []
        self.sleep_reasons = {}
        self.end_reasons = {}
    def sleep(self, run_id, reason="manual"):
        self.slept.append(run_id)
        self.sleep_reasons[run_id] = reason
        return True
    def end(self, run_id, reason="manual"):
        self.end_reasons[run_id] = reason
    def wake(self, run_id, origin="api"):
        yield TurnEvent("phase", {"name": "wake", "label": "Waking"})
        yield TurnEvent("url", {"web_url": "http://localhost:5599"})


def _lifecycle_client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = LifecycleManager(store)
    return TestClient(create_app(cfg, store, mgr)), store, mgr


def test_sleep_endpoint_calls_manager(tmp_path):
    client, store, mgr = _lifecycle_client(tmp_path)
    store.create_run("r1", "o/r", "", "b")
    store.create_env("r1", "forge-r1", None, 3000, "live")
    r = client.post("/api/sessions/r1/sleep")
    assert r.status_code == 200 and r.json() == {"asleep": True}
    assert "r1" in mgr.slept


def test_sleep_endpoint_404_for_unknown(tmp_path):
    client, _, _ = _lifecycle_client(tmp_path)
    assert client.post("/api/sessions/nope/sleep").status_code == 404


def test_sleep_endpoint_passes_web_reason(tmp_path):
    # The Slack sweep notice reads this cause to label (or suppress) its message.
    client, store, mgr = _lifecycle_client(tmp_path)
    store.create_run("r1", "o/r", "", "b")
    store.create_env("r1", "forge-r1", None, 3000, "live")
    client.post("/api/sessions/r1/sleep")
    assert mgr.sleep_reasons.get("r1") == "web"


def test_end_endpoint_passes_web_reason(tmp_path):
    client, store, mgr = _lifecycle_client(tmp_path)
    store.create_run("r1", "o/r", "", "b")
    store.set_state("r1", "running")   # a queued run would take the cancel path
    r = client.delete("/api/sessions/r1")
    assert r.json() == {"ended": True}
    assert mgr.end_reasons.get("r1") == "web"


def test_wake_endpoint_streams(tmp_path):
    client, store, _ = _lifecycle_client(tmp_path)
    store.create_run("r1", "o/r", "", "b")
    with client.stream("POST", "/api/sessions/r1/wake") as r:
        body = "".join(chunk for chunk in r.iter_text())
    assert "event: url" in body and "http://localhost:5599" in body


def test_session_detail_includes_branch(tmp_path):
    client, store, _ = _lifecycle_client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/session-r1")
    assert client.get("/api/sessions/r1").json()["branch"] == "forge/session-r1"


def test_make_app_factory_builds_from_env(tmp_path, monkeypatch):
    # `forge web --reload` runs uvicorn against the import string
    # "forge.webapp:make_app"; the factory must rebuild a working app from env.
    monkeypatch.setenv("FORGE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    from forge.webapp import make_app
    app = make_app()
    assert app.title == "forge"
    assert "/api/sessions" in {r.path for r in app.routes}


# --- tunnel lifecycle (Task 9) ---

def test_tunnel_reconcile_stops_dead_runs():
    from forge.webapp import tunnel_reconcile
    assert tunnel_reconcile(live_ids={"a", "b"}, tunnel_ids={"a", "b", "c"}) == {"c"}
    assert tunnel_reconcile(live_ids={"a"}, tunnel_ids=set()) == set()


def test_tunnel_sweep_stops_orphans_and_fires_transitions(tmp_path):
    from forge.webapp import tunnel_sweep
    store = Store(tmp_path / "f.db")
    store.create_run("r1", "x/y", "", "b")
    store.create_env("r1", "p", None, 1, "live")
    store.create_run("r2", "x/y", "", "b")
    store.create_env("r2", "p", None, 1, "live")
    store.mark_asleep("r2")
    stopped = []
    class T:
        def running_ids(self): return {"r1", "rX"}
        def stop(self, rid): stopped.append(rid)
    fired, seen = [], {}
    tunnel_sweep(store, T(), seen, lambda rid, t: fired.append((rid, t)))
    assert "rX" in stopped and "r1" not in stopped
    assert ("r2", "asleep") in fired
    tunnel_sweep(store, T(), seen, lambda rid, t: fired.append((rid, t)))
    assert fired.count(("r2", "asleep")) == 1   # transition fires once


def test_tunnel_sweep_keeps_provisioning_tunnels(tmp_path):
    # A tunnel started during provisioning (env state 'starting', before the app
    # is healthy) must NOT be reaped as an orphan. session._provision starts the
    # cloudflared tunnel before the web container so the public origin can be
    # baked into NEXT_PUBLIC_SUPABASE_URL; reaping it mid-provision kills the URL.
    from forge.webapp import tunnel_sweep
    store = Store(tmp_path / "f.db")
    store.create_run("r1", "x/y", "", "b")
    store.create_env("r1", "p", None, 1, "starting")   # still coming up
    stopped = []
    class T:
        def running_ids(self): return {"r1"}
        def stop(self, rid): stopped.append(rid)
    tunnel_sweep(store, T(), {}, None)
    assert "r1" not in stopped


def test_tunnel_sweep_renotifies_after_wake(tmp_path):
    # A run that wakes and later sleeps again must notify again: waking clears
    # its seen_state entry — otherwise the second sleep is silently swallowed
    # because seen_state still says "asleep" from the first one.
    from forge.webapp import tunnel_sweep
    store = Store(tmp_path / "f.db")
    store.create_run("r1", "x/y", "", "b")
    store.create_env("r1", "p", None, 1, "live")
    class T:
        def running_ids(self): return set()
        def stop(self, rid): pass
    fired, seen = [], {}
    notify = lambda rid, t: fired.append((rid, t))
    store.mark_asleep("r1")
    tunnel_sweep(store, T(), seen, notify)
    store.set_env_state("r1", "live")          # woken
    tunnel_sweep(store, T(), seen, notify)
    store.mark_asleep("r1")                    # slept again
    tunnel_sweep(store, T(), seen, notify)
    assert fired.count(("r1", "asleep")) == 2


def test_seed_seen_state_suppresses_restart_backlog(tmp_path):
    # Regression: on every daemon restart the in-memory seen_state was rebuilt
    # empty, so the first sweep re-announced "slept"/"removed" into every old
    # thread. Seeding the baseline from the DB at startup must suppress that.
    from forge.webapp import tunnel_sweep, seed_seen_state
    store = Store(tmp_path / "f.db")
    store.create_run("r2", "x/y", "", "b")
    store.create_env("r2", "p", None, 1, "live")
    store.mark_asleep("r2")             # went asleep during a PRIOR run
    class T:
        def running_ids(self): return set()
        def stop(self, rid): pass
    fired = []
    seen = seed_seen_state(store)       # fresh process: seed from DB
    tunnel_sweep(store, T(), seen, lambda rid, t: fired.append((rid, t)))
    assert fired == []                  # backlog is NOT re-announced


def test_seed_seen_state_still_fires_new_transition(tmp_path):
    # Seeding must not mute genuine transitions that happen after startup.
    from forge.webapp import tunnel_sweep, seed_seen_state
    store = Store(tmp_path / "f.db")
    store.create_run("r2", "x/y", "", "b")
    store.create_env("r2", "p", None, 1, "live")
    store.mark_asleep("r2")
    class T:
        def running_ids(self): return set()
        def stop(self, rid): pass
    fired = []
    seen = seed_seen_state(store)
    store.mark_deleted("r2")            # asleep -> deleted AFTER startup
    tunnel_sweep(store, T(), seen, lambda rid, t: fired.append((rid, t)))
    assert fired == [("r2", "deleted")]


def test_attach_tunnel_lifecycle_starts_for_live(tmp_path):
    from forge.webapp import attach_tunnel_lifecycle
    from fastapi import FastAPI
    cfg = Config.from_env(tmp_path)
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "x/y", "", "b")
    store.create_env("r1", "forge-r1", "http://localhost:3001", 3001, "live",
                     web_service="web")
    started = []
    class T:
        def start(self, rid, target, host_header=None):
            started.append((rid, target, host_header)); return "u"
        def stop(self, rid): pass
        def running_ids(self): return set()
        def url_for(self, rid): return None
    app = FastAPI()
    attach_tunnel_lifecycle(app, cfg, store, None, T(), lambda rid, t: None)
    with TestClient(app):
        pass
    # Tunnel fronts the shared Caddy (its port), rewriting Host to the per-run
    # site so Caddy's path-split (app + Supabase) applies.
    assert ("r1", f"http://localhost:{cfg.proxy_port}",
            f"run-r1.{cfg.proxy_domain}") in started


def test_review_endpoint_streams_events():
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from forge.webapp import create_app

    class M:
        def can_start(self):
            return (True, "")

        def review(self, run_id, pr, model="auto", origin="api"):
            yield SimpleNamespace(kind="phase", data={"label": "Checking out"})
            yield SimpleNamespace(kind="review",
                                  data={"ok": True, "review_url": "u",
                                        "comments": 1, "dropped": 0,
                                        "degraded": False})

    app = create_app(SimpleNamespace(workspace_dir="."), object(), M())
    client = TestClient(app)
    r = client.post("/api/review", json={"pr": "o/r#3"})
    assert r.status_code == 200
    assert "event: review" in r.text
    assert '"review_url": "u"' in r.text


# --- Task 7: start-task + checkpoint-response endpoints ---
class FakeGenManager(FakeManager):
    def plan_task(self, run_id, task, model="auto", attachments=None, origin="api"):
        from types import SimpleNamespace
        # mimic a gated planner: emit a plan then a checkpoint
        self.store.create_checkpoint(run_id, "plan_approval", {"plan": {"goal": task}})
        yield SimpleNamespace(kind="plan", data={"goal": task})
        yield SimpleNamespace(kind="checkpoint", data={"id": 1, "type": "plan_approval"})

    def respond_checkpoint(self, run_id, cid, action, body=None, model="auto", origin="api"):
        from types import SimpleNamespace
        yield SimpleNamespace(kind="done", data={"message": f"{action}", "verify_ok": True})


def _gen_client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    return TestClient(create_app(cfg, store, FakeGenManager(store))), store


def test_post_task_streams_plan_and_checkpoint(tmp_path):
    client, store = _gen_client(tmp_path)
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    r = client.post("/api/sessions/r1/task", json={"task": "Add logout"})
    assert r.status_code == 200
    assert "event: plan" in r.text and "event: checkpoint" in r.text


def test_post_checkpoint_streams_done(tmp_path):
    client, store = _gen_client(tmp_path)
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    r = client.post("/api/sessions/r1/checkpoints/1", json={"action": "approve"})
    assert r.status_code == 200 and "event: done" in r.text


def test_session_detail_includes_open_checkpoint(tmp_path):
    client, store = _gen_client(tmp_path)
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_checkpoint("r1", "plan_approval", {"plan": {"goal": "Add logout"}})
    body = client.get("/api/sessions/r1").json()
    assert body["checkpoint"]["ctype"] == "plan_approval"


def test_session_detail_includes_plan_field(tmp_path):
    client, store = _gen_client(tmp_path)
    # Test case 1: run with no plan_json -> plan is None
    store.create_run("r1", "o/r", "No plan", "forge/x")
    body = client.get("/api/sessions/r1").json()
    assert body["plan"] is None

    # Test case 2: run with plan_json set -> plan is parsed dict
    store.create_run("r2", "o/r", "Add logout", "forge/x")
    store.set_plan("r2", '{"goal":"Add logout"}')
    body = client.get("/api/sessions/r2").json()
    assert body["plan"]["goal"] == "Add logout"


# ---------------------------------------------------------------------------
# Live agent-browser view: GET /api/sessions/{id}/browser[/frame]
# ---------------------------------------------------------------------------

def test_browser_endpoints_404_for_unknown_run(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/sessions/nope/browser").status_code == 404
    assert client.get("/api/sessions/nope/browser/frame").status_code == 404


def test_browser_status_inactive_before_any_stream(tmp_path):
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    body = client.get("/api/sessions/r1/browser").json()
    assert body == {"active": False, "ts": 0, "url": "", "title": ""}
    assert client.get("/api/sessions/r1/browser/frame").status_code == 404


def test_browser_status_and_frame_served_from_workspace(tmp_path):
    import json as _j
    from forge import browserview
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    d = browserview.live_dir(tmp_path / "runs", "r1")
    d.mkdir(parents=True)
    (d / "frame.jpg").write_bytes(b"\xff\xd8fakejpeg")
    (d / "meta.json").write_text(_j.dumps({"url": "http://web:3000/x", "title": "X"}))

    body = client.get("/api/sessions/r1/browser").json()
    assert body["active"] is True and body["url"] == "http://web:3000/x"

    res = client.get("/api/sessions/r1/browser/frame")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.headers["cache-control"] == "no-store"   # always the newest frame
    assert res.content == b"\xff\xd8fakejpeg"


def test_browser_stream_404_for_unknown_run(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/sessions/nope/browser/stream").status_code == 404


def test_browser_stream_serves_mjpeg_parts(tmp_path, monkeypatch):
    # The route wires browserview.stream_frames into an MJPEG response; the
    # generator's own timing/termination is unit-tested in test_browserview.
    from forge import browserview
    client, store, _ = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")

    async def canned(runs_dir, run_id, **kw):
        yield b"--forgeframe\r\nContent-Type: image/jpeg\r\n\r\njpegbytes\r\n"

    monkeypatch.setattr(browserview, "stream_frames", canned)
    res = client.get("/api/sessions/r1/browser/stream")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert res.headers["cache-control"] == "no-store"
    assert b"jpegbytes" in res.content
