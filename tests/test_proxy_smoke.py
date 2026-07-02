import json
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

from forge import proxy
from forge.composeenv import ComposeEnv

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "node:22-bookworm-slim"],
                      capture_output=True).returncode != 0,
    reason="docker or node:22-bookworm-slim unavailable",
)

_SRV = "require('http').createServer((q,s)=>s.end('hello-proxy')).listen(3000,'0.0.0.0')"
COMPOSE = {"services": {"web": {"image": "node:22-bookworm-slim",
                               "command": ["node", "-e", _SRV]}}}

# Isolated name + a dynamically-chosen free port so this test never collides
# with — or tears down — a live `forge web` daemon (its forge-proxy on 8088 or
# its own web port). A fixed port would clash with whatever the daemon is using.
_PROXY = "forge-proxy-smoketest"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get_via_proxy(port, host, tries=60):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                         headers={"Host": host})
            return urllib.request.urlopen(req, timeout=2).read().decode()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise AssertionError(f"proxy never served {host} ({last})")


def test_proxy_routes_to_web_container(tmp_path):
    """Caddy on the run's compose network routes run-<id>.forge.localhost to the
    web container by name — the full nice-URL path."""
    f = tmp_path / "forge-compose.yml"
    f.write_text(json.dumps(COMPOSE))
    env = ComposeEnv("ptest", [f])
    env.up()
    caddyfile = tmp_path / "Caddyfile"
    port = _free_port()
    routes = proxy.routes_for(
        [{"run_id": "ptest", "web_service": "web", "web_port": 3000}],
        domain="forge.localhost")
    caddyfile.write_text(proxy.caddy_config(routes, port))
    try:
        proxy.ensure_proxy(str(caddyfile), port, name=_PROXY)
        proxy.connect_networks(["ptest"], name=_PROXY)
        proxy.reload_proxy(name=_PROXY)
        body = _get_via_proxy(port, "run-ptest.forge.localhost")
        assert body == "hello-proxy"
    finally:
        subprocess.run(["docker", "rm", "-f", _PROXY], capture_output=True)
        env.down()
