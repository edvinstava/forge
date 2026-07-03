import json
from pathlib import Path
from types import SimpleNamespace

from forge.slackqa import qa_dir, needs_clone, answer_question


def _cfg(tmp_path):
    return SimpleNamespace(runs_dir=tmp_path, gh_token="t", oauth_token="o",
                           repo_cache_ttl_secs=3600, image_tag="forge-worker",
                           provider="claude", codex_auth="auto", openai_api_key="")


def test_qa_dir_is_under_cache_qa(tmp_path):
    d = qa_dir(_cfg(tmp_path), "acme/webapp")
    assert d == tmp_path / "cache" / "qa" / "acme-webapp"


def test_needs_clone_when_missing(tmp_path):
    assert needs_clone(tmp_path / "nope", 3600, lambda: 100.0) is True


def test_needs_clone_false_when_fresh(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / ".forge-qa-ts").write_text("100.0")
    assert needs_clone(d, 3600, lambda: 200.0) is False


def test_needs_clone_true_when_stale(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / ".forge-qa-ts").write_text("100.0")
    assert needs_clone(d, 50, lambda: 200.0) is True


OK = json.dumps({"subtype": "success", "is_error": False,
                 "result": "You're on 2.41.3.", "session_id": "s"})


class FakeRun:
    """Records argv; returns a fake CompletedProcess. Creates the clone dir on a
    `gh repo clone` so the marker can be written, like a real clone would."""
    def __init__(self, claude_stdout=OK, clone_rc=0):
        self.calls = []
        self._claude = claude_stdout
        self._clone_rc = clone_rc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[:3] == ["gh", "repo", "clone"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=self._clone_rc, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout=self._claude, stderr="")


def _docker_calls(run):
    return [c for c in run.calls if c[0][0] == "docker"]


def test_answer_clones_then_asks(tmp_path):
    run = FakeRun()
    out = answer_question(_cfg(tmp_path), "acme/x", "what version?",
                          run=run, clock=lambda: 100.0)
    assert out == "You're on 2.41.3."
    assert any(c[0][:3] == ["gh", "repo", "clone"] for c in run.calls)
    assert len(_docker_calls(run)) == 1


def test_answer_runs_agent_in_disposable_readonly_container(tmp_path):
    cfg = _cfg(tmp_path)
    run = FakeRun()
    answer_question(cfg, "acme/x", "what version?", run=run, clock=lambda: 100.0)
    argv, kw = _docker_calls(run)[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    d = qa_dir(cfg, "acme/x")
    assert f"{d}:/work:ro" in argv                 # clone mounted read-only
    assert "forge-worker" in argv                  # cfg.image_tag
    i = argv.index("--entrypoint")                 # image ENTRYPOINT is sleep;
    assert argv[i + 1] == "claude"                 # run the agent CLI instead
    assert any("what version?" in a for a in argv)  # prompt reaches the CLI
    assert "cwd" not in kw                         # workdir lives in the container


def test_answer_keeps_secrets_out_of_argv(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.oauth_token = "sekrit-tok"
    run = FakeRun()
    answer_question(cfg, "acme/x", "q", run=run, clock=lambda: 100.0)
    argv, kw = _docker_calls(run)[0]
    assert "sekrit-tok" not in " ".join(argv)      # never in argv (ps-visible)
    i = argv.index("-e")
    assert argv[i + 1] == "CLAUDE_CODE_OAUTH_TOKEN"    # name-only flag
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sekrit-tok"  # value via env


def test_answer_mounts_codex_plan_auth(tmp_path, monkeypatch):
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    cfg = _cfg(tmp_path)
    cfg.provider = "codex"
    run = FakeRun(claude_stdout=json.dumps(
        {"type": "turn.completed", "usage": {}}))
    answer_question(cfg, "acme/x", "q", run=run, clock=lambda: 100.0)
    argv, kw = _docker_calls(run)[0]
    assert f"{home}:/home/forge/.codex" in argv    # plan auth rides the mount
    i = argv.index("--entrypoint")
    assert argv[i + 1] == "codex"
    assert kw["env"]["OPENAI_API_KEY"] == ""       # suppressed → bill the plan


def test_answer_reuses_fresh_clone(tmp_path):
    d = qa_dir(_cfg(tmp_path), "acme/x")
    d.mkdir(parents=True)
    (d / ".forge-qa-ts").write_text("100.0")
    run = FakeRun()
    answer_question(_cfg(tmp_path), "acme/x", "q", run=run, clock=lambda: 120.0)
    assert not any(c[0][:3] == ["gh", "repo", "clone"] for c in run.calls)


def test_answer_reports_clone_failure(tmp_path):
    run = FakeRun(clone_rc=1)
    out = answer_question(_cfg(tmp_path), "acme/x", "q", run=run, clock=lambda: 1.0)
    assert "couldn't" in out.lower()


def test_answer_rejects_malicious_slug_without_running_anything(tmp_path):
    run = FakeRun()
    out = answer_question(_cfg(tmp_path), "--upload-pack=evil/x", "q",
                          run=run, clock=lambda: 1.0)
    assert "doesn't look like a repo" in out
    assert run.calls == []                       # nothing ever shelled out
