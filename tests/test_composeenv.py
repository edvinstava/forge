import sys

import pytest

from forge.composeenv import ComposeEnv


def test_up_times_out_tears_down_and_raises(monkeypatch):
    # A stalled image pull/build must not hang the provisioning thread forever:
    # `up` is capped, and on timeout it tears the half-started project down.
    from forge import composeenv
    env = ComposeEnv("rid", [], up_timeout=0.3)
    monkeypatch.setattr(
        composeenv.compose, "up_cmd",
        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(10)"])
    down_calls = []
    monkeypatch.setattr(
        composeenv.compose, "down_cmd",
        lambda *a, **k: (down_calls.append(1) or [sys.executable, "-c", ""]))
    with pytest.raises(RuntimeError, match="timed out"):
        env.up()
    assert down_calls, "timeout must tear down the partial project"


def test_up_no_timeout_when_unset(monkeypatch):
    # Default (no cap) keeps prior behavior: a fast `up` returns cleanly.
    from forge import composeenv
    env = ComposeEnv("rid", [])           # up_timeout=None
    monkeypatch.setattr(composeenv.compose, "up_cmd",
                        lambda *a, **k: [sys.executable, "-c", ""])
    env.up()                              # must not raise


def test_down_is_bounded_by_timeout(monkeypatch):
    # down() is the teardown the up()-timeout handler calls; against a hung docker
    # daemon an un-timed `compose down` would block forever, defeating the cap.
    from forge import composeenv
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw)
        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    ComposeEnv("rid", [], down_timeout=12.0).down()
    assert captured.get("timeout") == 12.0


def test_down_swallows_timeout(monkeypatch):
    # Teardown is best-effort; a timeout must not propagate (callers like sleep()
    # don't expect down() to raise).
    import subprocess
    from forge import composeenv

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    ComposeEnv("rid", []).down()          # must not raise


def test_stop_is_bounded_and_best_effort(monkeypatch):
    from forge import composeenv
    from forge.composeenv import ComposeEnv
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"], captured["timeout"] = cmd, kw.get("timeout")
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    ComposeEnv("rid", [], down_timeout=12.0).stop()
    assert "stop" in captured["cmd"] and captured["timeout"] == 12.0


def test_stop_swallows_timeout(monkeypatch):
    import subprocess
    from forge import composeenv
    from forge.composeenv import ComposeEnv
    monkeypatch.setattr(composeenv.subprocess, "run",
                        lambda c, **k: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(c, k.get("timeout"))))
    ComposeEnv("rid", []).stop()          # must NOT raise


def test_exec_env_is_name_only_in_argv_value_via_process_env(monkeypatch):
    # exec(env=...) must keep the secret value OUT of argv (only `-e KEY`
    # forwarding) and hand it to the subprocess environment instead.
    from forge import composeenv
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"], captured["env"] = cmd, kw.get("env")
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    ComposeEnv("rid", []).exec(["git", "push"], env={"GH_TOKEN": "sekrit"})
    assert ["-e", "GH_TOKEN"] == captured["cmd"][
        captured["cmd"].index("-e"):captured["cmd"].index("-e") + 2]
    assert "sekrit" not in " ".join(captured["cmd"])
    assert captured["env"]["GH_TOKEN"] == "sekrit"


def test_exec_without_env_keeps_prior_shape(monkeypatch):
    from forge import composeenv
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"], captured["env"] = cmd, kw.get("env")
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    ComposeEnv("rid", []).exec(["ls"])
    assert "-e" not in captured["cmd"]
    assert captured["env"] is None        # inherit the parent env untouched


def test_start_raises_on_failure(monkeypatch):
    import pytest
    from forge import composeenv
    from forge.composeenv import ComposeEnv
    def fake_run(cmd, **kw):
        class R: returncode, stdout, stderr = 1, "", "no such project"
        return R()
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        ComposeEnv("rid", []).start()


def test_restart_targets_single_service(monkeypatch):
    # Self-heal restarts only the web service; the worker + supabase stay up.
    from forge import composeenv
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"], captured["timeout"] = cmd, kw.get("timeout")
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(composeenv.subprocess, "run", fake_run)
    ComposeEnv("rid", [], down_timeout=12.0).restart("web")
    assert captured["cmd"][-2:] == ["restart", "web"]
    assert captured["timeout"] == 12.0          # bounded like stop/start


def test_restart_raises_on_failure(monkeypatch):
    from forge import composeenv
    from forge.composeenv import ComposeEnv
    monkeypatch.setattr(composeenv.subprocess, "run",
                        lambda c, **k: type("R", (), {
                            "returncode": 1, "stdout": "", "stderr": "boom"})())
    with pytest.raises(RuntimeError):
        ComposeEnv("rid", []).restart("web")
