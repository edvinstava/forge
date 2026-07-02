import json
from pathlib import Path

from forge.compose_orchestrator import ComposeOrchestrator
from forge.config import Budget, Config
from forge.container import ExecResult
from forge.store import Store

WORKER_OK = json.dumps({"subtype": "success", "is_error": False, "num_turns": 1,
                        "duration_ms": 10, "total_cost_usd": 0.01,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "session_id": "s", "result": "done"})


class FakeHost:
    def __init__(self, files, clone_rc=0):
        self.files = files
        self.clone_rc = clone_rc
        self.host_runs = []
        self.writes = []

    def clone(self, repo, branch, dest, gh_token):
        return ExecResult(self.clone_rc, "", "boom" if self.clone_rc else "")

    def read(self, dest, rel):
        return self.files.get(rel)

    def exists(self, dest, rel):
        return rel in self.files

    def write_file(self, path, content):
        self.writes.append((path, content))

    def run(self, argv, env=None):
        self.host_runs.append(list(argv))
        return ExecResult(0, "", "")


class FakeEnv:
    def __init__(self, handlers, host_port=None):
        self.handlers = handlers
        self.calls = []
        self.upped = False
        self.downed = False
        self.host_port = host_port

    def up(self, env=None):
        self.upped = True

    def exec(self, argv, workdir="/work", service=None):
        self.calls.append(list(argv))
        for pred, res in self.handlers:
            if pred(argv):
                return res() if callable(res) else res
        return ExecResult(0, "", "")

    def port(self, service, container_port):
        return self.host_port

    def down(self):
        self.downed = True


_PLAN_OBJ = {"goal": "do x", "steps": [{"id": 1, "intent": "implement"}],
             "acceptance": ["works"], "open_questions": [], "risk": "low"}

_PLAN_WORKER_OK = json.dumps({"subtype": "success", "is_error": False, "num_turns": 1,
                              "duration_ms": 5, "total_cost_usd": 0.005,
                              "usage": {"input_tokens": 1, "output_tokens": 1},
                              "session_id": "psid", "result": "planned"})


class PlanWritingEnv(FakeEnv):
    """Like FakeEnv, but when exec sees a planning worker call (prompt contains
    'You are planning'), it writes a canned .forge/plan.json into the workspace
    before returning the worker result — mirroring how a real planning turn works."""

    def __init__(self, handlers, host_port, ws_path):
        super().__init__(handlers, host_port)
        self._ws = ws_path

    def exec(self, argv, workdir="/work", service=None):
        # Detect a planning worker call: claude -p <prompt that starts with planning text>
        if (len(argv) >= 3 and argv[0] == "claude" and argv[1] == "-p"
                and "You are planning" in argv[2]):
            forge_dir = Path(self._ws) / ".forge"
            forge_dir.mkdir(parents=True, exist_ok=True)
            (forge_dir / "plan.json").write_text(json.dumps(_PLAN_OBJ))
            return ExecResult(0, _PLAN_WORKER_OK, "")
        return super().exec(argv, workdir, service)


def _cfg(tmp):
    return Config(runs_dir=tmp, oauth_token="tok", gh_token="gh",
                  budget=Budget(max_iterations=3, max_wall_secs=9999))


def _has(argv, *needles):
    s = " ".join(argv)
    return all(n in s for n in needles)


def _orch(tmp_path, files, handlers, host_port=None):
    host = FakeHost(files)
    env = FakeEnv(handlers, host_port=host_port)
    store = Store(tmp_path / "db")
    o = ComposeOrchestrator(_cfg(tmp_path), store, host,
                            env_factory=lambda rid, f: env)
    return o, store, host, env


def test_node_web_run_registers_url(tmp_path):
    files = {"package.json": json.dumps({"scripts": {"dev": "next dev", "test": "t"}})}
    handlers = [
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/21", "")),
    ]
    o, store, host, env = _orch(tmp_path, files, handlers, host_port=5099)
    out = o.run("a/b", "fix", "cr1")
    assert out.state == "done" and out.draft is False
    assert out.web_url == "http://localhost:5099"
    assert store.get_env("cr1")["state"] == "live"
    assert env.upped and not env.downed       # provisioned + kept warm


def test_node_web_run_applies_resource_caps(tmp_path):
    # `forge run` must cap the dev server's memory exactly like the web/Slack
    # SessionManager path — otherwise a leaky `next dev` on the primary CLI path
    # can balloon and starve the host (the very leak the cap commit addressed).
    files = {"package.json": json.dumps({"scripts": {"dev": "next dev", "test": "t"}})}
    handlers = [
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/30", "")),
    ]
    o, store, host, env = _orch(tmp_path, files, handlers, host_port=5099)
    o.run("a/b", "fix", "cr_cap")
    compose_json = next(c for p, c in host.writes if p.endswith("forge-compose.yml"))
    web = json.loads(compose_json)["services"]["web"]
    assert web.get("mem_limit")                         # cgroup backstop applied
    opts = web["environment"]["NODE_OPTIONS"] if isinstance(
        web["environment"], dict) else next(
        e for e in web["environment"] if e.startswith("NODE_OPTIONS="))
    assert "--max-old-space-size" in opts


def test_none_recipe_opens_pr_without_url(tmp_path):
    files = {"package.json": json.dumps({"scripts": {"test": "t"}})}   # no dev/start
    handlers = [
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/22", "")),
    ]
    o, store, host, env = _orch(tmp_path, files, handlers)
    out = o.run("a/b", "fix", "cr2")
    assert out.state == "done"
    assert out.web_url is None                 # no web service in 'none' recipe


def test_verify_failure_then_pass(tmp_path):
    files = {"package.json": json.dumps({"scripts": {"dev": "next dev", "test": "t"}})}
    n = {"i": 0}

    def verify():
        n["i"] += 1
        return ExecResult(0, "ok", "") if n["i"] > 1 else ExecResult(1, "FAIL", "")

    handlers = [
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], verify),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/23", "")),
    ]
    o, store, host, env = _orch(tmp_path, files, handlers, host_port=5099)
    out = o.run("a/b", "fix", "cr3")
    assert out.state == "done" and out.draft is False


def test_clone_failure(tmp_path):
    host = FakeHost({}, clone_rc=1)
    env = FakeEnv([])
    store = Store(tmp_path / "db")
    o = ComposeOrchestrator(_cfg(tmp_path), store, host, env_factory=lambda r, f: env)
    out = o.run("a/b", "fix", "cr4")
    assert out.state == "failed" and out.reason == "clone_failed"
    assert not env.upped


def test_chap_frontend_routes_to_dhis2_chap(tmp_path):
    files = {"d2.config.js": "export const config = { id: 'a29851f9-xxxx' }"}
    o, store, host, env = _orch(tmp_path, files, [
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: _has(a, "git", "status"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/24", "")),
    ], host_port=8080)
    out = o.run("dhis2-chap/chap-frontend", "fix UI", "cr5")
    # recipe detected as dhis2-chap → frontend web service on 3000 → URL registered
    assert store.get_env("cr5")["web_port"] == 3000
    assert out.web_url == "http://localhost:8080"


def test_format_fix_runs_before_verify(tmp_path):
    """The repo's deterministic formatter runs before the read-only checks so
    style-only diffs never block the PR (kills the prettier-fails-CI class)."""
    files = {"package.json": json.dumps(
        {"scripts": {"dev": "next dev", "format": "prettier --write .", "test": "t"}})}
    handlers = [
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/40", "")),
    ]
    o, store, host, env = _orch(tmp_path, files, handlers, host_port=5099)
    o.run("a/b", "fix", "crfmt")
    fmt, test = ["npm", "run", "format"], ["npm", "test"]
    assert fmt in env.calls
    assert env.calls.index(fmt) < env.calls.index(test)


def test_run_real_plan_gate_rejects_after_provision(tmp_path):
    # A policy that gates + an approve() returning False should stop AFTER the env
    # is up and a real plan was read — not from a {repo,task} stub.
    from forge import flow
    files = {"package.json": json.dumps({"scripts": {"dev": "next dev", "test": "t"}})}
    # _cfg sets runs_dir=tmp_path; the gate reads plan.json from runs_dir/r1/workspace/.forge/
    ws_path = str(Path(tmp_path) / "r1" / "workspace")
    # PlanWritingEnv writes plan.json on the planning turn; no task-worker handlers
    # needed because the gate rejects before the build loop runs.
    env = PlanWritingEnv(handlers=[], host_port=None, ws_path=ws_path)
    store = Store(tmp_path / "db")
    host = FakeHost(files)
    o = ComposeOrchestrator(_cfg(tmp_path), store, host,
                            env_factory=lambda rid, f: env)

    seen = {}

    def approve(plan):
        seen["plan"] = plan
        return False

    out = o.run("o/r", "do x", "r1",
                policy=flow.CheckpointPolicy.for_cli(auto=False), approve=approve)
    assert out.state == "stopped_plan"
    assert "goal" in seen["plan"]            # a REAL plan dict, not {"repo","task"}
