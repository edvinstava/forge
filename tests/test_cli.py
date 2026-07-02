import pytest
from forge.cli import main
from forge.store import Store


def test_status_lists_envs(tmp_path, capsys):
    s = Store(tmp_path / "forge.db")
    s.create_env("r1", "forge-r1", "http://localhost:5051", 5051, "live")
    rc = main(["status", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "r1" in out and "live" in out and "5051" in out


def test_status_empty(tmp_path, capsys):
    rc = main(["status", "--runs-dir", str(tmp_path)])
    assert rc == 0 and "no environments" in capsys.readouterr().out


def test_down_reaps(tmp_path, capsys, monkeypatch):
    # down tears the compose project down (no-op here) and marks the env reaped
    monkeypatch.setattr("forge.lifecycle.compose_down_project", lambda project: None)
    Store(tmp_path / "forge.db").create_env("r1", "forge-r1", "u", 1, "live")
    rc = main(["down", "r1", "--runs-dir", str(tmp_path)])
    assert rc == 0
    assert Store(tmp_path / "forge.db").get_env("r1")["state"] == "reaped"
    assert "reaped r1" in capsys.readouterr().out


def test_bake_unknown_template_errors(tmp_path, capsys):
    rc = main(["bake", "whatever", "--runs-dir", str(tmp_path)])
    assert rc == 1 and "unknown bake template" in capsys.readouterr().err


def test_missing_tokens_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    rc = main(["run", "a/b", "do x", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "token" in capsys.readouterr().err.lower()


def test_bad_repo_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    rc = main(["run", "notarepo", "do x", "--runs-dir", str(tmp_path)])
    assert rc == 1


def test_web_subcommand_parses(monkeypatch):
    import forge.cli as cli
    called = {}
    def fake_web(args):
        called["host"], called["port"] = args.host, args.port
        return 0
    monkeypatch.setattr(cli, "_cmd_web", fake_web)
    # re-register parser path: main dispatches to func; ensure 'web' is wired
    rc = cli.main(["web", "--port", "9090", "--host", "127.0.0.1", "--runs-dir", "runs"])
    assert rc == 0 and called["port"] == 9090


def test_web_slack_requires_tokens(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("GH_TOKEN", "y")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_ALLOWED_USER", raising=False)
    rc = main(["web", "--slack", "--no-open", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "SLACK_" in capsys.readouterr().err


def test_web_slack_reject_reload(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("GH_TOKEN", "y")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp")
    monkeypatch.setenv("SLACK_ALLOWED_USER", "U1")
    rc = main(["web", "--slack", "--reload", "--no-open", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "reload" in capsys.readouterr().err.lower()


def test_run_parses_yes_flag(monkeypatch, tmp_path):
    import forge.cli as cli
    captured = {}

    def fake_run(self, repo, task, run_id, **kw):
        from forge.compose_orchestrator import RunOutcome
        captured["auto"] = kw.get("policy")
        return RunOutcome("done", "https://x/pull/1", False, "", None)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    monkeypatch.setattr("forge.compose_orchestrator.ComposeOrchestrator.run", fake_run)
    rc = cli.main(["run", "o/r", "do x", "--yes", "--runs-dir", str(tmp_path)])
    assert rc == 0
    assert captured["auto"] is not None and not captured["auto"].gates("plan_approval")


def test_attach_no_checkpoint_prints_state(tmp_path, capsys):
    from forge.cli import main
    from forge.store import Store
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "o/r", "do x", "forge/x")
    s.set_lifecycle_state("r1", "executing")
    rc = main(["attach", "r1", "--runs-dir", str(tmp_path)])
    assert rc == 0
    assert "executing" in capsys.readouterr().out


def test_attach_with_open_checkpoint_approves(tmp_path, monkeypatch, capsys):
    from forge.store import Store
    from forge.session import SessionManager
    from types import SimpleNamespace
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "o/r", "do x", "forge/x")
    s.create_checkpoint("r1", "plan_approval", {"goal": "do x"})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    calls = {}
    def fake_respond(self, run_id, cid, action, body=None, model="auto"):
        calls["action"] = action
        yield SimpleNamespace(kind="done", data={"message": "approved"})
    monkeypatch.setattr(SessionManager, "respond_checkpoint", fake_respond)
    rc = main(["attach", "r1", "--runs-dir", str(tmp_path)])
    assert rc == 0
    assert calls["action"] == "approve"


def test_review_subcommand_dispatch(monkeypatch, tmp_path):
    from forge import cli
    from types import SimpleNamespace
    captured = {}

    class FakeMgr:
        def review(self, run_id, pr_ref, model="auto"):
            captured["pr_ref"] = pr_ref
            yield SimpleNamespace(kind="phase", data={"label": "Checking out"})
            yield SimpleNamespace(kind="review", data={
                "ok": True, "review_url": "https://github.com/o/r/pull/3#x",
                "comments": 2, "dropped": 0, "degraded": False})

    monkeypatch.setattr("forge.session.SessionManager",
                        lambda *a, **k: FakeMgr())
    monkeypatch.setattr(cli.Config, "from_env",
                        classmethod(lambda cls, rd: cls(runs_dir=rd, oauth_token="t",
                                                        gh_token="g")))
    monkeypatch.setattr(cli, "_populate_identity", lambda cfg: None)
    monkeypatch.setattr(cli, "Store", lambda *a, **k: object())
    monkeypatch.setattr(cli, "LocalHost", lambda *a, **k: object())
    rc = cli.main(["review", "o/r#3", "--runs-dir", str(tmp_path)])
    assert rc == 0
    assert captured["pr_ref"] == "o/r#3"


def test_slack_missing_token_message_points_to_config_file(tmp_path, monkeypatch, capsys):
    import argparse
    from forge import cli

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_ALLOWED_USER", raising=False)
    monkeypatch.setattr(cli, "_populate_identity", lambda cfg: None)

    args = argparse.Namespace(runs_dir=str(tmp_path), slack=True, reload=False)
    rc = cli._cmd_web(args)

    assert rc == 1
    assert "config.env" in capsys.readouterr().err


def test_web_github_requires_app_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    monkeypatch.delenv("FORGE_GH_APP_ID", raising=False)
    monkeypatch.delenv("FORGE_GH_APP_KEY", raising=False)
    rc = main(["web", "--github", "--no-open", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "FORGE_GH_APP_ID" in capsys.readouterr().err


def test_web_github_rejects_reload(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    rc = main(["web", "--github", "--reload", "--no-open",
               "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "reload" in capsys.readouterr().err.lower()


def test_web_github_rejects_schemeless_public_url(monkeypatch, capsys, tmp_path):
    # urlparse("host.com").hostname is None — attaching the gate with an empty
    # public host would silently fail OPEN (whole API exposed via the user's
    # ingress). Boot must refuse instead.
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)  # never serve here
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    key = tmp_path / "key.pem"
    key.write_text("PEM")
    monkeypatch.setenv("FORGE_GH_APP_ID", "123")
    monkeypatch.setenv("FORGE_GH_APP_KEY", str(key))
    monkeypatch.setenv("FORGE_GH_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("FORGE_PUBLIC_URL", "forge.example.com")
    rc = main(["web", "--github", "--no-open", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "FORGE_PUBLIC_URL" in capsys.readouterr().err
