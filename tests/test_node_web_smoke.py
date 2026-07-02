import json
import shutil
import subprocess
import time
import urllib.request

import pytest

from forge.composeenv import ComposeEnv
from forge.health import health_poll_argv
from forge.recipe import node_web_recipe

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "forge-worker"],
                      capture_output=True).returncode != 0,
    reason="docker or forge-worker image unavailable",
)

PKG = json.dumps({"name": "tiny", "version": "1.0.0",
                  "scripts": {"dev": "node server.js"}})
SERVER = ("require('http').createServer((q,s)=>s.end('hello-forge'))"
          ".listen(process.env.PORT||3000,'0.0.0.0')")


def _get(url, tries=80):
    last = None
    for _ in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=2).read().decode()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise AssertionError(f"never reachable: {url} ({last})")


def test_node_web_recipe_runs_and_serves(tmp_path):
    """Generate the node-web recipe for a real tiny repo, bring it up via
    ComposeEnv, health-gate the dev server from the worker service, and fetch
    the app from the host — the full clickable-URL path for a single-service app."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(PKG)
    (repo / "server.js").write_text(SERVER)

    recipe = node_web_recipe(str(repo), "forge-worker", PKG)
    cf = tmp_path / "forge-compose.yml"
    cf.write_text(json.dumps(recipe.compose))

    env = ComposeEnv("nwsmoke", [cf])
    env.up({"CLAUDE_CODE_OAUTH_TOKEN": "x", "GH_TOKEN": "x"})
    try:
        # the worker service can reach the web dev server over the project network
        h = env.exec(health_poll_argv(3000, "/", 90, host="web"), service="forge")
        assert h.exit_code == 0, h.stderr
        hp = env.port("web", 3000)
        assert hp is not None
        assert _get(f"http://localhost:{hp}/") == "hello-forge"
    finally:
        env.down()
