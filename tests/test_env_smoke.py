import shutil
import subprocess
import urllib.request

import pytest

from forge.container import DockerRunner
from forge.health import health_poll_argv

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "forge-worker"],
                      capture_output=True).returncode != 0,
    reason="docker or forge-worker image unavailable",
)

_SERVER = (
    "node -e \"require('http').createServer((q,s)=>{s.statusCode=200;"
    "s.end('forge-ok')}).listen(3000,'0.0.0.0')\""
)


def test_publish_health_and_host_url():
    """The real clickable-URL path: publish a port, start a server, health-gate
    inside the container, resolve the host port, and fetch it from the host."""
    r = DockerRunner("forge-worker")
    cid = r.start("envsmoke", env={}, publish_port=3000)
    try:
        r.exec_detached(cid, ["sh", "-lc", _SERVER])
        h = r.exec(cid, health_poll_argv(3000, "/", 30))
        assert h.exit_code == 0, h.stderr
        host_port = r.port(cid, 3000)
        assert host_port is not None
        body = urllib.request.urlopen(
            f"http://localhost:{host_port}/", timeout=5).read().decode()
        assert body == "forge-ok"
    finally:
        r.stop(cid)
