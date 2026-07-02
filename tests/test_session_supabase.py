import json
from pathlib import Path

from forge.config import Budget, Config
from forge.session import SessionManager
from forge.store import Store
from forge.supaports import SupabaseAllocator
from test_session import FakeEnv

CONFIG = """\
project_id = "webapp"

[api]
port = 54321

[db]
port = 54322
shadow_port = 54320

[studio]
port = 54323

[analytics]
port = 54327
"""


class SupaHost:
    """A host whose clone yields a Next + Supabase repo; records host.run calls."""

    def __init__(self):
        self.runs = []

    def clone(self, repo, branch, ws, token):
        from forge.container import ExecResult
        p = Path(ws)
        (p / "supabase").mkdir(parents=True, exist_ok=True)
        (p / "package.json").write_text(
            '{"dependencies":{"next":"14"},"scripts":{"dev":"next dev","test":"jest"}}')
        (p / "supabase" / "config.toml").write_text(CONFIG)
        return ExecResult(0, "", "")

    def read(self, ws, rel):
        f = Path(ws) / rel
        return f.read_text() if f.is_file() else None

    def exists(self, ws, rel):
        return (Path(ws) / rel).exists()

    def write_file(self, path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)

    def run(self, argv, env=None):
        from forge.container import ExecResult
        self.runs.append(argv)
        return ExecResult(0, "", "")


def _mgr(tmp_path, is_free=lambda p: True):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    host = SupaHost()
    mgr = SessionManager(cfg, store, host,
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    mgr.alloc = SupabaseAllocator(store, is_free=is_free, stride=100)
    return mgr, store, host


def test_start_reserves_block_and_rewrites_cloned_config(tmp_path):
    mgr, store, host = _mgr(tmp_path)
    list(mgr.start("r1", "o/webapp", "github"))

    sup = store.get_supabase("r1")
    assert sup["offset"] == 100
    assert sup["project"] == "webapp-r1"

    cfg_text = host.read(str(tmp_path / "runs" / "r1" / "workspace"),
                         "supabase/config.toml")
    assert 'project_id = "webapp-r1"' in cfg_text
    assert "port = 54421" in cfg_text   # api shifted by offset
    assert "port = 54422" in cfg_text   # db shifted


def test_start_marks_config_skip_worktree(tmp_path):
    # the port-shifted config.toml must not leak into the worker's PR/diff
    mgr, store, host = _mgr(tmp_path)
    list(mgr.start("r1", "o/webapp", "github"))
    ws = str(tmp_path / "runs" / "r1" / "workspace")
    assert ["git", "-C", ws, "update-index", "--skip-worktree",
            "supabase/config.toml"] in host.runs


def test_start_bakes_offset_url_into_compose(tmp_path):
    mgr, store, host = _mgr(tmp_path)
    list(mgr.start("r1", "o/webapp", "github"))
    compose = json.loads((tmp_path / "runs" / "r1" / "forge-compose.yml").read_text())
    url = compose["services"]["web"]["environment"]["NEXT_PUBLIC_SUPABASE_URL"]
    assert url == "http://host.docker.internal:54421"


def test_concurrent_sessions_get_distinct_blocks(tmp_path):
    mgr, store, host = _mgr(tmp_path)
    list(mgr.start("r1", "o/webapp", "github"))
    list(mgr.start("r2", "o/webapp", "github"))
    assert store.get_supabase("r1")["offset"] != store.get_supabase("r2")["offset"]


def test_no_free_block_surfaces_error(tmp_path):
    mgr, store, host = _mgr(tmp_path, is_free=lambda p: False)
    mgr.alloc.max_blocks = 3
    events = list(mgr.start("r1", "o/webapp", "github"))
    assert events[-1].kind == "error" and events[-1].data["kind"] == "ports"
    assert store.get_supabase("r1") == {}  # nothing reserved


def test_end_stops_supabase_and_releases(tmp_path):
    mgr, store, host = _mgr(tmp_path)
    list(mgr.start("r1", "o/webapp", "github"))
    import forge.lifecycle as lc
    orig = lc.reap_project
    lc.reap_project = lambda store, run_id, **kw: None
    try:
        mgr.end("r1")
    finally:
        lc.reap_project = orig
    assert ["supabase", "stop", "--workdir",
            str(tmp_path / "runs" / "r1" / "workspace")] in host.runs
    assert store.get_supabase("r1") == {}


def test_failed_up_stops_supabase_and_releases_block(tmp_path):
    # supabase is started in host_pre; if compose `up` then fails, the host
    # Supabase stack must not be left orphaned (the bug behind a "Provisioning
    # error" session whose 13 supabase containers keep running in Docker).
    class FailingUpEnv(FakeEnv):
        def up(self, secrets):
            raise RuntimeError("compose up boom")

    mgr, store, host = _mgr(tmp_path)
    mgr.env_factory = lambda rid, files: FailingUpEnv(rid, files)
    events = list(mgr.start("r1", "o/webapp", "github"))

    assert events[-1].kind == "error" and events[-1].data["kind"] == "up"
    ws = str(tmp_path / "runs" / "r1" / "workspace")
    assert ["supabase", "stop", "--workdir", ws] in host.runs
    assert store.get_supabase("r1") == {}


def test_failed_health_stops_supabase_and_releases_block(tmp_path):
    # compose comes up but the app never turns healthy → same leak class.
    class UnhealthyEnv(FakeEnv):
        def exec(self, argv, service=None, workdir="/work"):
            from forge.container import ExecResult
            if "curl -fs" in " ".join(argv):       # the health poll
                return ExecResult(1, "", "health timeout")
            return super().exec(argv, service, workdir)

    mgr, store, host = _mgr(tmp_path)
    mgr.env_factory = lambda rid, files: UnhealthyEnv(rid, files)
    events = list(mgr.start("r1", "o/webapp", "github"))

    assert events[-1].kind == "error" and events[-1].data["kind"] == "health"
    ws = str(tmp_path / "runs" / "r1" / "workspace")
    assert ["supabase", "stop", "--workdir", ws] in host.runs
    assert store.get_supabase("r1") == {}


def test_end_without_supabase_does_not_stop(tmp_path):
    # node-web session (no supabase reservation) → no supabase stop on end
    from test_session import FakeHost
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    host = SupaHost()
    mgr = SessionManager(cfg, store, FakeHost(),
                         env_factory=lambda rid, files: FakeEnv(rid, files))
    list(mgr.start("r1", "o/r", "github"))
    import forge.lifecycle as lc
    orig = lc.reap_project
    lc.reap_project = lambda store, run_id, **kw: None
    try:
        mgr.end("r1")
    finally:
        lc.reap_project = orig
    # FakeHost has no `runs` list; assert no exception + no reservation
    assert store.get_supabase("r1") == {}
