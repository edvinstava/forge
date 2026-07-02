import sys
from forge.composeenv import ComposeEnv


def test_exec_stream_yields_lines(monkeypatch):
    env = ComposeEnv("rid", [])
    # bypass docker: make exec_cmd a local python one-liner printing 3 lines
    from forge import composeenv
    script = "import sys,time\n[print(f'line{i}') or sys.stdout.flush() for i in range(3)]"
    monkeypatch.setattr(composeenv.compose, "exec_cmd",
                        lambda *a, **k: [sys.executable, "-c", script])
    lines = list(env.exec_stream(["ignored"]))
    assert lines == ["line0", "line1", "line2"]
    assert env._proc is None   # cleared after exhaustion


def test_exec_stream_early_abandon_cleans_up(monkeypatch):
    import sys
    env = ComposeEnv("rid", [])
    from forge import composeenv
    script = "import sys,time\nprint('first'); sys.stdout.flush(); time.sleep(30)"
    monkeypatch.setattr(composeenv.compose, "exec_cmd",
                        lambda *a, **k: [sys.executable, "-c", script])
    gen = env.exec_stream(["ignored"])
    assert next(gen) == "first"
    proc = env._proc
    gen.close()                       # triggers GeneratorExit → finally
    assert env._proc is None
    assert proc.poll() is not None    # child was reaped, not left running
