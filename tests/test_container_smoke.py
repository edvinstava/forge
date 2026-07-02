import shutil
import subprocess
import pytest
from forge.container import DockerRunner

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "forge-worker"],
                      capture_output=True).returncode != 0,
    reason="docker or forge-worker image unavailable",
)


def test_start_exec_stop():
    r = DockerRunner("forge-worker")
    cid = r.start("smoke", env={"FOO": "bar"})
    try:
        out = r.exec(cid, ["printenv", "FOO"])
        assert out.exit_code == 0
        assert out.stdout.strip() == "bar"
        assert r.exec(cid, ["bash", "-lc", "echo hi"]).stdout.strip() == "hi"
    finally:
        r.stop(cid)


def test_start_failure_does_not_leak_secret():
    r = DockerRunner("forge-worker-nonexistent-image-zzz")
    with pytest.raises(Exception) as exc:
        r.start("leaktest", env={"CLAUDE_CODE_OAUTH_TOKEN": "SECRET-XYZ-123"})
    assert "SECRET-XYZ-123" not in str(exc.value)
