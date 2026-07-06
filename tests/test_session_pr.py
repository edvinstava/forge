"""_finish_pr composition: agent-authored title/body, task-derived fallbacks,
issue-key carrying, and lockfile-churn restore. The recording env captures the
exact argv sequence so the tests pin the git/gh contract, not just the result."""
import json
from pathlib import Path

from forge.config import Budget, Config
from forge.session import SessionManager
from forge.store import Store
from forge.verify import VerifyCmd, VerifyPlan


class RecordingEnv:
    def __init__(self):
        self.execs = []

    def exec(self, argv, service=None, workdir="/work", env=None):
        from forge.container import ExecResult
        self.execs.append(argv)
        joined = " ".join(argv)
        if "pr create" in joined or ("pr" in argv and "create" in argv):
            return ExecResult(0, "https://github.com/o/r/pull/7\n", "")
        return ExecResult(0, "", "")


class Host:
    def __init__(self):
        self.ran = []

    def read(self, ws, rel):
        p = Path(ws) / rel
        return p.read_text() if p.is_file() else None

    def exists(self, ws, rel):
        return (Path(ws) / rel).exists()

    def write_file(self, path, content):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)

    def run(self, argv, env=None):
        from forge.container import ExecResult
        self.ran.append(argv)
        return ExecResult(0, "", "")   # ls-files exit 0 → "tracked"


TASK = ("The offer table on the admin page is reflecting the project price, "
        "not the offer price. Issue number ABC-374")


def _mgr(tmp_path, task=TASK):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget())
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, Host(),
                         env_factory=lambda rid, files: RecordingEnv())
    store.create_run("r1", "o/r", task, "forge/fix-offer-abc123")
    mgr._verify_plans["r1"] = VerifyPlan(
        commands=[VerifyCmd("test", ["bash", "-lc", "true"])],
        has_real_verification=True)
    return mgr, store, cfg


def _write_pr_json(cfg, title, body):
    p = cfg.runs_dir / "r1" / "workspace" / ".forge" / "pr.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"title": title, "body": body}))


def _commit_msg(env):
    for argv in env.execs:
        if argv[:2] == ["git", "commit"]:
            return argv[argv.index("-m") + 1]
    raise AssertionError("no commit ran")


def _report_body(env):
    for argv in env.execs:
        if argv[0] == "bash" and "report.md" in argv[-1]:
            return argv[-1]
    raise AssertionError("no report.md written")


def test_finish_pr_uses_agent_authored_title_and_body(tmp_path):
    mgr, store, cfg = _mgr(tmp_path)
    _write_pr_json(cfg, "Show latest offer price in admin table (ABC-374)",
                   "## Summary\n\nUse total_price of the latest offer.")
    env = RecordingEnv()
    res = mgr._finish_pr("r1", env, verify_failed=[])
    assert res["ok"] and res["pr_url"].endswith("/pull/7")
    assert _commit_msg(env) == "Show latest offer price in admin table (ABC-374)"
    body = _report_body(env)
    assert "## Summary" in body and "Use total_price" in body
    assert "**Refs:** ABC-374" in body
    assert "Opened by forge" in body


def test_finish_pr_falls_back_to_task_title_with_issue_key(tmp_path):
    mgr, store, cfg = _mgr(tmp_path)   # no pr.json written
    env = RecordingEnv()
    res = mgr._finish_pr("r1", env, verify_failed=[])
    assert res["ok"]
    title = _commit_msg(env)
    assert title.startswith("The offer table on the admin page")
    assert "ABC-374" in title
    assert title != "forge: o/r"                      # the old generic title
    assert "## Task" in _report_body(env)             # body quotes the task


def test_finish_pr_restores_lockfile_churn_before_committing(tmp_path):
    mgr, store, cfg = _mgr(tmp_path)
    env = RecordingEnv()
    mgr._finish_pr("r1", env, verify_failed=[])
    joined = [" ".join(a) for a in env.execs]
    lock_idx = next(i for i, j in enumerate(joined) if "bun.lock" in j)
    add_idx = next(i for i, j in enumerate(joined) if j == "git add -A")
    assert lock_idx < add_idx


def test_finish_pr_refreshes_scratch_exclude_before_commit(tmp_path):
    """A warm env can outlive the forge that provisioned it (its exclude file
    predates patterns added since, e.g. .forge/live/). _finish_pr must refresh
    .git/info/exclude before `git add -A` so new scratch can't ride into the PR."""
    mgr, store, cfg = _mgr(tmp_path)
    env = RecordingEnv()
    res = mgr._finish_pr("r1", env, verify_failed=[])
    assert res["ok"]
    exclude = cfg.runs_dir / "r1" / "workspace" / ".git" / "info" / "exclude"
    assert exclude.is_file(), "exclude file was not refreshed before commit"
    assert ".forge/*" in exclude.read_text()


def test_finish_pr_draft_warning_leads_the_body(tmp_path):
    mgr, store, cfg = _mgr(tmp_path)
    env = RecordingEnv()
    res = mgr._finish_pr("r1", env, verify_failed=["ts:check"])
    assert res["draft"] is True
    body = _report_body(env)
    assert body.index("⚠️") < body.index("## Task")
    assert "ts:check" in body


def test_stale_pr_json_is_cleared_at_new_task_but_not_by_artifact_reset(tmp_path):
    mgr, store, cfg = _mgr(tmp_path)
    _write_pr_json(cfg, "old title", "old body")
    # Artifact reset runs inside self-review/open_pr — it must NOT eat the
    # description the task turn just wrote.
    mgr._reset_artifacts("r1")
    assert mgr._read_pr_meta("r1")["title"] == "old title"
    mgr._reset_pr_meta("r1")
    assert mgr._read_pr_meta("r1") == {"title": None, "body": None}


def test_secrets_follow_the_active_provider(tmp_path, monkeypatch):
    # Only the active provider's credential enters the container; the other
    # token rides along empty (keeps compose interpolation quiet).
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nocodex"))
    mgr, store, cfg = _mgr(tmp_path)
    s = mgr._secrets()
    assert s["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    assert s["OPENAI_API_KEY"] == ""

    codex_cfg = Config(runs_dir=tmp_path / "runs2", provider="codex",
                       oauth_token="t", openai_api_key="ok", gh_token="g",
                       budget=Budget())
    codex_store = Store(codex_cfg.runs_dir / "forge.db")
    codex_mgr = SessionManager(codex_cfg, codex_store, Host(),
                               env_factory=lambda rid, files: RecordingEnv())
    s2 = codex_mgr._secrets()
    assert s2["OPENAI_API_KEY"] == "ok"          # no plan login → API key
    assert s2["CLAUDE_CODE_OAUTH_TOKEN"] == ""   # claude token stays out


def test_finish_pr_strips_next_patch_for_commit_and_reapplies(tmp_path):
    # Forge's Next origin patch must never ship in a PR — but a legitimate
    # agent edit to the config must (the old skip-worktree approach silently
    # dropped it). The patch is stripped + unhidden around the commit, then
    # re-applied so the live dev server keeps working.
    from forge import nextdev
    mgr, store, cfg = _mgr(tmp_path)
    ws = cfg.runs_dir / "r1" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    agent_edit = 'module.exports = { images: { domains: ["cdn.x"] } };\n'
    (ws / "next.config.js").write_text(nextdev.inject(agent_edit))
    env = RecordingEnv()
    assert mgr._finish_pr("r1", env, verify_failed=[])["ok"]
    # unhidden for the commit…
    assert any("--no-skip-worktree" in a for a in mgr.host.ran)
    # …and re-patched afterwards (skip-worktree re-asserted) so HMR keeps working.
    final = (ws / "next.config.js").read_text()
    assert "cdn.x" in final and "forge:" in final
    assert any("--skip-worktree" in a and "--no-skip-worktree" not in a
               for a in mgr.host.ran)


def test_session_id_is_scoped_to_the_provider_that_minted_it(tmp_path):
    # `codex exec resume <claude-session-uuid>` fails every turn — after a
    # FORGE_PROVIDER switch the stored id must be ignored, not resumed.
    mgr, store, cfg = _mgr(tmp_path)
    mgr._persist_session_id("r1", "sess-claude-1")
    assert mgr._session_id("r1") == "sess-claude-1"

    codex_cfg = Config(runs_dir=cfg.runs_dir, provider="codex",
                       oauth_token="t", openai_api_key="ok", gh_token="g",
                       budget=Budget())
    codex_mgr = SessionManager(codex_cfg, store, Host(),
                               env_factory=lambda rid, files: RecordingEnv())
    assert codex_mgr._session_id("r1") is None      # claude id ≠ codex resume
    codex_mgr._persist_session_id("r1", "thread-9")
    assert codex_mgr._session_id("r1") == "thread-9"
    assert mgr._session_id("r1") is None            # and vice versa


def test_legacy_rows_without_provider_are_claudes(tmp_path):
    mgr, store, cfg = _mgr(tmp_path)
    store.set_session_fields("r1", claude_session_id="old-sess")  # pre-upgrade row
    assert mgr._session_id("r1") == "old-sess"
