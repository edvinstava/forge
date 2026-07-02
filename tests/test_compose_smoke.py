import json
import shutil
import subprocess
import time
import urllib.request

import pytest

from forge.composeenv import ComposeEnv

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "node:22-bookworm-slim"],
                      capture_output=True).returncode != 0,
    reason="docker or node:22-bookworm-slim unavailable",
)

_API = "require('http').createServer((q,s)=>s.end('db-ok')).listen(4000,'0.0.0.0')"
_WEB = (
    "const http=require('http');"
    "http.createServer((q,s)=>{http.get('http://api:4000/',r=>{let d='';"
    "r.on('data',c=>d+=c);r.on('end',()=>{s.statusCode=200;s.end('web-saw:'+d)})})"
    ".on('error',()=>{s.statusCode=502;s.end('err')})}).listen(3000,'0.0.0.0')"
)

COMPOSE = {
    "services": {
        "api": {"image": "node:22-bookworm-slim", "command": ["node", "-e", _API]},
        "web": {"image": "node:22-bookworm-slim", "command": ["node", "-e", _WEB],
                "ports": ["127.0.0.1::3000"], "depends_on": ["api"]},
        "forge": {"image": "node:22-bookworm-slim", "command": ["sleep", "infinity"]},
    }
}


def _get(url, tries=40):
    last = None
    for _ in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=2).read().decode()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise AssertionError(f"never reachable: {url} ({last})")


def test_multiservice_network_port_and_exec(tmp_path):
    f = tmp_path / "forge-compose.yml"
    f.write_text(json.dumps(COMPOSE))   # JSON is valid YAML — no yaml dep needed
    env = ComposeEnv("smoke", [f])
    env.up()
    try:
        # worker exec works (in the `forge` service)
        r = env.exec(["sh", "-lc", "echo exec-ok"], workdir="/", service="forge")
        assert r.exit_code == 0 and r.stdout.strip() == "exec-ok"
        # published web port → host, and web reached `api` over the project network
        hp = env.port("web", 3000)
        assert hp is not None
        assert _get(f"http://localhost:{hp}/") == "web-saw:db-ok"
    finally:
        env.down()
