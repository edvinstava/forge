"""Gated real-Docker session smoke tests.

Module gate: skip the entire module unless docker is present AND the
forge-worker image exists locally (mirrors test_node_web_smoke.py).

Test A — test_session_start_serves_and_diff_empty
    Hermetic: no Claude, no GitHub, no outbound network.
    Provisions a real compose stack via SessionManager.start() using a tiny
    local git repo, health-gates the dev server, asserts the web_url is
    reachable and returns the expected body, and asserts diff() is empty.
    Teardown via mgr.end() runs in a finally block.

Test B — test_live_turn_and_on_demand_pr
    Skipped by default. Opted-in only when FORGE_SMOKE_LIVE=1 AND both
    CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN are set. Exercises the full live arc:
    start → turn → diff non-empty → open_pr.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request

import pytest

from forge.config import Config
from forge.hostops import LocalHost
from forge.session import SessionManager
from forge.store import Store

# ---------------------------------------------------------------------------
# Module-level gate: skip unless Docker + forge-worker image are present
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(
        ["docker", "image", "inspect", "forge-worker"],
        capture_output=True,
    ).returncode != 0,
    reason="docker or forge-worker image unavailable",
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

PKG = json.dumps(
    {"name": "tiny", "version": "1.0.0", "scripts": {"dev": "node server.js"}}
)
SERVER = (
    "require('http').createServer((q,s)=>s.end('hello-forge'))"
    ".listen(process.env.PORT||3000,'0.0.0.0')"
)


def _make_local_repo(path):
    """Create a minimal committed node repo at *path* so clone_local works."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(PKG)
    (path / "server.js").write_text(SERVER)
    # Exclude node_modules and lockfiles so npm install during provisioning
    # does not produce a non-empty diff.
    (path / ".gitignore").write_text("node_modules/\npackage-lock.json\n")
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@forge"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Forge Test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", "commit", "-m", "init"],
        check=True, capture_output=True,
    )


def _http_get(url, tries=80):
    last = None
    for _ in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=2).read().decode()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise AssertionError(f"never reachable: {url} ({last})")


def _build_session_manager(tmp_path):
    """Return (mgr, store) wired to a fresh runs_dir under tmp_path."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    cfg = Config(
        runs_dir=str(runs_dir),
        oauth_token="x",
        gh_token="x",
    )
    store = Store(runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, LocalHost())
    return mgr, store


# ---------------------------------------------------------------------------
# Test A — hermetic provisioning: no Claude, no GitHub
# ---------------------------------------------------------------------------


def test_session_start_serves_and_diff_empty(tmp_path):
    """SessionManager.start() provisions a real compose stack and serves HTTP.

    Steps:
    1. Build a tiny committed local git repo.
    2. start() the session (consumes generator; no Claude or GitHub needed).
    3. Assert env state == 'live'.
    4. Fetch the web_url and assert the body is 'hello-forge'.
    5. Assert diff() is empty/whitespace (no edits were made).
    6. Teardown via end() in a finally block.
    """
    repo = tmp_path / "repo"
    _make_local_repo(repo)

    mgr, store = _build_session_manager(tmp_path)

    # Derive a short unique run_id from tmp_path to avoid compose project collisions.
    run_id = "smk" + tmp_path.name[-6:]

    events = []
    try:
        events = list(mgr.start(run_id, str(repo), "local"))

        # Verify no error events were emitted during provisioning
        error_events = [e for e in events if e.kind == "error"]
        assert error_events == [], f"provisioning errors: {error_events}"

        env_row = store.get_env(run_id)
        assert env_row.get("state") == "live", (
            f"expected env state 'live', got {env_row.get('state')!r}; "
            f"events={events}"
        )

        web_url = env_row.get("web_url")
        assert web_url, f"no web_url in env row: {env_row}"

        body = _http_get(web_url)
        assert body == "hello-forge", f"unexpected body: {body!r}"

        diff_output = mgr.diff(run_id)
        assert not diff_output.strip(), (
            f"expected empty diff, got:\n{diff_output}"
        )
    finally:
        mgr.end(run_id)


# ---------------------------------------------------------------------------
# Test B — live arc (Claude + GitHub); SKIPPED by default
# ---------------------------------------------------------------------------

_LIVE_TOKENS = (
    os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
    os.environ.get("GH_TOKEN", ""),
)


@pytest.mark.skipif(
    not os.environ.get("FORGE_SMOKE_LIVE")
    or not (_LIVE_TOKENS[0] and _LIVE_TOKENS[1]),
    reason=(
        "live smoke disabled by default; set FORGE_SMOKE_LIVE=1 and "
        "CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN to opt in"
    ),
)
def test_live_turn_and_on_demand_pr(tmp_path):
    """Live arc: start → turn (real Claude) → diff non-empty → open_pr.

    Opt-in only: requires FORGE_SMOKE_LIVE=1, CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN.

    Arc:
    1. Provision a session from a local repo (same fixture as Test A).
    2. Send a turn() prompt that makes a visible change to server.js
       (e.g. 'Change the HTTP response body to hello-forge-live').
    3. Assert the live web_url now serves the updated content.
    4. Assert diff(run_id) is non-empty.
    5. Assert store.get_run(run_id)['pr_url'] is None before open_pr().
    6. Call open_pr(run_id) and assert pr_url is returned.
    7. Teardown in finally.
    """
    repo = tmp_path / "repo"
    _make_local_repo(repo)

    mgr, store = _build_session_manager(tmp_path)
    run_id = "smklive" + tmp_path.name[-6:]

    try:
        list(mgr.start(run_id, str(repo), "local"))
        assert store.get_env(run_id).get("state") == "live"

        # Issue a live Claude turn
        turn_events = list(
            mgr.turn(run_id, "Change the HTTP response body to hello-forge-live")
        )
        error_events = [e for e in turn_events if e.kind == "error"]
        assert error_events == [], f"turn errors: {error_events}"

        env_row = store.get_env(run_id)
        web_url = env_row.get("web_url")
        assert web_url
        body = _http_get(web_url)
        assert "hello-forge-live" in body, f"unexpected body after turn: {body!r}"

        diff_output = mgr.diff(run_id)
        assert diff_output.strip(), "expected non-empty diff after turn"

        # No PR before open_pr
        assert store.get_run(run_id).get("pr_url") is None

        result = mgr.open_pr(run_id)
        assert result.get("ok"), f"open_pr failed: {result}"
        assert result.get("pr_url"), "no pr_url in result"
        assert store.get_run(run_id).get("pr_url") == result["pr_url"]
    finally:
        mgr.end(run_id)
