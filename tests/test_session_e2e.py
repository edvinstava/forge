# tests/test_session_e2e.py
"""
Full-lifecycle integration test for SessionManager:
  start → turn → turn → open_pr → end

Assertions:
  (a) Two turn() calls persist two assistant messages (plus user messages).
  (b) claude_session_id is stable/carried across turns.
  (c) open_pr creates a PR ONLY when called — no pr_url after turns, yes after.
  (d) end() reaps the env — store.get_env state becomes "reaped".
"""
import sys
from pathlib import Path

# tests/ is not a package (no __init__.py) and not on sys.path by default;
# add it so we can import the shared fakes from test_session.py.
_tests_dir = str(Path(__file__).parent)
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import forge.lifecycle as lc
from test_session import FakeHost, FakeEnv, _mgr


def test_full_lifecycle(tmp_path):
    mgr, store = _mgr(tmp_path)

    # ── start ──────────────────────────────────────────────────────────────
    start_events = list(mgr.start("r1", "o/r", "github"))
    kinds = [e.kind for e in start_events]
    assert "phase" in kinds, "start() must yield at least one phase event"
    assert store.get_env("r1")["state"] == "live"

    # ── turn 1 ─────────────────────────────────────────────────────────────
    t1_events = list(mgr.turn("r1", "make the header bold"))
    assert t1_events[-1].kind == "done", f"turn 1 must end with 'done', got {t1_events[-1]}"

    # After turn 1: one user + one assistant message persisted
    msgs_after_t1 = store.list_messages("r1")
    user_msgs = [m for m in msgs_after_t1 if m["role"] == "user"]
    asst_msgs = [m for m in msgs_after_t1 if m["role"] == "assistant"]
    assert len(user_msgs) >= 1, "turn 1 must persist user message"
    assert len(asst_msgs) == 1, f"turn 1 must persist 1 assistant message; got {asst_msgs}"

    # claude_session_id set after turn 1
    run_after_t1 = store.get_run("r1")
    assert run_after_t1["claude_session_id"] == "sess-1", (
        f"claude_session_id should be 'sess-1' after turn 1, got {run_after_t1['claude_session_id']}"
    )

    # (c) No PR yet — pr_url must be absent/None before open_pr is called
    assert not run_after_t1.get("pr_url"), (
        f"pr_url should be None before open_pr; got {run_after_t1.get('pr_url')}"
    )

    # ── turn 2 ─────────────────────────────────────────────────────────────
    t2_events = list(mgr.turn("r1", "also center the footer"))
    assert t2_events[-1].kind == "done", f"turn 2 must end with 'done', got {t2_events[-1]}"

    # (a) After turn 2: two assistant messages total
    msgs_after_t2 = store.list_messages("r1")
    asst_msgs_2 = [m for m in msgs_after_t2 if m["role"] == "assistant"]
    assert len(asst_msgs_2) == 2, (
        f"expected 2 assistant messages after two turns; got {len(asst_msgs_2)}"
    )
    user_msgs_2 = [m for m in msgs_after_t2 if m["role"] == "user"]
    assert len(user_msgs_2) == 2, (
        f"expected 2 user messages after two turns; got {len(user_msgs_2)}"
    )

    # (b) claude_session_id is still "sess-1" (stable across turns)
    run_after_t2 = store.get_run("r1")
    assert run_after_t2["claude_session_id"] == "sess-1", (
        f"claude_session_id must remain 'sess-1' after turn 2; got {run_after_t2['claude_session_id']}"
    )

    # (c) Still no PR after turn 2
    assert not run_after_t2.get("pr_url"), (
        f"pr_url must still be None before open_pr; got {run_after_t2.get('pr_url')}"
    )

    # ── open_pr ────────────────────────────────────────────────────────────
    pr_result = mgr.open_pr("r1")
    assert pr_result["ok"] is True, f"open_pr must succeed; got {pr_result}"
    assert pr_result.get("pr_url"), "open_pr result must include pr_url"

    # (c) PR is now recorded in the store
    run_after_pr = store.get_run("r1")
    assert run_after_pr.get("pr_url"), (
        f"pr_url must be set in store after open_pr; got {run_after_pr.get('pr_url')}"
    )

    # ── end ────────────────────────────────────────────────────────────────
    # (d) end() calls lifecycle.reap_project(store, run_id) which in turn calls
    # downer(project) then store.mark_reaped(run_id).
    # The default downer runs `docker compose … down`; it catches FileNotFoundError
    # when docker is absent, so no real docker is required.  We patch reap_project
    # to use a no-op downer so the test stays hermetic and we can verify the
    # observable store transition without subprocess noise.
    reaped_projects = []

    def noop_downer(project):
        reaped_projects.append(project)

    orig_reap = lc.reap_project
    lc.reap_project = lambda store, run_id, **kw: orig_reap(store, run_id,
                                                             downer=noop_downer)
    try:
        mgr.end("r1")
    finally:
        lc.reap_project = orig_reap

    # noop downer was invoked for the project belonging to "r1"
    assert any("r1" in p for p in reaped_projects), (
        f"reap_project downer must have been called for project containing 'r1'; calls: {reaped_projects}"
    )

    # (d) env state is "deleted" in the store — end() now tombstones the session
    # (workspace teardown via reap_project + mark_deleted), keeping the row +
    # transcript queryable while marking it gone.
    env_after_end = store.get_env("r1")
    assert env_after_end["state"] == "deleted", (
        f"env state must be 'deleted' after end(); got {env_after_end['state']}"
    )
    assert store.get_run("r1")["state"] == "deleted"
