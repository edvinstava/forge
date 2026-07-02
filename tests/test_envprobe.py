from forge import envprobe
from forge.container import ExecResult


class FakeEnv:
    """Scripts the agent: `claude` exec 'succeeds', `cat` returns the overlay the
    agent supposedly wrote (or fails when overlay_yaml is None)."""
    def __init__(self, overlay_yaml, worker_ok=True):
        self.overlay_yaml, self.worker_ok = overlay_yaml, worker_ok
        self.calls = []

    def exec(self, argv, service=None, workdir="/work"):
        self.calls.append((argv, service))
        if argv[:1] == ["cat"]:
            return ExecResult(0 if self.overlay_yaml is not None else 1,
                              self.overlay_yaml or "", "")
        if argv[:1] == ["claude"]:
            return ExecResult(0 if self.worker_ok else 1,
                              '{"subtype":"success","is_error":false,"result":"done"}', "")
        return ExecResult(0, "", "")


def test_probe_returns_validated_overlay():
    env = FakeEnv("pkg_manager: bun\napt: [libnss3]\n")
    out = envprobe.probe(env, model=None, max_iterations=4)
    assert out["pkg_manager"] == "bun" and out["apt"] == ["libnss3"]
    assert any(a[:1] == ["claude"] and s == "forge" for a, s in env.calls)


def test_probe_returns_none_when_no_overlay_file():
    assert envprobe.probe(FakeEnv(None), model=None, max_iterations=4) is None


def test_probe_returns_none_on_invalid_overlay():
    assert envprobe.probe(FakeEnv("pkg_manager: cargo\n"),
                          model=None, max_iterations=4) is None


def test_repair_includes_failure_context_in_prompt():
    env = FakeEnv("apt: [libglib2.0-0]\n")
    out = envprobe.repair(env, "verify", "libglib-2.0.so.0: cannot open",
                          model=None, max_iterations=4)
    assert out["apt"] == ["libglib2.0-0"]
    prompt = next(a[2] for a, s in env.calls if a[:1] == ["claude"])
    assert "verify" in prompt and "libglib" in prompt
