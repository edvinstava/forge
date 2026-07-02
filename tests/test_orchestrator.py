import json
from forge.config import Config, Budget
from forge.store import Store
from forge.container import ExecResult
from forge.orchestrator import Orchestrator

PKG = json.dumps({"scripts": {"test": "node --test"}})
WORKER_OK = json.dumps({"subtype": "success", "is_error": False, "num_turns": 1,
                        "duration_ms": 10, "total_cost_usd": 0.01,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "session_id": "s", "result": "done"})


class FakeRunner:
    """Scriptable ContainerRunner. `handlers` is a list of (predicate(argv), ExecResult).
    The first matching handler wins. Return a callable to get dynamic results."""

    def __init__(self, handlers, host_port=None):
        self.handlers = handlers      # list of (predicate(argv), ExecResult or callable)
        self.calls = []
        self.detached = []
        self.start_envs = []
        self.publish_ports = []
        self.stopped = []
        self.host_port = host_port

    def start(self, run_id, env, publish_port=None):
        self.start_envs.append(dict(env))
        self.publish_ports.append(publish_port)
        assert "ANTHROPIC_API_KEY" not in env, "must NOT pass ANTHROPIC_API_KEY"
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN"), "must pass CLAUDE_CODE_OAUTH_TOKEN"
        return "cid"

    def exec(self, cid, argv, workdir="/work"):
        self.calls.append(list(argv))
        for pred, res in self.handlers:
            if pred(argv):
                return res() if callable(res) else res
        return ExecResult(0, "", "")

    def exec_detached(self, cid, argv, workdir="/work"):
        self.detached.append(list(argv))

    def port(self, cid, container_port):
        return self.host_port

    def stop(self, cid):
        self.stopped.append(cid)


def _cfg(tmp_path):
    return Config(runs_dir=tmp_path, oauth_token="tok", gh_token="gh",
                  budget=Budget(max_iterations=3, max_wall_secs=9999))


def _has(argv, *needles):
    s = " ".join(argv)
    return all(n in s for n in needles)


def test_success_after_one_verify_failure_opens_nondraft_pr(tmp_path):
    """verify fails once then passes → state 'done', draft=False, pr_url set"""
    verify_call_count = {"n": 0}

    def verify_res():
        verify_call_count["n"] += 1
        if verify_call_count["n"] <= 1:
            return ExecResult(1, "FAIL expected 5", "")
        return ExecResult(0, "ok", "")

    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "no file")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], verify_res),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f.js", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/7", "")),
    ]

    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "fix add", "run0001")

    assert out.state == "done"
    assert out.draft is False
    assert out.pr_url is not None and out.pr_url.endswith("/pull/7")


def test_start_receives_oauth_not_api_key(tmp_path):
    """start() receives CLAUDE_CODE_OAUTH_TOKEN and NOT ANTHROPIC_API_KEY"""
    runner = FakeRunner([
        (lambda a: _has(a, "cat", "package.json"),
         ExecResult(0, json.dumps({"scripts": {"start": "x"}}), "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/8", "")),
    ])
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), runner)
    o.run("a/b", "tweak readme", "run_tok")

    assert len(runner.start_envs) == 1
    env = runner.start_envs[0]
    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok"


def test_no_verification_opens_draft(tmp_path):
    """no real verification detected → draft=True, reason='no_verification'"""
    handlers = [
        (lambda a: _has(a, "cat", "package.json"),
         ExecResult(0, json.dumps({"scripts": {"start": "x"}}), "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/8", "")),
    ]
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "tweak readme", "run0002")

    assert out.draft is True
    assert out.reason == "no_verification"


def test_auth_error_stops_budget(tmp_path):
    """worker returns auth/usage error → state 'stopped_budget', reason 'usage', draft=True"""
    worker_auth_err = json.dumps({
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "usage limit reached",
        "num_turns": 1,
        "duration_ms": 5,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "session_id": "s-auth",
    })
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, worker_auth_err, "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/10", "")),
    ]
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "some task", "run_auth")

    assert out.state == "stopped_budget"
    assert out.reason == "usage"
    assert out.draft is True


def test_budget_exhaustion_stops_and_drafts(tmp_path):
    """verify always fails → loops until iteration cap (3) → state 'stopped_budget', reason 'iterations', draft=True"""
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(1, "still failing", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/9", "")),
    ]
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "hard task", "run0003")

    assert out.state == "stopped_budget"
    assert out.reason == "iterations"
    assert out.draft is True


def test_push_failure_marks_failed(tmp_path):
    """verify passes but git push fails → state 'failed', reason 'push_failed', no PR."""
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M add.js", "")),
        (lambda a: _has(a, "git", "push"), ExecResult(1, "", "remote: Permission denied")),
    ]
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "fix it", "runpush01")
    assert out.state == "failed"
    assert out.reason == "push_failed"
    assert out.pr_url is None


def test_app_started_and_url_registered(tmp_path):
    """repo with a dev server → app started, health passes, URL registered + kept warm."""
    pkg = json.dumps({"scripts": {"dev": "next dev", "test": "node --test"}})
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, pkg, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/11", "")),
    ]
    runner = FakeRunner(handlers, host_port=5051)
    store = Store(tmp_path / "db")
    out = Orchestrator(_cfg(tmp_path), store, runner).run("a/b", "fix", "runapp1")

    assert out.state == "done"
    assert out.web_url == "http://localhost:5051"
    assert store.get_env("runapp1")["state"] == "live"
    assert runner.publish_ports == [3000]          # web port published at start
    assert runner.detached                          # the dev server was started
    assert "forge-runapp1" not in runner.stopped    # container kept warm


def test_app_health_fail_still_opens_pr_without_url(tmp_path):
    """dev server never becomes healthy → no URL, env failed, but PR still opens."""
    pkg = json.dumps({"scripts": {"dev": "next dev", "test": "node --test"}})
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, pkg, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "curl", "localhost:3000"), ExecResult(1, "", "timeout")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/12", "")),
    ]
    runner = FakeRunner(handlers, host_port=5051)
    store = Store(tmp_path / "db")
    out = Orchestrator(_cfg(tmp_path), store, runner).run("a/b", "fix", "runapp2")

    assert out.state == "done"
    assert out.web_url is None
    assert store.get_env("runapp2")["state"] == "failed"


def test_concurrency_one_reaps_prior_env(tmp_path):
    """starting a run reaps any pre-existing live env (concurrency 1)."""
    store = Store(tmp_path / "db")
    store.create_env("old", "forge-old", "http://localhost:1", 3000, "live")
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/13", "")),
    ]
    runner = FakeRunner(handlers)
    Orchestrator(_cfg(tmp_path), store, runner).run("a/b", "fix", "new1")
    assert store.get_env("old")["state"] == "reaped"
    assert "forge-old" in runner.stopped


def test_format_fix_runs_before_verify(tmp_path):
    """Orchestrator runs the repo's formatter before verifying, so style-only
    diffs don't fail checks."""
    pkg = json.dumps({"scripts": {"format": "prettier --write .", "test": "node --test"}})
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, pkg, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(0, "ok", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/41", "")),
    ]
    runner = FakeRunner(handlers)
    Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), runner).run("a/b", "fix", "ofmt")
    fmt, test = ["npm", "run", "format"], ["npm", "test"]
    assert fmt in runner.calls
    assert runner.calls.index(fmt) < runner.calls.index(test)
