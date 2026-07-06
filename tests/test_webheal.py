from forge import webheal

# A real tail of a dev-server log with the Turbopack persistent-cache corruption
# that makes every route 500 (observed 2026-07-06, opplandstaal #3eba…).
CORRUPT_LOG = """\
 GET /sign-in 500 in 120ms
⨯ Error [TurbopackInternalError]: Failed to lookup task ids: Looking up task id \
for CachedTaskType { native_fn: NativeFunction { name: "endpoint_write_to_disk_operation" } }
  Caused by:
      0: Unable to open static sorted file referenced from 00000076.meta
      2: Failed to open SST file /work/.next/dev/cache/turbopack/f37fad94/00000072.sst
      3: No such file or directory (os error 2)
Error: ENOENT: no such file or directory, open \
'/work/.next/dev/server/app/(auth-pages)/sign-in/page/build-manifest.json'
"""

# A genuine application 500 (bad code the agent wrote) — NOT recoverable by
# clearing the cache, so it must not match the corruption signature.
REAL_APP_ERROR_LOG = """\
 GET /dashboard 500 in 40ms
⨯ TypeError: Cannot read properties of undefined (reading 'map')
    at Dashboard (app/dashboard/page.tsx:22:31)
"""


def test_is_corruption_matches_turbopack_sst_signature():
    assert webheal.is_corruption(CORRUPT_LOG) is True


def test_is_corruption_matches_build_manifest_enoent():
    only_enoent = ("Error: ENOENT: no such file or directory, open "
                   "'/work/.next/dev/server/app/page/build-manifest.json'")
    assert webheal.is_corruption(only_enoent) is True


def test_is_corruption_ignores_real_app_error():
    assert webheal.is_corruption(REAL_APP_ERROR_LOG) is False


def test_is_corruption_empty_is_false():
    assert webheal.is_corruption("") is False
    assert webheal.is_corruption(None) is False


def test_status_probe_argv_follows_redirects():
    # / often 307s to /sign-in (auth middleware); the destination is what 500s
    # under corruption, so the probe MUST follow redirects to see it.
    argv = webheal.status_probe_argv("web", 3000, "/")
    assert argv[0] == "curl"
    assert "-L" in " ".join(argv) or "-sL" in argv
    assert "http://web:3000/" in argv
    # writes only the final HTTP status to stdout
    assert "%{http_code}" in argv


def test_is_server_error():
    assert webheal.is_server_error("500") is True
    assert webheal.is_server_error("503") is True
    assert webheal.is_server_error("200") is False
    assert webheal.is_server_error("307") is False
    assert webheal.is_server_error("404") is False
    assert webheal.is_server_error("000") is False   # curl connection failure
    assert webheal.is_server_error("") is False


def test_is_reachable():
    # A real HTTP response (any 3-digit code) means the app answered; curl's
    # 000 (connection failure) and empty output mean it didn't.
    assert webheal.is_reachable("200") is True
    assert webheal.is_reachable("307") is True
    assert webheal.is_reachable("500") is True
    assert webheal.is_reachable("000") is False
    assert webheal.is_reachable("") is False


def test_clear_cache_argv_removes_next():
    argv = webheal.clear_cache_argv()
    assert "rm" in argv and "-rf" in argv
    assert any(".next" in a for a in argv)


# ── orchestration: SessionManager.heal_corrupted_web ──────────────────────────

from forge.config import Config, Budget
from forge.store import Store
from forge.session import SessionManager
from forge.container import ExecResult


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _FakeEnv:
    """Records exec/restart and returns a scripted probe status + logs."""

    def __init__(self, rid, files):
        self.run_id = rid
        self.probe_status = "200"
        self.probe_exit = 0
        self.log_text = ""
        self.exec_calls = []      # (argv, service)
        self.restart_calls = []   # service

    def exec(self, argv, workdir="/work", service=None, env=None):
        self.exec_calls.append((list(argv), service))
        if argv and argv[0] == "curl":
            return ExecResult(self.probe_exit, self.probe_status, "")
        return ExecResult(0, "", "")

    def logs(self, service=None):
        return self.log_text

    def restart(self, service):
        self.restart_calls.append(service)


def _mgr(tmp_path, clock, **cfg_kw):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 knowledge_dir=tmp_path / "kn",
                 budget=Budget(max_iterations=2, max_wall_secs=60), **cfg_kw)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.runs_dir / "forge.db")
    envs: dict = {}

    def factory(rid, files):
        return envs.setdefault(rid, _FakeEnv(rid, files))

    mgr = SessionManager(cfg, store, object(), env_factory=factory, clock=clock)
    return mgr, store, envs


def _live_web(store, rid="r1", web_service="web", web_port=3000):
    store.create_env(rid, f"forge-{rid}", None, web_port, "live",
                     web_service=web_service)


def test_heals_when_5xx_and_corruption_signature(tmp_path):
    mgr, store, envs = _mgr(tmp_path, _Clock())
    _live_web(store)
    envs_seed = mgr._env_for("r1")            # materialise the fake for scripting
    envs_seed.probe_status, envs_seed.log_text = "500", CORRUPT_LOG

    healed = mgr.heal_corrupted_web()

    assert healed == ["r1"]
    env = envs["r1"]
    assert env.restart_calls == ["web"]
    # cleared the cache before restarting
    assert any(webheal.clear_cache_argv() == argv for argv, _ in env.exec_calls)


def test_probe_targets_web_service_from_worker(tmp_path):
    mgr, store, envs = _mgr(tmp_path, _Clock())
    _live_web(store, web_service="web", web_port=3000)
    mgr.heal_corrupted_web()
    argv, service = envs["r1"].exec_calls[0]
    assert service == "forge"                 # probe runs from the worker
    assert argv[0] == "curl"
    assert any("http://web:3000/" in a for a in argv)   # web_service:web_port + health_path


def test_skips_and_resets_budget_when_healthy(tmp_path):
    clock = _Clock()
    mgr, store, envs = _mgr(tmp_path, clock)
    _live_web(store)
    env = mgr._env_for("r1")
    env.probe_status = "200"
    mgr._heal_state["r1"] = {"attempts": 1, "last": 900.0}   # a prior episode

    assert mgr.heal_corrupted_web() == []
    assert env.restart_calls == []
    assert "r1" not in mgr._heal_state          # recovered → budget reset


def test_skips_5xx_without_corruption_signature(tmp_path):
    mgr, store, envs = _mgr(tmp_path, _Clock())
    _live_web(store)
    env = mgr._env_for("r1")
    env.probe_status, env.log_text = "500", REAL_APP_ERROR_LOG

    assert mgr.heal_corrupted_web() == []
    assert env.restart_calls == []              # a real 500 is left alone


def test_skips_when_unreachable(tmp_path):
    mgr, store, envs = _mgr(tmp_path, _Clock())
    _live_web(store)
    env = mgr._env_for("r1")
    env.probe_status, env.probe_exit = "000", 7   # curl connection failure

    assert mgr.heal_corrupted_web() == []
    assert env.restart_calls == []


def test_respects_cooldown_and_max_attempts(tmp_path):
    clock = _Clock(1000.0)
    mgr, store, envs = _mgr(tmp_path, clock,
                            web_heal_max_attempts=2, web_heal_cooldown_secs=180)
    _live_web(store)
    env = mgr._env_for("r1")
    env.probe_status, env.log_text = "500", CORRUPT_LOG

    mgr.heal_corrupted_web()                     # attempt 1
    assert env.restart_calls == ["web"]

    clock.t = 1030.0                             # 30s later — within cooldown
    mgr.heal_corrupted_web()
    assert env.restart_calls == ["web"]          # not yet

    clock.t = 1000.0 + 181                        # cooldown elapsed
    mgr.heal_corrupted_web()                     # attempt 2
    assert env.restart_calls == ["web", "web"]

    clock.t += 1000                              # budget exhausted
    mgr.heal_corrupted_web()
    assert env.restart_calls == ["web", "web"]   # capped at max_attempts


def test_disabled_when_self_heal_off(tmp_path):
    mgr, store, envs = _mgr(tmp_path, _Clock(), self_heal=False)
    _live_web(store)
    env = mgr._env_for("r1")
    env.probe_status, env.log_text = "500", CORRUPT_LOG

    assert mgr.heal_corrupted_web() == []
    assert env.restart_calls == []


def test_emits_narration_event_on_heal(tmp_path):
    mgr, store, envs = _mgr(tmp_path, _Clock())
    _live_web(store)
    env = mgr._env_for("r1")
    env.probe_status, env.log_text = "500", CORRUPT_LOG

    mgr.heal_corrupted_web()
    kinds = [e["kind"] for e in mgr.bus.replay("r1")]
    assert "narration" in kinds


def test_heal_does_not_clobber_cached_env(tmp_path):
    # stop() cancels a running turn via self._envs[rid]; the reap-thread heal
    # probe must use a TRANSIENT env and never overwrite that slot.
    mgr, store, envs = _mgr(tmp_path, _Clock())
    _live_web(store)
    envs["r1"] = _FakeEnv("r1", [])          # factory singleton (healthy 200)
    sentinel = object()
    mgr._envs["r1"] = sentinel               # pretend a turn is streaming

    mgr.heal_corrupted_web()

    assert mgr._envs["r1"] is sentinel        # untouched by the heal probe


def test_worker_only_env_is_ignored(tmp_path):
    # A worker-only run (no web_service) has nothing to probe or heal.
    mgr, store, envs = _mgr(tmp_path, _Clock())
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service=None)

    assert mgr.heal_corrupted_web() == []
    assert envs == {}                            # never even built an env
