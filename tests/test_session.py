# tests/test_session.py
import time
from pathlib import Path
from forge.session import SessionManager, TurnEvent
from forge.config import Config, Budget
from forge.store import Store
from forge.verify import VerifyPlan, VerifyCmd


def _drain_capture(gen, events=None):
    try:
        while True:
            ev = next(gen)
            if events is not None:
                events.append(ev)
    except StopIteration as e:
        return e.value


class FakeEnv:
    def __init__(self, run_id, files):
        self.run_id = run_id
        self.files = files
        # provisioning + turn behavior is scripted by the test via attributes

    up_calls = 0

    def up(self, secrets):
        type(self).up_calls += 1

    def exec(self, argv, service=None, workdir="/work", env=None):
        from forge.container import ExecResult
        joined = " ".join(argv)
        if "status" in joined and "--porcelain" in joined:
            return ExecResult(0, " M src/x.ts\n", "")
        if "rev-parse" in joined or "diff" in joined:
            return ExecResult(0, "diff --git a/x b/x\n", "")
        if "pr" in joined and "create" in joined:
            return ExecResult(0, "https://github.com/o/r/pull/1\n", "")
        if "push" in joined:
            return ExecResult(0, "", "")
        return ExecResult(0, "", "")

    def exec_stream(self, argv, service=None, workdir="/work"):
        import json
        yield json.dumps({"type": "assistant",
                          "message": {"content": [{"type": "text", "text": "editing"}]}})
        yield json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "session_id": "sess-1", "result": "fixed",
                          "total_cost_usd": 0.1, "num_turns": 1, "usage": {}})

    def port(self, service, port):
        return 5599

    def cancel(self):
        pass

    last_stopped = None
    last_started = None
    last_downed = None

    def stop(self):
        type(self).last_stopped = self.run_id

    def start(self):
        type(self).last_started = self.run_id

    def down(self):
        type(self).last_downed = self.run_id


class FakeHost:
    def clone(self, repo, branch, ws, token):
        from forge.container import ExecResult
        Path(ws).mkdir(parents=True, exist_ok=True)
        (Path(ws) / "package.json").write_text('{"scripts":{"dev":"vite","test":"jest"}}')
        return ExecResult(0, "", "")

    def read(self, ws, rel):
        return (Path(ws) / rel).read_text() if (Path(ws)/rel).is_file() else None

    def exists(self, ws, rel):
        return (Path(ws) / rel).exists()

    def write_file(self, path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)

    def run(self, argv, env=None):
        from forge.container import ExecResult
        return ExecResult(0, "", "")


def _mgr(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    # health_poll succeeds because FakeEnv.exec returns 0 for the health argv
    return SessionManager(cfg, store, FakeHost(),
                          env_factory=lambda rid, files: FakeEnv(rid, files)), store


def test_start_provisions_and_registers_url(tmp_path):
    mgr, store = _mgr(tmp_path)
    events = list(mgr.start("r1", "o/r", "github"))
    kinds = [e.kind for e in events]
    assert "phase" in kinds
    assert events[-1].kind == "url"
    assert store.get_env("r1")["state"] == "live"
    assert store.get_run("r1")["repo_source"] == "github:o/r"


NEXT_SUPABASE_CONFIG = """project_id = "demo"

[api]
port = 54321

[db]
port = 54322
shadow_port = 54320

[studio]
port = 54323
"""


class NextSupabaseHost(FakeHost):
    def clone(self, repo, branch, ws, token):
        from forge.container import ExecResult
        Path(ws).mkdir(parents=True, exist_ok=True)
        (Path(ws) / "package.json").write_text(
            '{"dependencies":{"next":"15.0.0"},"scripts":{"dev":"next dev"}}')
        (Path(ws) / "supabase").mkdir(parents=True, exist_ok=True)
        (Path(ws) / "supabase" / "config.toml").write_text(NEXT_SUPABASE_CONFIG)
        return ExecResult(0, "", "")


class _SupabaseStartFailsHost(NextSupabaseHost):
    def run(self, argv, env=None):
        from forge.container import ExecResult
        if list(argv[:2]) == ["supabase", "start"]:
            return ExecResult(1, "", "supabase start: port 54321 already in use")
        return ExecResult(0, "", "")


def test_host_pre_failure_aborts_with_clear_error(tmp_path):
    # A failed host_pre (supabase start) must abort with a named, actionable
    # error instead of pressing on into a cryptic health-check timeout.
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, _SupabaseStartFailsHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    events = list(mgr.start("r1", "o/r", "github"))
    err = events[-1]
    assert err.kind == "error" and err.data["kind"] == "host_pre"
    assert "supabase start" in err.data["detail"]
    assert "port 54321" in err.data["detail"]
    assert store.get_run("r1")["state"] == "failed"


class FakeTunnel:
    def __init__(self, url="https://demo.trycloudflare.com"):
        self.url, self.calls = url, []
        self._urls = {}

    def start(self, run_id, target, host_header=None):
        self.calls.append((run_id, target, host_header))
        self._urls[run_id] = self.url
        return self.url

    def url_for(self, run_id):
        return self._urls.get(run_id)


def test_next_supabase_bakes_same_origin_url_and_refreshes_proxy(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    tunnel = FakeTunnel()
    refreshed = []
    mgr = SessionManager(cfg, store, NextSupabaseHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files),
                         tunnel=tunnel, proxy_refresh=lambda: refreshed.append(True))
    events = list(mgr.start("r1", "o/r", "github"))

    # tunnel pointed at Caddy with the per-run host header
    assert tunnel.calls == [("r1", f"http://localhost:{cfg.proxy_port}",
                             f"run-r1.{cfg.proxy_domain}")]
    # Supabase is baked as the DNS-free LOCAL proxy URL, not the tunnel: the
    # server-side Supabase client runs inside the web container, which usually
    # can't resolve the *.trycloudflare.com tunnel host (DNS rebind / Docker
    # resolver) -> "fetch failed ENOTFOUND" and login fails. The local URL is
    # same-origin for the host browser and, via extra_hosts below, reachable from
    # the container -> Caddy -> host Supabase.
    import json
    compose = json.loads((tmp_path / "runs" / "r1" / "forge-compose.yml").read_text())
    web = compose["services"]["web"]
    assert web["environment"]["NEXT_PUBLIC_SUPABASE_URL"] \
        == f"http://run-r1.{cfg.proxy_domain}:{cfg.proxy_port}"
    # the container must resolve the run host to the host gateway to reach Caddy
    assert f"run-r1.{cfg.proxy_domain}:host-gateway" in web["extra_hosts"]
    # Caddy refreshed, web_url is still the public origin
    assert refreshed == [True]
    assert store.get_env("r1")["web_url"] == "https://demo.trycloudflare.com"
    assert events[-1].kind == "url"


def test_url_event_includes_local_proxy_url_when_tunnelled(tmp_path):
    # The public tunnel URL can be unresolvable on the forge host's own network
    # (e.g. a router that NXDOMAINs *.trycloudflare.com). The url event therefore
    # also carries a `*.forge.localhost` proxy URL that resolves to 127.0.0.1 in
    # any browser with no external DNS, so the operator always has a working link.
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, NextSupabaseHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files),
                         tunnel=FakeTunnel(), proxy_refresh=lambda: None)
    events = list(mgr.start("r1", "o/r", "github"))
    url_ev = events[-1]
    assert url_ev.kind == "url"
    assert url_ev.data["web_url"] == "https://demo.trycloudflare.com"
    assert url_ev.data["local_url"] == \
        f"http://run-r1.{cfg.proxy_domain}:{cfg.proxy_port}"


def test_provision_injects_allowed_dev_origins_for_next_app(tmp_path):
    # Next dev blocks cross-origin HMR/dev assets; served via the proxy+tunnel
    # the browser never live-reloads. Provisioning must give the app an
    # allowedDevOrigins config (created here, since this app ships none).
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, NextSupabaseHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files),
                         tunnel=FakeTunnel(), proxy_refresh=lambda: None)
    list(mgr.start("r1", "o/r", "github"))
    cfg_file = tmp_path / "runs" / "r1" / "workspace" / "next.config.js"
    assert cfg_file.exists()
    text = cfg_file.read_text()
    assert "allowedDevOrigins" in text
    assert "*.forge.localhost" in text and "*.trycloudflare.com" in text


def test_url_event_omits_local_url_without_tunnel(tmp_path):
    # No tunnel → no shared Caddy fronting this run → no *.forge.localhost link.
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, NextSupabaseHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    events = list(mgr.start("r1", "o/r", "github"))
    url_evs = [e for e in events if e.kind == "url"]
    assert url_evs and url_evs[-1].data.get("local_url") is None


def test_open_pr_refuses_while_turn_active(tmp_path):
    # open_pr runs a worker (self-review + commit). The Slack "Open PR" button
    # invokes it OUTSIDE the per-thread turn queue, so without an _active guard it
    # could run concurrently with an in-flight turn and corrupt the diff/commit.
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr._active.add("r1")                      # a turn is in flight
    try:
        assert mgr.open_pr("r1") == {"ok": False, "reason": "busy"}
    finally:
        mgr._active.discard("r1")


def test_turn_preserves_public_tunnel_url(tmp_path):
    # Regression: _refresh_url re-read the host port every turn and overwrote the
    # stored web_url with http://localhost:<port>, losing the public tunnel
    # origin. A teammate viewing the session (web app or Slack status) must keep
    # the working public link, not a host-only localhost URL.
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    tunnel = FakeTunnel()
    mgr = SessionManager(cfg, store, NextSupabaseHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files),
                         tunnel=tunnel)
    list(mgr.start("r1", "o/r", "github"))
    assert store.get_env("r1")["web_url"] == "https://demo.trycloudflare.com"
    events = list(mgr.turn("r1", "make a change"))
    url_evs = [e for e in events if e.kind == "url"]
    assert url_evs and url_evs[-1].data["web_url"] == "https://demo.trycloudflare.com"
    assert store.get_env("r1")["web_url"] == "https://demo.trycloudflare.com"


def test_no_tunnel_keeps_localhost_url_and_default_env(tmp_path):
    # Backward-compat: without a tunnel, behavior is unchanged.
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, NextSupabaseHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    import json
    compose = json.loads((tmp_path / "runs" / "r1" / "forge-compose.yml").read_text())
    # default fallback retained (container-only host)
    assert "host.docker.internal" in \
        compose["services"]["web"]["environment"]["NEXT_PUBLIC_SUPABASE_URL"]
    assert store.get_env("r1")["web_url"].startswith("http://localhost:")


class _NoAppHost(FakeHost):
    # No package.json / supabase → resolver returns none_recipe (confidence low).
    def clone(self, repo, branch, ws, token):
        from forge.container import ExecResult
        Path(ws).mkdir(parents=True, exist_ok=True)
        (Path(ws) / "README.md").write_text("hi")
        return ExecResult(0, "", "")


def test_low_confidence_triggers_probe_and_persists(tmp_path, monkeypatch):
    from forge import envprobe
    from forge.knowledge import KnowledgeStore
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    calls = {"probe": 0}

    def fake_probe(env, model=None, max_iterations=6, provider=None):
        calls["probe"] += 1
        return {"pkg_manager": "bun"}
    monkeypatch.setattr(envprobe, "probe", fake_probe)
    mgr = SessionManager(cfg, store, _NoAppHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    assert calls["probe"] == 1
    assert KnowledgeStore(cfg.knowledge_dir).load("o/r")["pkg_manager"] == "bun"


def test_self_heal_off_skips_probe(tmp_path, monkeypatch):
    from forge import envprobe
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb", self_heal=False,
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    monkeypatch.setattr(envprobe, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    mgr = SessionManager(cfg, store, _NoAppHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))   # must not call probe


class _HealthEnv(FakeEnv):
    """Health passes only when health_ok is set (set per-instance by the factory)."""
    health_ok = False

    def exec(self, argv, service=None, workdir="/work", env=None):
        from forge.container import ExecResult
        if "curl -fs" in " ".join(argv):                 # the health poll
            return ExecResult(0 if self.health_ok else 1, "", "")
        return super().exec(argv, service=service, workdir=workdir, env=env)


def test_health_failure_triggers_repair_and_retry(tmp_path, monkeypatch):
    from forge import envprobe
    from forge.knowledge import KnowledgeStore
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    made = []

    def factory(rid, files):
        e = _HealthEnv(rid, files)
        e.health_ok = len(made) >= 1     # first env fails health, retry env passes
        made.append(e)
        return e
    repaired = {"n": 0}

    def fake_repair(env, phase, logs, model=None, max_iterations=6, provider=None):
        repaired["n"] += 1
        return {"apt": ["libnss3"]}
    monkeypatch.setattr(envprobe, "repair", fake_repair)
    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)  # FakeHost → node-web (high)
    events = list(mgr.start("r1", "o/r", "github"))
    assert repaired["n"] == 1
    assert events[-1].kind == "url"                       # retry succeeded
    assert KnowledgeStore(cfg.knowledge_dir).load("o/r")["apt"] == ["libnss3"]


def test_repair_retry_up_failure_yields_error_not_crash(tmp_path, monkeypatch):
    # Regression: the repair-path retry `env.up()` was unguarded. If the repaired
    # compose also failed to start, the RuntimeError escaped the generator,
    # stranding the session in "running" with no error event and Supabase leaked.
    from forge import envprobe
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    made = []

    class _RepairBlowsUp(_HealthEnv):
        def up(self, secrets):
            # First env (probe/initial up) is fine; the retry env raises.
            if getattr(self, "_explode", False):
                raise RuntimeError("compose up failed again")

    def factory(rid, files):
        e = _RepairBlowsUp(rid, files)
        e.health_ok = False          # never healthy → repair path
        e._explode = len(made) >= 1  # the retry env (2nd) blows up on up()
        made.append(e)
        return e

    monkeypatch.setattr(envprobe, "repair",
                        lambda *a, **k: {"apt": ["libnss3"]})
    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    events = list(mgr.start("r1", "o/r", "github"))        # must NOT raise
    assert events[-1].kind == "error" and events[-1].data["kind"] == "up"
    assert store.get_run("r1")["state"] == "failed"


def test_provision_reuses_existing_workspace_without_clone(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    ws = str(tmp_path / "runs" / "r1" / "workspace")
    # _provision against the existing workspace must re-register a url without
    # touching the host clone path.
    events = list(mgr._provision("r1", ws))
    assert events[-1].kind == "url"
    assert store.get_env("r1")["state"] == "live"


def test_compose_bind_mount_source_is_absolute(tmp_path, monkeypatch):
    # Reproduces the real failure: with the CLI's relative --runs-dir ("runs"), the
    # forge-compose.yml volume source comes out relative and Docker Compose rejects
    # it as an undefined named volume, so `compose up` fails before the app ever runs.
    import json
    monkeypatch.chdir(tmp_path)
    cfg = Config(runs_dir=Path("runs"), oauth_token="t", gh_token="g")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, FakeHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    compose = json.loads((cfg.runs_dir / "r1" / "forge-compose.yml").read_text())
    source = compose["services"]["web"]["volumes"][0].split(":", 1)[0]
    assert Path(source).is_absolute(), \
        f"compose bind-mount source must be absolute, got {source!r}"


def test_start_normalizes_github_url(tmp_path):
    mgr, store = _mgr(tmp_path)
    events = list(mgr.start("r1", "https://github.com/o/r.git", "github"))
    assert events[-1].kind == "url"
    # the pasted URL is reduced to owner/name everywhere it is stored
    assert store.get_run("r1")["repo"] == "o/r"
    assert store.get_run("r1")["repo_source"] == "github:o/r"


def test_start_yields_clean_error_for_bad_repo(tmp_path):
    mgr, store = _mgr(tmp_path)
    events = list(mgr.start("r1", "https://example.com/not-a-repo", "github"))
    # no crash: a single clean error bubble, and the session is queryable as failed
    assert events[-1].kind == "error"
    assert events[-1].data["kind"] == "repo"
    assert store.get_run("r1")["state"] == "failed"


def test_turn_streams_verifies_and_persists(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    events = list(mgr.turn("r1", "make the header bold"))
    kinds = [e.kind for e in events]
    assert "narration" in kinds
    assert "verify" in kinds
    assert events[-1].kind == "done"
    msgs = store.list_messages("r1")
    assert msgs[0]["role"] == "user" and "header" in msgs[0]["content"]
    assert msgs[-1]["role"] == "assistant"
    assert store.get_run("r1")["claude_session_id"] == "sess-1"


class CapturingEnv(FakeEnv):
    """Records the argv handed to exec_stream so a test can assert the worker
    was launched with the resolved --model."""
    last_stream_argv = None

    def exec_stream(self, argv, service=None, workdir="/work"):
        type(self).last_stream_argv = list(argv)
        return super().exec_stream(argv, service=service, workdir=workdir)


def test_turn_resolves_model_and_emits_model_event(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.env_factory = lambda rid, files: CapturingEnv(rid, files)
    list(mgr.start("r1", "o/r", "github"))
    # "implement" is a heavy keyword → auto resolves to opus.
    events = list(mgr.turn("r1", "Implement an error boundary", model="auto"))
    model_ev = next(e for e in events if e.kind == "model")
    assert model_ev.data["choice"] == "auto"
    assert model_ev.data["resolved"] == "opus"
    # The resolved alias is actually passed to the worker CLI.
    argv = CapturingEnv.last_stream_argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "opus"


def test_turn_explicit_model_overrides_auto(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.env_factory = lambda rid, files: CapturingEnv(rid, files)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "Implement a huge refactor", model="haiku"))
    argv = CapturingEnv.last_stream_argv
    assert argv[argv.index("--model") + 1] == "haiku"


class _NoChecksHost(FakeHost):
    def clone(self, repo, branch, ws, token):
        from forge.container import ExecResult
        Path(ws).mkdir(parents=True, exist_ok=True)
        # No test/lint/typecheck/build script → no real verification.
        (Path(ws) / "package.json").write_text('{"scripts":{"dev":"vite"}}')
        return ExecResult(0, "", "")


def test_turn_verify_ok_is_none_when_no_checks_configured(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, _NoChecksHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    events = list(mgr.turn("r1", "make the header bold"))
    kinds = [e.kind for e in events]
    # No checks → no verify event, and verify_ok is tri-state None (not a
    # misleading "passing"/True).
    assert "verify" not in kinds
    meta = store.list_messages("r1")[-1]["meta"]
    assert meta["verify_ok"] is None
    done = next(e for e in events if e.kind == "done")
    assert done.data["verify_ok"] is None


def test_turn_persists_verify_failure_output(tmp_path):
    mgr, store = _mgr(tmp_path)

    class FailingVerifyEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if argv[:2] == ["npm", "test"]:
                return ExecResult(1, "FAIL src/x.test.ts\n", "1 test failed\n")
            return super().exec(argv, service=service, workdir=workdir, env=env)

    mgr.env_factory = lambda rid, files: FailingVerifyEnv(rid, files)
    list(mgr.start("r1", "o/r", "github"))
    events = list(mgr.turn("r1", "change something"))
    verify_ev = next(e for e in events if e.kind == "verify")
    assert verify_ev.data["ok"] is False
    assert "test" in verify_ev.data["failed"]
    assert "FAIL" in verify_ev.data["output"]
    # The failure output is persisted so the inspector can show it after reload.
    meta = store.list_messages("r1")[-1]["meta"]
    assert meta["verify_ok"] is False
    assert "test" in meta["verify_failed"]
    assert "FAIL" in meta["verify_output"]


def test_turn_rejects_concurrent_turn(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr._active.add("r1")
    out = list(mgr.turn("r1", "x"))
    assert out[0].kind == "error" and out[0].data["kind"] == "busy"


# --- sleep/wake/delete tests ---

def test_sleep_warm_stops_and_marks_asleep(tmp_path):
    """sleep() warm-stops (env.stop, not down) and KEEPS the Supabase reservation."""
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    # Reserve a Supabase block; sleep must NOT release it.
    store.reserve_supabase("r1", 0, "proj-r1")
    released = []
    mgr._release_supabase = lambda rid: released.append(rid)
    stopped = []

    class StopEnv(FakeEnv):
        def stop(self):
            stopped.append(self.run_id)

    mgr.env_factory = lambda rid, files: StopEnv(rid, files)
    host_calls = []
    mgr.host.run = lambda argv, env=None: host_calls.append(argv) or __import__(
        "forge.container", fromlist=["ExecResult"]).ExecResult(0, "", "")
    assert mgr.sleep("r1") is True
    assert released == []                              # reservation KEPT
    assert stopped == ["r1"]                           # stop() called (not down())
    assert store.get_supabase("r1")                    # row still present
    assert any(a[:2] == ["supabase", "stop"] for a in host_calls)  # supabase paused
    assert store.get_env("r1")["state"] == "asleep"
    assert store.get_run("r1")["state"] == "asleep"


def test_sleep_warm_stops_keeps_supabase(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    store.create_env("r1", "forge-r1", "u", 3000, "live", web_service="web")
    store.reserve_supabase("r1", 100, "demo-r1")
    Path(tmp_path / "runs" / "r1" / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / "r1" / "workspace" / "package-lock.json").write_text("{}")

    host_calls = []
    mgr.host.run = lambda argv, env=None: host_calls.append(argv) or __import__(
        "forge.container", fromlist=["ExecResult"]).ExecResult(0, "", "")

    FakeEnv.last_stopped = None
    FakeEnv.last_started = None
    assert mgr.sleep("r1") is True
    env = mgr._env_for("r1")
    assert getattr(env, "stopped", False) or FakeEnv.last_stopped == "r1"  # stop() called
    assert store.get_env("r1")["state"] == "asleep"
    assert store.get_env("r1")["snapshot_lockhash"]                        # signature recorded
    assert store.get_supabase("r1")                                        # reservation KEPT
    assert any(a[:2] == ["supabase", "stop"] for a in host_calls)          # supabase paused
    assert store.get_env("r1")["state"] == "asleep"


def test_sleep_refused_while_turn_in_flight(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr._active.add("r1")
    assert mgr.sleep("r1") is False
    assert store.get_env("r1")["state"] == "live"


def test_wake_reprovisions_asleep_session(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.env_factory = lambda rid, files: FakeEnv(rid, files)
    mgr.sleep("r1")
    assert store.get_run("r1")["state"] == "asleep"
    events = list(mgr.wake("r1"))
    assert events[-1].kind == "url"
    assert store.get_env("r1")["state"] == "live"
    assert store.get_run("r1")["state"] == "running"


def test_wake_errors_when_workspace_deleted(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    import shutil
    shutil.rmtree(tmp_path / "runs" / "r1" / "workspace")
    events = list(mgr.wake("r1"))
    assert events[-1].kind == "error"
    assert events[-1].data["kind"] == "gone"
    assert store.get_run("r1")["state"] == "deleted"


def test_delete_dormant_pushes_then_removes_workspace(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.sleep("r1")
    ws = tmp_path / "runs" / "r1" / "workspace"
    # Make the workspace look like a git repo so _archive_code proceeds.
    (ws / ".git").mkdir(parents=True, exist_ok=True)
    pushed = []

    class PushHost(FakeHost):
        def run(self, argv, env=None):
            from forge.container import ExecResult
            if argv[:1] == ["git"] and "push" in argv:
                pushed.append(argv)
            return ExecResult(0, "", "")

    mgr.host = PushHost()
    assert mgr.delete_dormant("r1") is True
    assert pushed, "expected a git push before deletion"
    assert not ws.exists()
    assert store.get_run("r1")["state"] == "deleted"


def test_delete_dormant_keeps_workspace_when_push_fails(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.sleep("r1")
    ws = tmp_path / "runs" / "r1" / "workspace"
    (ws / ".git").mkdir(parents=True, exist_ok=True)

    class FailPushHost(FakeHost):
        def run(self, argv, env=None):
            from forge.container import ExecResult
            if argv[:1] == ["git"] and "push" in argv:
                return ExecResult(1, "", "auth failed")
            return ExecResult(0, "", "")

    mgr.host = FailPushHost()
    assert mgr.delete_dormant("r1") is False
    assert ws.exists()                       # not deleted
    assert store.get_run("r1")["state"] == "asleep"   # still dormant


def test_delete_dormant_tears_down_warm_stack(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = Path(tmp_path / "runs" / "r1" / "workspace"); ws.mkdir(parents=True)
    store.create_env("r1", "forge-r1", "u", 3000, "asleep", web_service="web")
    mgr._archive_code = lambda rid: True            # skip real git archive
    FakeEnv.last_downed = None
    def down(self): type(self).last_downed = self.run_id
    FakeEnv.down = down
    assert mgr.delete_dormant("r1") is True
    assert FakeEnv.last_downed == "r1"              # warm stack torn down (down -v)
    assert store.get_env("r1")["state"] == "deleted"
    assert not ws.exists()


def test_end_marks_deleted(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.end("r1")
    assert store.get_run("r1")["state"] == "deleted"


def test_reconcile_sleeps_orphaned_running_session(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    assert store.get_env("r1")["state"] == "live"
    # No containers (e.g. after a forge restart) → session should be wakeable,
    # not dead.
    mgr.reconcile(ps_checker=lambda project: False)
    assert store.get_env("r1")["state"] == "asleep"
    assert store.get_run("r1")["state"] == "asleep"


# --- Task 10 tests ---

def test_open_pr_only_on_demand_and_commits(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    # FakeEnv.exec reports porcelain has changes + pr url on gh pr create
    res = mgr.open_pr("r1")
    assert res["ok"] is True


def test_self_review_runs_worker_and_records_message(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    # write a review.json into the run workspace so the count is reported
    ws = tmp_path / "runs" / "r1" / "workspace" / ".forge"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "review.json").write_text(
        '{"summary":"fixed stuff","comments":[{"path":"x","line":1,"body":"a"},'
        '{"path":"y","line":2,"body":"b"}]}')
    n = mgr._self_review_and_fix("r1")
    assert n == 2
    sys_msgs = [m for m in store.list_messages("r1") if m["role"] == "system"]
    assert any("Self-review" in m["content"] for m in sys_msgs)


def test_self_review_disabled_is_noop(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.self_review = False
    list(mgr.start("r1", "o/r", "github"))
    assert mgr._self_review_and_fix("r1") == 0
    assert not [m for m in store.list_messages("r1")
                if m["role"] == "system" and "Self-review" in m["content"]]


def test_open_pr_invokes_self_review(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    called = []
    monkeypatch.setattr(mgr, "_self_review_and_fix",
                        lambda rid: called.append(rid) or 0)
    res = mgr.open_pr("r1")
    assert res["ok"] is True
    assert called == ["r1"]


def test_commit_identity_user_mode(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "user"
    mgr.cfg.git_author_name = "Dev"
    mgr.cfg.git_author_email = "dev@example.com"
    assert mgr._commit_identity("r1") == ("Dev", "dev@example.com")


def test_commit_identity_auto_falls_back_to_user_without_app(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "auto"          # no gh_app_id configured
    mgr.cfg.git_author_name = "Dev"
    mgr.cfg.git_author_email = "dev@example.com"
    assert mgr._commit_identity("r1") == ("Dev", "dev@example.com")


def test_commit_identity_forge_mode_uses_bot(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "forge"

    class FakeApp:
        def bot_identity(self):
            return ("forge[bot]", "42+forge[bot]@users.noreply.github.com")

    monkeypatch.setattr(mgr, "_ghapp", lambda: FakeApp())
    assert mgr._commit_identity("r1") == (
        "forge[bot]", "42+forge[bot]@users.noreply.github.com")


def test_commit_identity_forge_mode_errors_without_app(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "forge"          # no app
    import pytest
    with pytest.raises(RuntimeError):
        mgr._commit_identity("r1")


class FakeGhApp:
    def bot_identity(self):
        return ("forge[bot]", "42+forge[bot]@users.noreply.github.com")


def test_commit_identity_auto_keeps_user_as_author_with_app(tmp_path, monkeypatch):
    # auto = the user authors; the bot gets credit via the Co-Authored-By
    # trailer, not by taking over authorship.
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "auto"
    mgr.cfg.git_author_name = "Dev"
    mgr.cfg.git_author_email = "dev@example.com"
    monkeypatch.setattr(mgr, "_ghapp", lambda: FakeGhApp())
    assert mgr._commit_identity("r1") == ("Dev", "dev@example.com")
    assert mgr._commit_trailer("r1") == (
        "Co-Authored-By: forge[bot] <42+forge[bot]@users.noreply.github.com>")


def test_commit_trailer_empty_without_app_or_in_pinned_modes(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "auto"           # no app configured
    assert mgr._commit_trailer("r1") == ""
    monkeypatch.setattr(mgr, "_ghapp", lambda: FakeGhApp())
    for mode in ("user", "forge"):             # single-author modes: no trailer
        mgr.cfg.commit_identity = mode
        assert mgr._commit_trailer("r1") == ""


def test_commit_trailer_never_blocks_commit_on_identity_failure(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.commit_identity = "auto"

    class BrokenApp:
        def bot_identity(self):
            raise RuntimeError("api unreachable")

    monkeypatch.setattr(mgr, "_ghapp", lambda: BrokenApp())
    assert mgr._commit_trailer("r1") == ""


def test_open_pr_commit_message_carries_coauthor_trailer(tmp_path, monkeypatch):
    recorded = []

    class RecEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            recorded.append(argv)
            return FakeEnv.exec(self, argv, service, workdir)

    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, FakeHost(),
                         env_factory=lambda rid, files: RecEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    monkeypatch.setattr(mgr, "_ghapp", lambda: FakeGhApp())
    monkeypatch.setattr(mgr, "_self_review_and_fix", lambda rid: 0)
    res = mgr.open_pr("r1")
    assert res["ok"] is True
    commit = next(a for a in recorded if a[:2] == ["git", "commit"])
    assert commit[3].endswith(
        "\n\nCo-Authored-By: forge[bot] <42+forge[bot]@users.noreply.github.com>")


def test_can_start_enforces_cap(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.max_live_sessions = 1
    store.create_env("a", "forge-a", None, 3000, "live")
    ok, msg = mgr.can_start()
    assert ok is False and "max" in msg.lower()


def test_can_start_allows_below_cap(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.max_live_sessions = 4
    ok, msg = mgr.can_start()
    assert ok is True and msg == ""


def test_diff_returns_patch(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    patch = mgr.diff("r1")
    assert "diff" in patch


def test_end_reaps_project(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    # end should call reap_project which calls mark_reaped
    reaped = []
    import forge.lifecycle as lc
    orig = lc.reap_project
    lc.reap_project = lambda store, run_id, **kw: reaped.append(run_id)
    try:
        mgr.end("r1")
    finally:
        lc.reap_project = orig
    assert "r1" in reaped


def test_reconcile_leaves_live_when_containers_present(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.reconcile(ps_checker=lambda project: True)
    assert store.get_env("r1")["state"] == "live"


def test_stop_cancels_and_removes_active(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr._active.add("r1")
    mgr.stop("r1")
    assert "r1" not in mgr._active


def test_open_pr_no_changes(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))

    # Override exec to simulate no changes
    import forge.container as ctr
    orig_factory = mgr.env_factory

    class NoChangesEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            joined = " ".join(argv)
            if "status" in joined and "--porcelain" in joined:
                return ctr.ExecResult(0, "", "")
            return super().exec(argv, service=service, workdir=workdir)

    mgr.env_factory = lambda rid, files: NoChangesEnv(rid, files)
    res = mgr.open_pr("r1")
    assert res["ok"] is False and res["reason"] == "no_changes"


def test_turn_counts_untracked_files_via_intent_to_add(tmp_path):
    """The per-turn diff_files count must include intent-to-add (git add -A -N)
    so brand-new untracked files are counted, matching what diff() renders.
    Regression: _diff_file_count used a plain `git diff --name-only HEAD`,
    which omits untracked files and undercounts the change."""
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))

    class NewFileEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            joined = " ".join(argv)
            if "diff" in joined and "--name-only" in joined:
                # A brand-new untracked file is invisible to `git diff` unless
                # `git add -A -N` (intent-to-add) ran first.
                if "add -A -N" in joined:
                    return ExecResult(0, "new_file.ts\n", "")
                return ExecResult(0, "", "")
            return super().exec(argv, service=service, workdir=workdir)

    mgr.env_factory = lambda rid, files: NewFileEnv(rid, files)
    events = list(mgr.turn("r1", "create a brand new file"))
    done = events[-1]
    assert done.kind == "done"
    assert done.data["diff_files"] == 1


def _artifacts_dir(cfg, run_id):
    d = Path(cfg.runs_dir) / run_id / "workspace" / ".forge" / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_artifacts_reads_manifest_from_run_workspace(tmp_path):
    mgr, _ = _mgr(tmp_path)
    d = _artifacts_dir(mgr.cfg, "r1")
    (d / "after.png").write_bytes(b"x")
    (d / "manifest.json").write_text(
        '{"artifacts": [{"path": "after.png", "kind": "after", "caption": "Done"}]}')
    arts = mgr.artifacts("r1")
    assert [(a.path.name, a.kind, a.caption) for a in arts] == [
        ("after.png", "after", "Done")]
    assert arts[0].path.is_file()      # absolute host path resolved


def test_artifacts_empty_when_dir_absent(tmp_path):
    mgr, _ = _mgr(tmp_path)
    assert mgr.artifacts("no-such-run") == []


def test_reset_artifacts_clears_stale_capture(tmp_path):
    # A prior turn's screenshots + manifest must not be re-uploaded on a later
    # turn that captured nothing.
    mgr, _ = _mgr(tmp_path)
    d = _artifacts_dir(mgr.cfg, "r1")
    (d / "after.png").write_bytes(b"x")
    (d / "manifest.json").write_text('{"artifacts": [{"path": "after.png"}]}')
    assert mgr.artifacts("r1")            # present before
    mgr._reset_artifacts("r1")
    assert mgr.artifacts("r1") == []      # gone after


class ReviewHost(FakeHost):
    def clone_pr(self, repo, dest, number, gh_token):
        from forge.container import ExecResult
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "package.json").write_text('{"scripts":{"dev":"vite"}}')
        return ExecResult(0, "", "")

    def run(self, argv, env=None):
        from forge.container import ExecResult
        joined = " ".join(argv)
        if "pr" in joined and "diff" in joined:
            return ExecResult(0, "diff --git a/foo.py b/foo.py\n"
                                 "--- a/foo.py\n+++ b/foo.py\n"
                                 "@@ -1,1 +1,2 @@\n x\n+y\n", "")
        if "api" in joined and "reviews" in joined:
            return ExecResult(0, '{"html_url":"https://github.com/o/r/pull/3#x"}', "")
        return ExecResult(0, "", "")


def _review_mgr(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, ReviewHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    return mgr, store, cfg


def test_review_persists_user_and_assistant_messages(tmp_path):
    # Web parity: the web transcript renders store.list_messages. A review must
    # persist the request + the agent's result (like turn()), not only a lone
    # "Review posted" system line — otherwise the web app and Slack show
    # materially different conversations for the same session.
    mgr, store, cfg = _review_mgr(tmp_path)
    rid = "rr"
    ws = tmp_path / "runs" / rid / "workspace" / ".forge"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "review.json").write_text('{"summary":"ok","comments":[]}')
    list(mgr.review(rid, "o/r#3"))
    msgs = store.list_messages(rid)
    roles = [m["role"] for m in msgs]
    assert "user" in roles and "assistant" in roles
    assert "o/r#3" in next(m for m in msgs if m["role"] == "user")["content"]
    assert next(m for m in msgs if m["role"] == "assistant")["content"]


def test_review_bad_ref_yields_error(tmp_path):
    mgr, store, _ = _review_mgr(tmp_path)
    evs = list(mgr.review("rr", "not-a-ref"))
    assert evs[-1].kind == "error" and evs[-1].data["kind"] == "prref"


def test_review_posts_and_validates_inline_comments(tmp_path):
    mgr, store, cfg = _review_mgr(tmp_path)
    rid = "rr"
    # the faked worker "wrote" this review.json into the run workspace
    ws = tmp_path / "runs" / rid / "workspace" / ".forge"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "review.json").write_text(
        '{"summary":"looks ok","comments":['
        '{"path":"foo.py","line":2,"side":"RIGHT","body":"in"},'
        '{"path":"foo.py","line":99,"side":"RIGHT","body":"off"}]}')
    evs = list(mgr.review(rid, "o/r#3"))
    review_ev = [e for e in evs if e.kind == "review"][-1]
    assert review_ev.data["ok"] is True
    assert review_ev.data["review_url"] == "https://github.com/o/r/pull/3#x"
    assert review_ev.data["comments"] == 1      # in-diff kept
    assert review_ev.data["dropped"] == 1       # off-diff folded into summary
    assert review_ev.data["degraded"] is True   # no App configured → user token
    assert store.get_run(rid)["pr_url"] == "https://github.com/o/r/pull/3#x"


# --- open_pr verification gate (run the repo's checks before pushing) ---

class _VerifyFailEnv(FakeEnv):
    """Verify command (`npm test`) always fails, the worker fix never helps,
    and a `format` script call is recorded so we can assert auto-format ran."""
    def __init__(self, run_id, files):
        super().__init__(run_id, files)
        self.calls = []

    def exec(self, argv, service=None, workdir="/work", env=None):
        from forge.container import ExecResult
        self.calls.append(list(argv))
        if argv[:2] == ["npm", "test"]:
            return ExecResult(1, "FAIL src/x.ts: type error", "")
        return super().exec(argv, service=service, workdir=workdir)


def test_open_pr_drafts_and_warns_when_checks_fail(tmp_path):
    """A failing check must NOT yield a clean PR: forge tries to fix, and if it
    still fails the PR is opened as a draft and the failure is surfaced."""
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    mgr.env_factory = lambda rid, files: _VerifyFailEnv(rid, files)
    res = mgr.open_pr("r1")
    assert res["ok"] is True
    assert res["draft"] is True                       # failing checks → draft
    sys_msgs = [m["content"] for m in store.list_messages("r1")
                if m["role"] == "system"]
    assert any("test" in m and ("fail" in m.lower() or "check" in m.lower())
               for m in sys_msgs)


def test_open_pr_clean_pr_when_checks_pass(tmp_path):
    """All checks pass (default FakeEnv returns 0) → non-draft PR, no warning."""
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    res = mgr.open_pr("r1")
    assert res["ok"] is True
    assert res["draft"] is False


def test_open_pr_tries_to_fix_failing_checks(tmp_path):
    """When verify fails, open_pr dispatches a worker fix pass before giving up
    (so most failures self-heal instead of landing broken)."""
    mgr, store = _mgr(tmp_path)
    mgr.cfg.self_review = False                       # isolate the verify-fix worker
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    env = _VerifyFailEnv("r1", [])
    mgr.env_factory = lambda rid, files: env
    mgr.open_pr("r1")
    assert any(a[:2] == ["claude", "-p"] for a in env.calls)   # a fix worker ran
    # verify ran more than once (initial + re-verify after the fix attempt)
    assert sum(1 for a in env.calls if a[:2] == ["npm", "test"]) >= 2


def test_repair_runs_formatter_before_verifying(tmp_path):
    """The deterministic formatter (plan.format_fix) runs before the read-only
    checks so style-only diffs never reach CI. (_format_and_gate superseded by
    _repair; this test was updated in Task 3 to call the new helper.)"""
    from forge.verify import VerifyPlan, VerifyCmd
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    plan = VerifyPlan([VerifyCmd("format", ["npm", "run", "format:check"])], True,
                      VerifyCmd("format", ["npm", "run", "format"]))

    class _Rec(FakeEnv):
        def __init__(self, rid, files):
            super().__init__(rid, files)
            self.calls = []

        def exec(self, argv, service=None, workdir="/work", env=None):
            self.calls.append(list(argv))
            return super().exec(argv, service=service, workdir=workdir)

    env = _Rec("r1", [])
    from forge.session import _drain
    failed = _drain(mgr._repair("r1", env, plan))
    assert failed == []                               # check passes in FakeEnv
    fmt = ["npm", "run", "format"]
    chk = ["npm", "run", "format:check"]
    assert fmt in env.calls and chk in env.calls
    assert env.calls.index(fmt) < env.calls.index(chk)   # format BEFORE check


# ---------------------------------------------------------------------------
# Task 4 (Task 3 new test): _finish_pr
# ---------------------------------------------------------------------------

def test_finish_pr_opens_pr(tmp_path):
    """_finish_pr commits, pushes, and opens a PR.
    _planner_mgr's workspace has no package.json → has_real_verification=False
    → draft=True regardless of verify_failed."""
    mgr, store, flow = _planner_mgr(tmp_path)
    env = mgr._env_for("r1")
    out = mgr._finish_pr("r1", env, verify_failed=[])
    assert out["ok"] and out["pr_url"].startswith("https://github.com/")
    assert out["draft"] is True   # _planner_mgr's FakeHost has no real verification → draft


# ---------------------------------------------------------------------------
# Task 5: plan_task / _execute
# ---------------------------------------------------------------------------
import json as _json


class PlannerEnv(FakeEnv):
    """exec_stream writes a canned .forge/plan.json into the workspace, then
    yields a normal worker stream — exercising the real _read_plan path."""
    plan_obj = {"goal": "Add logout", "steps": [{"id": 1, "intent": "button"}],
                "acceptance": ["logout works"], "open_questions": [], "risk": "low"}

    def exec_stream(self, argv, service=None, workdir="/work"):
        yield from self._write_plan_then_stream()

    def _write_plan_then_stream(self):
        d = Path(self._ws)
        (d / ".forge").mkdir(parents=True, exist_ok=True)
        (d / ".forge" / "plan.json").write_text(_json.dumps(self.plan_obj))
        yield _json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": "planning"}]}})
        yield _json.dumps({"type": "result", "subtype": "success", "is_error": False,
                           "session_id": "sess-1", "result": "planned",
                           "total_cost_usd": 0.1, "num_turns": 1, "usage": {}})


def _planner_mgr(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = PlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e
    return SessionManager(cfg, store, FakeHost(), env_factory=factory), store, flow


def test_plan_task_gates_with_checkpoint(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    events = list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    kinds = [e.kind for e in events]
    assert "plan" in kinds
    assert events[-1].kind == "checkpoint"
    assert store.open_checkpoint("r1")["ctype"] == "plan_approval"
    assert store.get_run("r1")["lifecycle_state"] == "awaiting_approval"


def test_plan_task_auto_executes_when_ungated(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    kinds = [e.kind for e in events]
    assert "plan" in kinds
    assert "checkpoint" not in kinds
    assert events[-1].kind == "done"          # ran straight through _execute → PR
    assert events[-1].data.get("pr_url", "").startswith("https://github.com/")
    assert store.open_checkpoint("r1") is None
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


# ---------------------------------------------------------------------------
# Task 5 (image attachments): save_attachment / turn / plan_task / _execute
# ---------------------------------------------------------------------------

def test_save_attachment_lands_in_run_inbox(tmp_path):
    mgr, store = _mgr(tmp_path)
    name = mgr.save_attachment("r1", "bug.png", b"\x89PNG")
    assert (Path(mgr.cfg.runs_dir) / "r1" / "inbox" / name).is_file()


class CapturingPlannerEnv(PlannerEnv):
    """Like PlannerEnv (writes a canned plan.json) but also records the argv
    handed to exec_stream, so a test can assert on the prompt text."""
    last_argv = None

    def exec_stream(self, argv, service=None, workdir="/work"):
        type(self).last_argv = list(argv)
        yield from self._write_plan_then_stream()


def test_plan_task_persists_attachments_and_prompts_with_paths(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = CapturingPlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    name = mgr.save_attachment("r1", "bug.png", b"\x89PNG")
    list(mgr.plan_task("r1", "match the design",
                       policy=flow.CheckpointPolicy.for_web(), attachments=[name]))

    assert store.get_run("r1")["attachments_json"] == _json.dumps([name])
    argv = CapturingPlannerEnv.last_argv
    prompt = argv[argv.index("-p") + 1]
    assert f"/work/.forge/inbox/{name}" in prompt


def test_turn_prompts_with_attachment_paths(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.env_factory = lambda rid, files: CapturingEnv(rid, files)
    list(mgr.start("r1", "o/r", "github"))
    name = mgr.save_attachment("r1", "bug.png", b"\x89PNG")
    list(mgr.turn("r1", "fix it", attachments=[name]))
    argv = CapturingEnv.last_stream_argv
    prompt = argv[argv.index("-p") + 1]
    assert f"/work/.forge/inbox/{name}" in prompt


class RoundTripCapturingEnv(PlannerEnv):
    """Like CapturingPlannerEnv but remembers EVERY exec_stream call's argv (not
    just the last), so a test can distinguish the planning turn from the later
    (post-checkpoint) executor turn without a following QA turn clobbering it."""
    calls = []

    def exec_stream(self, argv, service=None, workdir="/work"):
        type(self).calls.append(list(argv))
        yield from self._write_plan_then_stream()


def test_attachments_survive_plan_approve_execute_roundtrip(tmp_path):
    """An attachment named at plan time must still resolve to a container path
    on the executor turn that runs after the human approves the checkpoint —
    the attachments list is persisted on the run row (not just held in-memory
    across the gate), and the inbox file is re-synced into the workspace on
    each turn."""
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    RoundTripCapturingEnv.calls = []

    def factory(rid, files):
        e = RoundTripCapturingEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    name = mgr.save_attachment("r1", "bug.png", b"\x89PNG")

    events = list(mgr.plan_task("r1", "match the design",
                                policy=flow.CheckpointPolicy.for_web(),
                                attachments=[name]))
    assert events[-1].kind == "checkpoint"
    cid = store.open_checkpoint("r1")["id"]

    events = list(mgr.respond_checkpoint("r1", cid, "approve"))
    assert events[-1].kind == "done"

    # calls[0] is the planning turn; calls[1] is the executor's turn that
    # resumes after approval. A QA turn may stream after that — assert on
    # index 1 specifically, NOT the last captured call.
    assert len(RoundTripCapturingEnv.calls) >= 2
    argv = RoundTripCapturingEnv.calls[1]
    prompt = argv[argv.index("-p") + 1]
    assert f"/work/.forge/inbox/{name}" in prompt

    assert (Path(cfg.runs_dir) / "r1" / "workspace" / ".forge" /
            "inbox" / name).is_file()


# ---------------------------------------------------------------------------
# Task 6: respond_checkpoint (approve / edit / reject)
# ---------------------------------------------------------------------------

def test_respond_approve_executes(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    cid = store.open_checkpoint("r1")["id"]
    events = list(mgr.respond_checkpoint("r1", cid, "approve"))
    assert events[-1].kind == "done"
    assert events[-1].data.get("pr_url", "").startswith("https://github.com/")
    assert store.open_checkpoint("r1") is None
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


def test_respond_reject_goes_idle(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    cid = store.open_checkpoint("r1")["id"]
    events = list(mgr.respond_checkpoint("r1", cid, "reject"))
    assert events[-1].kind == "done"
    assert store.get_run("r1")["lifecycle_state"] == "idle"
    assert store.open_checkpoint("r1") is None


def test_respond_edit_replans_and_reraises_checkpoint(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    cid = store.open_checkpoint("r1")["id"]
    events = list(mgr.respond_checkpoint("r1", cid, "edit",
                                         body="also handle logged-out state"))
    assert events[-1].kind == "checkpoint"
    new_cp = store.open_checkpoint("r1")
    assert new_cp["id"] != cid
    assert store.get_run("r1")["lifecycle_state"] == "awaiting_approval"


def test_respond_rejects_stale_checkpoint_id(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    events = list(mgr.respond_checkpoint("r1", 99999, "approve"))
    assert events[0].kind == "error"


def test_plan_task_errors_when_not_provisioned(tmp_path):
    """plan_task on a run with no live env must yield a not_provisioned error
    and must NOT create a checkpoint (the session has no container to exec into)."""
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    # Create a run but NO env row — simulates a fresh/unstarted session.
    store.create_run("r1", "o/r", "Add logout", "forge/x")

    def factory(rid, files):
        e = PlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    events = list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["kind"] == "not_provisioned"
    # No checkpoint must have been created.
    assert store.open_checkpoint("r1") is None


def test_respond_checkpoint_approve_errors_when_not_provisioned(tmp_path):
    """Approving a checkpoint on an unprovisioned (asleep) run must yield
    not_provisioned and leave the checkpoint open so the user can retry after wake."""
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    # Create env as live so plan_task succeeds and creates a checkpoint.
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = PlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    cid = store.open_checkpoint("r1")["id"]

    # Now simulate the session going asleep (env state becomes "asleep").
    store.mark_asleep("r1")

    events = list(mgr.respond_checkpoint("r1", cid, "approve"))
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["kind"] == "not_provisioned"
    # Checkpoint must still be open — the user can wake the session and retry.
    assert store.open_checkpoint("r1") is not None


# ---------------------------------------------------------------------------
# Task 2: _repair generator + _drain
# ---------------------------------------------------------------------------

def test_repair_reaches_green_after_fixes(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    env = mgr._env_for("r1")
    # A verify plan that fails twice then passes: fake via a stub plan + counter env.
    from forge.verify import VerifyPlan, VerifyCmd
    calls = {"n": 0}
    plan = VerifyPlan(commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
                      has_real_verification=True, format_fix=None)

    class FlakyEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            joined = " ".join(argv)
            if "run-tests" in joined:
                calls["n"] += 1
                return ExecResult(0, "", "") if calls["n"] >= 3 else ExecResult(1, "boom", "")
            return super().exec(argv, service=service, workdir=workdir)

    fenv = FlakyEnv("r1", env.files); fenv._ws = env._ws
    remaining = _drain_capture(mgr._repair("r1", fenv, plan, "auto"))
    assert remaining == []          # green after 2 fixes (3rd verify passes)
    assert calls["n"] >= 3


def test_repair_exhausts_and_returns_failures(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    from forge.verify import VerifyPlan, VerifyCmd

    class AlwaysRedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "still broken", "")
            return super().exec(argv, service=service, workdir=workdir)

    plan = VerifyPlan(commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
                      has_real_verification=True, format_fix=None)
    env = mgr._env_for("r1")
    aenv = AlwaysRedEnv("r1", env.files); aenv._ws = env._ws
    events = []
    remaining = _drain_capture(mgr._repair("r1", aenv, plan, "auto"), events)
    assert remaining == ["test"]                       # never went green
    assert any(e.kind == "repair" for e in events)     # emitted per-iter progress
    assert sum(1 for e in events if e.kind == "repair") == mgr.cfg.budget.max_repair_iters


# ---------------------------------------------------------------------------
# Task 4: _execute repair-then-complete (PR on green, escalate on red)
# ---------------------------------------------------------------------------

def test_execute_completes_with_pr_when_green(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].data.get("pr_url", "").startswith("https://github.com/")
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


def test_execute_escalates_when_repair_exhausts(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    # Force a real-but-always-red verify so repair exhausts.
    from forge.verify import VerifyPlan, VerifyCmd
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
        has_real_verification=True, format_fix=None)

    class RedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "red", "")
            return super().exec(argv, service=service, workdir=workdir)
    # rebuild the manager's env_factory to yield RedEnv
    def factory(rid, files):
        e = RedEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    assert events[-1].kind == "checkpoint"
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == "repair_escalation"
    assert store.get_run("r1")["lifecycle_state"] == "awaiting_input"


# ---------------------------------------------------------------------------
# Task 5: respond_checkpoint handles repair_escalation (retry/abort)
# ---------------------------------------------------------------------------

def test_respond_repair_escalation_abort(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
        has_real_verification=True, format_fix=None)

    class RedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "red", "")
            return super().exec(argv, service=service, workdir=workdir)
    def factory(rid, files):
        e = RedEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_cli(auto=True)))
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == "repair_escalation"
    events = list(mgr.respond_checkpoint("r1", cp["id"], "reject"))
    assert events[-1].kind == "done"
    assert store.get_run("r1")["lifecycle_state"] == "idle"
    assert store.open_checkpoint("r1") is None
    assert events[-1].data["message"] == "Stopped without pushing."
    assert events[-1].data["verify_ok"] is False


def test_repair_escalation_retry_threads_guidance_into_fix_prompt(tmp_path):
    """The user's guidance body typed on a repair-escalation retry must be
    forwarded all the way into the fix-worker prompt (argv[2] of the
    `claude -p <prompt> ...` call). This proves the wiring:
    respond_checkpoint -> _execute(extra_guidance=body) -> _repair(extra_guidance)
    -> build_fix_prompt(...) + '\\n\\nHuman guidance: ...'"""
    mgr, store, flow = _planner_mgr(tmp_path)
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
        has_real_verification=True, format_fix=None)

    # Capture fix-worker prompts: a fix-worker call is exec() with
    # argv[0]=="claude" and "-p" in argv (non-streaming worker_cmd).
    fix_prompts = []

    class RedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if argv and argv[0] == "claude" and "-p" in argv:
                # argv[2] is the prompt (["claude", "-p", <prompt>, ...])
                fix_prompts.append(argv[2])
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "red", "")
            return super().exec(argv, service=service, workdir=workdir)

    def factory(rid, files):
        e = RedEnv(rid, files)
        e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e
    mgr.env_factory = factory

    # Drive to a repair_escalation checkpoint.
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_cli(auto=True)))
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == "repair_escalation"

    # Retry with user guidance. action="approve" is not "reject"/"abort"/"stop"/"no"
    # so the non-abort (retry) path fires and body is passed as extra_guidance.
    list(mgr.respond_checkpoint("r1", cp["id"], "approve",
                                body="use the FooBar helper"))

    # At least one fix-worker prompt must contain the guidance substring.
    assert fix_prompts, "no fix-worker calls captured — wiring missing"
    assert any("use the FooBar helper" in p for p in fix_prompts), (
        f"guidance not found in any fix-worker prompt; captured prompts: {fix_prompts}"
    )


def test_provision_warm_starts_not_ups(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = str(tmp_path / "runs" / "r1" / "workspace")
    Path(ws).mkdir(parents=True, exist_ok=True)
    (Path(ws) / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    FakeEnv.up_calls = 0; FakeEnv.last_started = None
    events = list(mgr._provision("r1", ws, warm=True))
    assert FakeEnv.last_started == "r1"     # used start()
    assert FakeEnv.up_calls == 0            # did NOT up()
    assert any(e.kind == "url" for e in events) or \
           store.get_env("r1")["state"] in ("live", "failed")


def test_lockfile_hash_stable_and_changes(tmp_path):
    mgr, store = _mgr(tmp_path)
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "package-lock.json").write_text('{"v":1}')
    h1 = mgr._lockfile_hash(str(ws))
    assert h1 != "none" and h1 == mgr._lockfile_hash(str(ws))   # stable
    (ws / "package-lock.json").write_text('{"v":2}')
    assert mgr._lockfile_hash(str(ws)) != h1                    # changes with deps


def test_lockfile_hash_none_when_absent(tmp_path):
    mgr, store = _mgr(tmp_path)
    ws = tmp_path / "empty"; ws.mkdir()
    assert mgr._lockfile_hash(str(ws)) == "none"


# ---------------------------------------------------------------------------
# Task 6: wake(fresh=) warm/cold dispatch with cold fallback
# ---------------------------------------------------------------------------

def test_wake_warm_when_signature_matches(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = Path(tmp_path / "runs" / "r1" / "workspace"); ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (ws / "package-lock.json").write_text("{}")
    store.create_env("r1", "forge-r1", "u", 3000, "asleep", web_service="web")
    store.set_snapshot_lockhash("r1", mgr._lockfile_hash(str(ws)))   # signature matches
    FakeEnv.up_calls = 0; FakeEnv.last_started = None
    list(mgr.wake("r1"))
    assert FakeEnv.last_started == "r1" and FakeEnv.up_calls == 0     # warm path


def test_wake_cold_when_lockfile_changed(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = Path(tmp_path / "runs" / "r1" / "workspace"); ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (ws / "package-lock.json").write_text("{}")
    store.create_env("r1", "forge-r1", "u", 3000, "asleep", web_service="web")
    store.set_snapshot_lockhash("r1", "STALE")                       # signature mismatch
    FakeEnv.up_calls = 0; FakeEnv.last_started = None
    list(mgr.wake("r1"))
    assert FakeEnv.up_calls >= 1                                     # cold path (up)


def test_wake_fresh_forces_cold(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = Path(tmp_path / "runs" / "r1" / "workspace"); ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (ws / "package-lock.json").write_text("{}")
    store.create_env("r1", "forge-r1", "u", 3000, "asleep", web_service="web")
    store.set_snapshot_lockhash("r1", mgr._lockfile_hash(str(ws)))   # would match
    FakeEnv.up_calls = 0
    list(mgr.wake("r1", fresh=True))
    assert FakeEnv.up_calls >= 1                                     # forced cold


def test_wake_cold_fallback_on_warm_failure(tmp_path):
    """wake() must cold-reprovision when a warm start ends in state 'failed'.
    Exercises the branch: warm _provision → state==failed → down() → cold _provision."""
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = Path(tmp_path / "runs" / "r1" / "workspace"); ws.mkdir(parents=True)
    (ws / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (ws / "package-lock.json").write_text("{}")
    store.create_env("r1", "forge-r1", "u", 3000, "asleep", web_service="web")
    store.set_snapshot_lockhash("r1", mgr._lockfile_hash(str(ws)))   # warm-eligible

    calls = []

    def fake_provision(run_id, ws, warm=False):
        calls.append(warm)
        if warm:
            store.set_env_state(run_id, "failed")     # warm start unhealthy
        else:
            store.set_env_state(run_id, "live")       # cold succeeds
        yield TurnEvent("phase", {"name": "up", "label": "x"})

    mgr._provision = fake_provision
    FakeEnv.last_downed = None
    list(mgr.wake("r1"))
    assert calls == [True, False]                      # warm attempted, then cold fallback
    assert FakeEnv.last_downed == "r1"                 # env.down() ran before cold retry


# ---------------------------------------------------------------------------
# Task 4: _qa — browser acceptance QA turn
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 — already imported above as alias; safe duplicate
from forge.plan import Plan


class QaEnv(PlannerEnv):
    """exec_stream writes a canned .forge/qa.json when the prompt is a QA prompt
    (contains '.forge/qa.json'); otherwise behaves like PlannerEnv."""
    qa_obj = {"acceptance": [{"criterion": "logout works", "passed": False,
                              "evidence": "500"}], "summary": "0/1"}

    def exec_stream(self, argv, service=None, workdir="/work"):
        if any(".forge/qa.json" in a for a in argv):
            d = Path(self._ws); (d / ".forge").mkdir(parents=True, exist_ok=True)
            (d / ".forge" / "qa.json").write_text(_json.dumps(self.qa_obj))
            yield _json.dumps({"type": "result", "subtype": "success",
                               "is_error": False, "session_id": "sess-qa",
                               "result": "qa done", "total_cost_usd": 0.1,
                               "num_turns": 1, "usage": {}})
        else:
            yield from self._write_plan_then_stream()


def _qa_mgr(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = QaEnv(rid, files); e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    return SessionManager(cfg, store, FakeHost(), env_factory=factory), store, flow


def test_qa_returns_failures_and_emits_event(tmp_path):
    mgr, store, flow = _qa_mgr(tmp_path)
    env = mgr._env_for("r1")
    plan = Plan(goal="x", acceptance=("logout works",))
    events = []
    failed = _drain_capture(mgr._qa("r1", env, plan, "auto"), events)
    assert failed == ["logout works"]
    qa_events = [e for e in events if e.kind == "qa"]
    assert qa_events and qa_events[-1].data["failed"] == ["logout works"]
    assert qa_events[-1].data["checked"] == 1
    assert qa_events[-1].data["summary"] == "0/1"


def test_qa_inconclusive_when_no_qa_json(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)   # PlannerEnv writes plan.json, not qa.json
    env = mgr._env_for("r1")
    plan = Plan(goal="x", acceptance=("logout works",))
    events = []
    failed = _drain_capture(mgr._qa("r1", env, plan, "auto"), events)
    assert failed == []          # missing qa.json → inconclusive → no gating
    qa_events = [e for e in events if e.kind == "qa"]
    assert qa_events and qa_events[-1].data["failed"] == [] and qa_events[-1].data["checked"] == 0


# ---------------------------------------------------------------------------
# Task 5 (integration): _qa_gate + _execute acceptance-QA tier
# ---------------------------------------------------------------------------

def test_execute_escalates_on_acceptance_failure(tmp_path):
    mgr, store, flow = _qa_mgr(tmp_path)   # QaEnv writes a FAILING qa.json
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    assert events[-1].kind == "checkpoint"
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == "repair_escalation"
    assert "acceptance" in (cp["payload"].get("kind") or "")
    assert store.get_run("r1")["lifecycle_state"] == "awaiting_input"


def test_execute_advisory_qa_completes_despite_failure(tmp_path):
    mgr, store, flow = _qa_mgr(tmp_path)
    mgr.cfg.qa_gating = False                       # advisory
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    assert any(e.kind == "qa" for e in events)      # QA still ran + reported
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].data.get("pr_url")     # completed despite failed QA
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


# ---------------------------------------------------------------------------
# Task 5 (Phase 5): _retrospective + learn toggle
# ---------------------------------------------------------------------------
import json as _json  # noqa: F811 (already imported; safe alias re-use)


def test_retrospective_saves_lessons_to_overlay(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "t", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    class LessonEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if argv[:2] == ["claude", "-p"]:           # the retrospective worker turn
                d = Path(self._ws); (d / ".forge").mkdir(parents=True, exist_ok=True)
                (d / ".forge" / "lessons.json").write_text(_json.dumps(
                    {"lessons": [{"text": "use pnpm", "kind": "build"}]}))
                return ExecResult(0, _json.dumps({"subtype": "success", "is_error": False,
                                                  "session_id": "s", "result": "ok",
                                                  "usage": {}}), "")
            return super().exec(argv, service=service, workdir=workdir)

    def factory(rid, files):
        e = LessonEnv(rid, files); e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)

    n = mgr._retrospective("r1")
    assert n == 1
    saved = mgr.knowledge.load("o/r")["lessons"]
    assert saved[0]["text"] == "use pnpm" and saved[0]["added_run"] == "r1"


def test_retrospective_skipped_when_learn_off(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb", learn=False,
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "t", "forge/x")
    mgr = SessionManager(cfg, store, FakeHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    assert mgr._retrospective("r1") == 0
    assert mgr.knowledge.load("o/r") is None        # nothing saved


def test_qa_gate_bottoms_out_when_fix_breaks_ci(tmp_path):
    """_qa_gate's `if ci: return ci` branch: when a QA fix attempt regresses CI
    (verify command fails after the fix worker runs), _qa_gate must return the
    CI failure names immediately — not the acceptance criterion name."""
    from forge.verify import VerifyPlan, VerifyCmd
    mgr, store, flow = _qa_mgr(tmp_path)   # QaEnv writes a FAILING qa.json
    # Install a real verify plan that will fail when exec'd (exercises _repair).
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("lint", ["bash", "-lc", "run-lint"])],
        has_real_verification=True, format_fix=None)

    class CiFailEnv(QaEnv):
        """QA worker writes a failing qa.json; lint command always exits 1."""
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-lint" in " ".join(argv):
                return ExecResult(1, "lint broke", "")
            return super().exec(argv, service=service, workdir=workdir)

    def factory(rid, files):
        e = CiFailEnv(rid, files)
        e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr.env_factory = factory
    env = mgr._env_for("r1")
    plan = Plan(goal="x", acceptance=("logout works",))
    out = _drain_capture(mgr._qa_gate("r1", env, plan, "auto"))
    # Must return the CI failure name ("lint"), NOT the acceptance criterion
    # ("logout works") — proving the `if ci: return ci` branch was taken.
    assert out == ["lint"], (
        f"expected CI bottom-out ['lint'], got {out!r} — "
        "the if-ci-return-ci branch was NOT taken"
    )


# ---------------------------------------------------------------------------
# Task 4 (Phase 5): Planner reads per-repo lessons
# ---------------------------------------------------------------------------

def test_planner_includes_repo_lessons(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    seen = {}

    class CapturePlanEnv(PlannerEnv):
        def exec_stream(self, argv, service=None, workdir="/work"):
            # Capture the plan prompt specifically (starts with "You are planning")
            match = next((a for a in argv if "You are planning" in a), None)
            if match is not None:
                seen["prompt"] = match
            yield from self._write_plan_then_stream()

    def factory(rid, files):
        e = CapturePlanEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    mgr.knowledge.save("o/r", {"lessons": [{"text": "tests need DISPLAY=:99"}]})

    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_cli(auto=True)))
    assert "tests need DISPLAY=:99" in seen["prompt"]   # lesson reached the planner


def test_lessons_empty_when_no_overlay(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    assert mgr._lessons("r1") == []


# ---------------------------------------------------------------------------
# Phase 5 closed-loop: lesson written by Run A's retrospective reaches Run B's
# planner — proving the knowledge overlay persists across separate runs for the
# same repo slug.
# ---------------------------------------------------------------------------

def test_retrospective_lesson_reaches_later_run_planner(tmp_path):
    """End-to-end closed-loop proof:
    Run A  → _retrospective("rA")  writes {"text":"tests need DISPLAY=:99"} to the
              knowledge overlay for repo "o/r".
    Run B  → plan_task("rB", ...)   reads that overlay and injects the lesson text
              into its planner prompt (argv arg containing "You are planning").
    Both runs share the same SessionManager (and hence the same knowledge_dir),
    which is all that's needed for the overlay to be visible across runs.
    """
    from forge import flow

    kb_dir = tmp_path / "kb"
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=kb_dir,
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")

    # --- Run A setup ----------------------------------------------------------
    store.create_run("rA", "o/r", "First task", "forge/x")
    store.create_env("rA", "forge-rA", None, 3000, "live", web_service="web")

    # --- Run B setup ----------------------------------------------------------
    store.create_run("rB", "o/r", "Second task", "forge/x")
    store.create_env("rB", "forge-rB", None, 3000, "live", web_service="web")

    # Capture the plan prompt from Run B
    captured = {}

    class ClosedLoopEnv(PlannerEnv):
        """Dispatches by inspecting the argv:
        - retrospective worker (argv[:2] == ["claude", "-p"] for sync exec):
            → writes lessons.json for Run A's retrospective.
        - plan prompt (exec_stream with "You are planning" in any arg):
            → captures the prompt, writes plan.json so _read_plan succeeds.
        - else: delegate to PlannerEnv defaults.
        """

        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if argv[:2] == ["claude", "-p"]:
                # Retrospective worker: write the lesson that should propagate to Run B
                d = Path(self._ws)
                (d / ".forge").mkdir(parents=True, exist_ok=True)
                (d / ".forge" / "lessons.json").write_text(_json.dumps(
                    {"lessons": [{"text": "tests need DISPLAY=:99", "kind": "test"}]}))
                return ExecResult(
                    0,
                    _json.dumps({"subtype": "success", "is_error": False,
                                 "session_id": "sess-retro", "result": "ok",
                                 "usage": {}}),
                    "",
                )
            return super().exec(argv, service=service, workdir=workdir)

        def exec_stream(self, argv, service=None, workdir="/work"):
            # Capture plan prompt from Run B's planner call
            match = next((a for a in argv if "You are planning" in a), None)
            if match is not None:
                captured["plan_prompt"] = match
            yield from self._write_plan_then_stream()

    def factory(rid, files):
        e = ClosedLoopEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)

    # --- Run A: retrospective writes the lesson to the shared overlay ----------
    n = mgr._retrospective("rA")
    assert n == 1, "retrospective must have saved 1 lesson"
    overlay = mgr.knowledge.load("o/r")
    assert overlay is not None, "knowledge overlay must exist after retrospective"
    lesson_texts = [l["text"] for l in overlay.get("lessons", [])]
    assert "tests need DISPLAY=:99" in lesson_texts, (
        f"lesson not in overlay after Run A: {lesson_texts!r}"
    )

    # --- Run B: plan_task reads the overlay and injects lessons into its prompt --
    list(mgr.plan_task("rB", "Second task",
                       policy=flow.CheckpointPolicy.for_cli(auto=True)))

    assert "plan_prompt" in captured, (
        "Run B's plan prompt was never captured — "
        "check that ClosedLoopEnv.exec_stream is being called for the planner"
    )
    assert "tests need DISPLAY=:99" in captured["plan_prompt"], (
        f"lesson text not found in Run B's plan prompt.\n"
        f"Captured prompt (first 500 chars): {captured['plan_prompt'][:500]!r}"
    )


# ---------------------------------------------------------------------------
# Managed parallelism (Task 3): autonomous=True skips the ambiguity gate and
# drafts a PR on verify/QA bottom-out instead of stalling on a checkpoint.
# ---------------------------------------------------------------------------

def test_autonomous_plan_with_open_questions_does_not_gate(tmp_path):
    """autonomous=True → no ambiguity checkpoint even when the plan has open
    questions; it proceeds straight to execute + PR."""
    mgr, store, flow = _planner_mgr(tmp_path)

    class OpenQEnv(PlannerEnv):
        plan_obj = {"goal": "g", "steps": [{"id": 1, "intent": "x"}],
                    "acceptance": [], "open_questions": ["which db?"], "risk": "low"}

    def factory(rid, files):
        e = OpenQEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    events = list(mgr.plan_task("r1", "task",
                                policy=flow.CheckpointPolicy.for_cli(auto=True),
                                autonomous=True))
    assert store.open_checkpoint("r1") is None            # no ambiguity gate
    assert events[-1].kind == "done"


def test_autonomous_verify_bottom_out_opens_draft_pr(tmp_path):
    """autonomous=True → unfixable verify failures → draft PR (done), NOT a
    repair-escalation checkpoint."""
    mgr, store, flow = _planner_mgr(tmp_path)
    from forge.verify import VerifyPlan, VerifyCmd
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
        has_real_verification=True, format_fix=None)

    class RedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "red", "")
            return super().exec(argv, service=service, workdir=workdir)

    def factory(rid, files):
        e = RedEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    events = list(mgr.plan_task("r1", "task",
                                policy=flow.CheckpointPolicy.for_cli(auto=True),
                                autonomous=True))
    assert store.open_checkpoint("r1") is None
    done = [e for e in events if e.kind == "done"][-1]
    assert done.data.get("draft") is True
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


def test_non_autonomous_verify_bottom_out_still_escalates(tmp_path):
    """Regression: WITHOUT autonomous=True, for_cli(auto=True) still escalates
    (the pre-existing behavior the escalation tests rely on)."""
    mgr, store, flow = _planner_mgr(tmp_path)
    from forge.verify import VerifyPlan, VerifyCmd
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
        has_real_verification=True, format_fix=None)

    class RedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "red", "")
            return super().exec(argv, service=service, workdir=workdir)

    def factory(rid, files):
        e = RedEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    events = list(mgr.plan_task("r1", "task",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    assert events[-1].kind == "checkpoint"
    assert store.open_checkpoint("r1")["ctype"] == "repair_escalation"


def test_autonomous_acceptance_qa_fail_opens_draft_pr(tmp_path):
    """autonomous=True → failing acceptance QA drafts a PR instead of escalating."""
    mgr, store, flow = _qa_mgr(tmp_path)   # QaEnv writes a FAILING qa.json
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True),
                                autonomous=True))
    assert store.open_checkpoint("r1") is None
    done = [e for e in events if e.kind == "done"][-1]
    assert done.data.get("draft") is True
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


# ---------------------------------------------------------------------------
# Blocked-QA: needs_credentials -> NEEDS_INPUT checkpoint; resume with creds
# ---------------------------------------------------------------------------

class BlockedQaEnv(PlannerEnv):
    """QA writes a BLOCKED qa.json until credentials appear in the prompt
    (rendered as `username=…`), then writes a PASSING qa.json — exercising the
    full inject-creds-and-retry loop."""
    def exec_stream(self, argv, service=None, workdir="/work"):
        if any(".forge/qa.json" in a for a in argv):
            has_creds = any("username=" in a for a in argv)
            obj = ({"acceptance": [{"criterion": "logout works", "passed": True,
                                    "evidence": "logged in ok"}],
                    "summary": "1/1", "blocked": None} if has_creds else
                   {"acceptance": [{"criterion": "logout works", "passed": False,
                                    "evidence": "login wall"}], "summary": "0/1",
                    "blocked": {"kind": "needs_credentials",
                                "question": "which login should I use?"}})
            d = Path(self._ws); (d / ".forge").mkdir(parents=True, exist_ok=True)
            (d / ".forge" / "qa.json").write_text(_json.dumps(obj))
            yield _json.dumps({"type": "result", "subtype": "success",
                               "is_error": False, "session_id": "sess-qa",
                               "result": "qa done", "total_cost_usd": 0.1,
                               "num_turns": 1, "usage": {}})
        else:
            yield from self._write_plan_then_stream()


def _blocked_qa_mgr(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "knowledge",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = BlockedQaEnv(rid, files); e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    return SessionManager(cfg, store, FakeHost(), env_factory=factory), store, flow


def test_qa_blocked_raises_needs_input_checkpoint(tmp_path):
    mgr, store, flow = _blocked_qa_mgr(tmp_path)
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_cli(auto=True)))
    assert events[-1].kind == "checkpoint"
    assert events[-1].data["type"] == flow.NEEDS_INPUT
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == flow.NEEDS_INPUT
    assert store.get_run("r1")["lifecycle_state"] == flow.AWAITING_INPUT
    # No PR was pushed while blocked.
    assert not any(e.kind == "done" and e.data.get("pr_url") for e in events)


def test_needs_input_reply_saves_creds_and_resumes(tmp_path):
    mgr, store, flow = _blocked_qa_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout",
                       policy=flow.CheckpointPolicy.for_cli(auto=True)))   # -> NEEDS_INPUT
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == flow.NEEDS_INPUT
    events = list(mgr.respond_checkpoint(
        "r1", cp["id"], "edit", "admin@x.com :: s3cret for admin account"))
    saved = (mgr.knowledge.load("o/r") or {}).get("qa_credentials")
    assert saved == [{"role": "admin", "username": "admin@x.com", "password": "s3cret"}]
    assert any(e.kind == "creds_saved" for e in events)
    # BlockedQaEnv passes once creds are injected → resume completes with a PR.
    assert any(e.kind == "done" and e.data.get("pr_url") for e in events)
    assert store.open_checkpoint("r1") is None


def test_needs_input_reply_redacts_credentials_from_transcript_and_event(tmp_path):
    # The QA login reply is a password: it must reach parse_credentials (creds
    # are saved) but never the API-served transcript or the mirrored
    # checkpoint_answered event that Slack echoes.
    mgr, store, flow = _blocked_qa_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout",
                       policy=flow.CheckpointPolicy.for_cli(auto=True)))
    cp = store.open_checkpoint("r1")
    secret = "s3cret"
    events = list(mgr.respond_checkpoint(
        "r1", cp["id"], "edit", f"admin@x.com :: {secret} for admin account"))
    # creds were still parsed and saved (raw body reached the logic)
    assert (mgr.knowledge.load("o/r") or {}).get("qa_credentials")
    # ...but the secret appears in neither the event nor the transcript
    answered = [e for e in events if e.kind == "checkpoint_answered"]
    assert answered and answered[0].data["body"] == "[credentials provided]"
    assert all(secret not in (e.data.get("body") or "")
               for e in events if e.kind == "checkpoint_answered")
    assert all(secret not in (m.get("text") or "")
               for m in store.list_messages("r1"))


def test_needs_input_reject_stops_without_pushing(tmp_path):
    mgr, store, flow = _blocked_qa_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout",
                       policy=flow.CheckpointPolicy.for_cli(auto=True)))
    cp = store.open_checkpoint("r1")
    events = list(mgr.respond_checkpoint("r1", cp["id"], "reject"))
    assert events[-1].kind == "done"
    assert store.get_run("r1")["lifecycle_state"] == flow.IDLE
    assert not any(e.data.get("pr_url") for e in events if e.kind == "done")


def test_forget_credentials_removes_them(tmp_path):
    mgr, store, flow = _blocked_qa_mgr(tmp_path)
    mgr.knowledge.merge_save("o/r", {"qa_credentials": [
        {"username": "u", "password": "p"}]})
    assert mgr.forget_credentials("o/r") is True
    assert "qa_credentials" not in (mgr.knowledge.load("o/r") or {})
    assert mgr.forget_credentials("o/r") is False          # nothing to remove


# ---------------------------------------------------------------------------
# auto_draft decoupled from autonomous: Slack builds keep the ambiguity gate
# (the one "strictly necessary" stop) but never stall on execution bottom-outs
# — they open a DRAFT PR instead. Credential walls ask AT MOST ONCE, then draft.
# ---------------------------------------------------------------------------

def test_auto_draft_keeps_ambiguity_gate(tmp_path):
    """auto_draft=True with autonomous left False: an open-question plan STILL
    gates (ambiguity is the strictly-necessary stop), unlike autonomous=True."""
    mgr, store, flow = _planner_mgr(tmp_path)

    class OpenQEnv(PlannerEnv):
        plan_obj = {"goal": "g", "steps": [{"id": 1, "intent": "x"}],
                    "acceptance": [], "open_questions": ["which db?"], "risk": "low"}

    def factory(rid, files):
        e = OpenQEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    events = list(mgr.plan_task("r1", "task",
                                policy=flow.CheckpointPolicy.for_slack(),
                                auto_draft=True))
    assert events[-1].kind == "checkpoint"
    assert store.open_checkpoint("r1")["ctype"] == flow.AMBIGUITY


def test_auto_draft_verify_bottom_out_opens_draft_pr(tmp_path):
    """auto_draft=True (autonomous False) → unfixable CI → draft PR, not a
    repair-escalation checkpoint. This is the Slack execution-bottom-out path."""
    mgr, store, flow = _planner_mgr(tmp_path)
    from forge.verify import VerifyPlan, VerifyCmd
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "run-tests"])],
        has_real_verification=True, format_fix=None)

    class RedEnv(PlannerEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            from forge.container import ExecResult
            if "run-tests" in " ".join(argv):
                return ExecResult(1, "red", "")
            return super().exec(argv, service=service, workdir=workdir)

    def factory(rid, files):
        e = RedEnv(rid, files); e._ws = str(mgr.cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    mgr.env_factory = factory

    events = list(mgr.plan_task("r1", "task",
                                policy=flow.CheckpointPolicy.for_slack(),
                                auto_draft=True))
    assert store.open_checkpoint("r1") is None
    done = [e for e in events if e.kind == "done"][-1]
    assert done.data.get("draft") is True
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


class AlwaysBlockedQaEnv(PlannerEnv):
    """QA ALWAYS writes a blocked qa.json — the login wall can't be crossed even
    when credentials are supplied (wrong password / wrong role)."""
    def exec_stream(self, argv, service=None, workdir="/work"):
        if any(".forge/qa.json" in a for a in argv):
            obj = {"acceptance": [{"criterion": "x", "passed": False,
                                   "evidence": "login wall"}], "summary": "0/1",
                   "blocked": {"kind": "needs_credentials",
                               "question": "which login should I use?"}}
            d = Path(self._ws); (d / ".forge").mkdir(parents=True, exist_ok=True)
            (d / ".forge" / "qa.json").write_text(_json.dumps(obj))
            yield _json.dumps({"type": "result", "subtype": "success",
                               "is_error": False, "session_id": "sess-qa",
                               "result": "qa done", "total_cost_usd": 0.1,
                               "num_turns": 1, "usage": {}})
        else:
            yield from self._write_plan_then_stream()


def _always_blocked_mgr(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "knowledge",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = AlwaysBlockedQaEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True); return e
    return SessionManager(cfg, store, FakeHost(), env_factory=factory), store, flow


def test_qa_blocked_auto_draft_asks_once(tmp_path):
    """First cred wall in auto_draft mode with no saved creds → ask ONE time
    (NEEDS_INPUT), carrying the agent's work summary so Slack shows the work."""
    mgr, store, flow = _blocked_qa_mgr(tmp_path)
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_slack(),
                                auto_draft=True))
    assert events[-1].kind == "checkpoint"
    assert events[-1].data["type"] == flow.NEEDS_INPUT
    assert events[-1].data.get("summary")          # work summary rides along
    assert store.open_checkpoint("r1")["ctype"] == flow.NEEDS_INPUT
    assert not any(e.kind == "done" and e.data.get("pr_url") for e in events)


def test_qa_blocked_auto_draft_resume_still_blocked_drafts_pr(tmp_path):
    """After the one-time ask, if creds still can't cross the wall, do NOT loop —
    open a draft PR. (This resume path only drafts if respond_checkpoint restores
    the persisted auto_draft flag, so it also proves persistence across resume.)"""
    mgr, store, flow = _always_blocked_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout",
                       policy=flow.CheckpointPolicy.for_slack(),
                       auto_draft=True))                       # → NEEDS_INPUT (asked once)
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == flow.NEEDS_INPUT
    events = list(mgr.respond_checkpoint(
        "r1", cp["id"], "edit", "admin@x.com :: s3cret for admin"))
    assert store.open_checkpoint("r1") is None              # no re-ask, no loop
    done = [e for e in events if e.kind == "done"][-1]
    assert done.data.get("draft") is True
    assert done.data.get("pr_url")
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


def test_qa_blocked_auto_draft_with_saved_creds_drafts_without_asking(tmp_path):
    """Creds already saved for the repo but the wall still blocks → don't ask
    again, go straight to a draft PR."""
    mgr, store, flow = _always_blocked_mgr(tmp_path)
    mgr.knowledge.merge_save("o/r", {"qa_credentials": [
        {"role": "admin", "username": "a@x", "password": "p"}]})
    events = list(mgr.plan_task("r1", "Add logout",
                                policy=flow.CheckpointPolicy.for_slack(),
                                auto_draft=True))
    assert store.open_checkpoint("r1") is None              # never asked
    assert not any(e.kind == "checkpoint" for e in events)
    done = [e for e in events if e.kind == "done"][-1]
    assert done.data.get("draft") is True
    assert store.get_run("r1")["lifecycle_state"] == "pr_open"


def test_qa_blocked_supervised_reasks_on_resume(tmp_path):
    """Regression: WITHOUT auto_draft (web/CLI), a still-blocked resume re-asks
    (NEEDS_INPUT again) rather than drafting — supervised behavior is unchanged."""
    mgr, store, flow = _always_blocked_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout",
                       policy=flow.CheckpointPolicy.for_cli(auto=True)))  # supervised
    cp = store.open_checkpoint("r1")
    assert cp["ctype"] == flow.NEEDS_INPUT
    events = list(mgr.respond_checkpoint(
        "r1", cp["id"], "edit", "admin@x.com :: s3cret for admin"))
    # Still blocked → asks again; never drafts a PR behind the user's back.
    assert events[-1].kind == "checkpoint"
    assert events[-1].data["type"] == flow.NEEDS_INPUT
    assert not any(e.kind == "done" and e.data.get("pr_url") for e in events)


# ---------------------------------------------------------------------------
# Interruptibility: env caching (so stop() cancels) + graceful deferred sleep
# ---------------------------------------------------------------------------

def test_stop_cancels_the_live_env(tmp_path):
    """stop() must cancel the env currently streaming (recorded by _env_for), not
    a throwaway fresh env whose _proc is None (the pre-fix no-op bug)."""
    mgr, store = _mgr(tmp_path)
    cancelled = []

    class CancelEnv(FakeEnv):
        def cancel(self):
            cancelled.append(self.run_id)

    mgr.env_factory = lambda rid, files: CancelEnv(rid, files)
    mgr._env_for("r1")                         # a turn obtains + streams on this env
    mgr._active.add("r1")
    mgr.stop("r1")
    assert cancelled == ["r1"]                 # the live subprocess was cancelled
    assert "r1" not in mgr._active


def test_request_sleep_defers_when_turn_active(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr._active.add("run1")
    assert mgr.request_sleep("run1") == "deferred"
    assert "run1" in mgr._sleep_requested


def test_request_sleep_deferred_remembers_reason(tmp_path):
    # The reason survives the deferral so the boundary sleep is attributed to
    # the surface that asked (and the Slack sweep notice can stay silent).
    mgr, store = _mgr(tmp_path)
    mgr._active.add("run1")
    assert mgr.request_sleep("run1", reason="slack") == "deferred"
    assert mgr._sleep_requested.get("run1") == "slack"


def test_request_sleep_sleeps_now_when_idle(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    store.create_env("r1", "forge-r1", "u", 3000, "live", web_service="web")
    Path(tmp_path / "runs" / "r1" / "workspace").mkdir(parents=True, exist_ok=True)
    assert mgr.request_sleep("r1") == "sleeping"
    assert store.get_env("r1")["state"] == "asleep"


def test_execute_pauses_at_boundary_when_sleep_requested(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    mgr._sleep_requested["r1"] = "slack"
    events = list(mgr._execute("r1", "auto"))
    assert any(e.kind == "slept" for e in events)
    assert store.get_run("r1")["state"] == "asleep"
    assert not any(e.kind == "done" and e.data.get("pr_url") for e in events)


def test_mark_asleep_and_deleted_persist_reason(tmp_path):
    store = Store(tmp_path / "f.db")
    store.create_run("r1", "o/r", "t", "b")
    store.create_env("r1", "p", None, 1, "live")
    store.mark_asleep("r1", reason="idle")
    assert store.get_env("r1")["state_reason"] == "idle"
    store.mark_deleted("r1", reason="web")
    assert store.get_env("r1")["state_reason"] == "web"


def test_sleep_records_reason(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    assert mgr.sleep("r1", reason="web") is True
    assert store.get_env("r1")["state_reason"] == "web"


def test_end_records_reason(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.end("r1", reason="web")
    assert store.get_env("r1")["state_reason"] == "web"


def test_reconcile_records_restart_reason(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.reconcile(ps_checker=lambda project: False)
    assert store.get_env("r1")["state_reason"] == "restart"


def test_delete_dormant_records_reason(tmp_path):
    mgr, store = _mgr(tmp_path)
    store.create_run("r1", "o/r", "t", "forge/x")
    ws = Path(tmp_path / "runs" / "r1" / "workspace"); ws.mkdir(parents=True)
    store.create_env("r1", "forge-r1", "u", 3000, "asleep", web_service="web")
    mgr._archive_code = lambda rid: True            # skip real git archive
    assert mgr.delete_dormant("r1") is True
    assert store.get_env("r1")["state_reason"] == "dormant"


# --- GitHub-token containment (the worker never holds the PAT) -------------

def test_worker_container_env_carries_no_github_token(tmp_path):
    # The worker runs untrusted repo code with permission gates disabled; no
    # GitHub token may sit in its environment. The key stays (empty) so the
    # compose ${GH_TOKEN} interpolation is quiet.
    mgr, _store = _mgr(tmp_path)
    assert mgr._secrets()["GH_TOKEN"] == ""


def test_git_execs_receive_a_per_exec_token(tmp_path):
    # forge's own git/gh execs (credential setup, push, PR create) get the
    # token injected per exec — the PAT fallback when no App is configured.
    gh_execs = []

    class RecordingEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            if env is not None:
                gh_execs.append((list(argv), dict(env)))
            return super().exec(argv, service=service, workdir=workdir)

    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="ghp_pat",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, FakeHost(),
                         env_factory=lambda rid, files: RecordingEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    assert mgr.open_pr("r1")["ok"] is True
    argvs = [" ".join(a) for a, _ in gh_execs]
    assert any(a.startswith("gh auth setup-git") for a in argvs)
    assert any("push" in a for a in argvs)
    assert any("pr create" in a for a in argvs)
    assert all(e == {"GH_TOKEN": "ghp_pat"} for _, e in gh_execs)


def test_git_execs_use_scoped_app_token_when_app_configured(tmp_path, monkeypatch):
    from forge import ghapp
    seen = {}

    def fake_worker_token(cfg, slug, app=None):
        seen["slug"] = slug
        return "ghs_scoped"
    monkeypatch.setattr(ghapp, "worker_token", fake_worker_token)
    gh_execs = []

    class RecordingEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work", env=None):
            if env is not None:
                gh_execs.append(dict(env))
            return super().exec(argv, service=service, workdir=workdir)

    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="ghp_pat",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, FakeHost(),
                         env_factory=lambda rid, files: RecordingEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    assert seen["slug"] == "o/r"
    assert gh_execs and all(e == {"GH_TOKEN": "ghs_scoped"} for e in gh_execs)


def test_archive_code_hardens_host_git(tmp_path):
    # Host-side git against a workspace the agent has modified treats the
    # repo's .git/config as hostile input: core.fsmonitor / core.hooksPath /
    # credential.helper could otherwise execute arbitrary commands ON THE HOST.
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr.sleep("r1")
    ws = tmp_path / "runs" / "r1" / "workspace"
    (ws / ".git").mkdir(parents=True, exist_ok=True)
    git_calls = []

    class RecordingHost(FakeHost):
        def run(self, argv, env=None):
            from forge.container import ExecResult
            if argv[:1] == ["git"]:
                git_calls.append(list(argv))
            return ExecResult(0, "", "")

    mgr.host = RecordingHost()
    assert mgr.delete_dormant("r1") is True
    assert git_calls
    for argv in git_calls:
        joined = " ".join(argv)
        assert "core.fsmonitor=false" in joined, argv
        assert "core.hooksPath=/dev/null" in joined, argv


def test_probe_learns_full_synthesis_and_spins_up_app(tmp_path, monkeypatch):
    # The "spin up anything" path: a repo with NO recognized marker (no
    # package.json / supabase / compose) provisions end-to-end because the probe
    # agent read the repo and described the environment; the resolver
    # synthesizes a runnable compose from that overlay.
    import json
    from forge import envprobe
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kb",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    monkeypatch.setattr(envprobe, "probe", lambda *a, **k: {
        "image": "python:3.12-slim",
        "setup_cmds": ["pip install -e ."],
        "dev_cmd": "python -m app",
        "web_port": 8000,
        "services": {"db": {"image": "postgres:16",
                            "environment": {"POSTGRES_PASSWORD": "forge"}}}})
    mgr = SessionManager(cfg, store, _NoAppHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    events = list(mgr.start("r1", "o/r", "github"))
    assert events[-1].kind == "url"                     # app is fronted, not worker-only
    labels = [e.data.get("label") for e in events if e.kind == "phase"]
    assert "Recipe: synthesized" in labels              # re-resolve surfaced to the user
    compose = json.loads((tmp_path / "runs" / "r1" / "forge-compose.yml").read_text())
    web = compose["services"]["web"]
    assert web["image"] == "python:3.12-slim"
    assert "pip install -e ." in web["command"][0]
    assert "PORT=8000 python -m app" in web["command"][0]
    assert compose["services"]["db"]["image"] == "postgres:16"
    facts = json.loads(store.get_env("r1")["runtime_facts"])
    assert facts["stack"] == "synthesized" and facts["dev_cmd"] == "python -m app"
