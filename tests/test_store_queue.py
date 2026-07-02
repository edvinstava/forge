from pathlib import Path
from forge.store import Store


def _store(tmp_path) -> Store:
    return Store(tmp_path / "forge.db")


def test_migration_adds_queue_columns(tmp_path):
    s = _store(tmp_path)
    with s._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
    assert {"model", "batch_id", "queue_error"} <= cols


def test_set_queue_fields_and_list_sessions_exposes_them(tmp_path):
    s = _store(tmp_path)
    s.create_run("r1", "o/r", "do a thing", "")
    s.set_queue_fields("r1", model="opus", batch_id="b1")
    row = s.get_run("r1")
    assert row["model"] == "opus" and row["batch_id"] == "b1"
    sess = {x["run_id"]: x for x in s.list_sessions()}["r1"]
    assert sess["batch_id"] == "b1" and sess["model"] == "opus"


def test_claim_queued_is_fifo_and_respects_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.create_run(f"r{i}", "o/r", f"t{i}", "")
    claimed = s.claim_queued(limit=2)
    assert [r["run_id"] for r in claimed] == ["r0", "r1"]      # oldest first
    assert all(r["state"] == "running" for r in claimed)
    assert s.get_run("r2")["state"] == "queued"                # untouched


def test_claim_queued_never_double_dispatches(tmp_path):
    s = _store(tmp_path)
    for i in range(2):
        s.create_run(f"r{i}", "o/r", f"t{i}", "")
    first = {r["run_id"] for r in s.claim_queued(limit=5)}
    second = {r["run_id"] for r in s.claim_queued(limit=5)}     # all now running
    assert first == {"r0", "r1"} and second == set()


def test_claim_queued_ignores_non_queued(tmp_path):
    s = _store(tmp_path)
    s.create_run("live", "o/r", "t", "")
    s.set_state("live", "running")                             # e.g. interactive turn
    assert s.claim_queued(limit=5) == []


def test_set_queue_error(tmp_path):
    s = _store(tmp_path)
    s.create_run("r1", "o/r", "t", "")
    s.set_queue_error("r1", "boom")
    assert s.get_run("r1")["queue_error"] == "boom"


def test_cancel_queued_only_when_queued(tmp_path):
    s = _store(tmp_path)
    s.create_run("q", "o/r", "t", "")
    assert s.cancel_queued("q") is True
    assert s.get_run("q")["state"] == "canceled"
    s.create_run("run", "o/r", "t", "")
    s.set_state("run", "running")
    assert s.cancel_queued("run") is False
    assert s.get_run("run")["state"] == "running"


def test_cancel_batch_cancels_only_queued_in_batch(tmp_path):
    s = _store(tmp_path)
    for rid in ("a", "b", "c"):
        s.create_run(rid, "o/r", "t", "")
        s.set_queue_fields(rid, batch_id="B")
    s.create_run("other", "o/r", "t", "")
    s.set_queue_fields("other", batch_id="OTHER")
    s.set_state("b", "running")                                # already dispatched
    canceled = set(s.cancel_batch("B"))
    assert canceled == {"a", "c"}
    assert s.get_run("b")["state"] == "running"
    assert s.get_run("other")["state"] == "queued"


def test_reclaim_orphans_only_touches_batched_running(tmp_path):
    s = _store(tmp_path)
    s.create_run("batched", "o/r", "t", "")
    s.set_queue_fields("batched", batch_id="B")
    s.set_state("batched", "running")                          # orphaned worker
    s.create_run("interactive", "o/r", "t", "")
    s.set_state("interactive", "running")                      # live human turn, no batch_id
    reset = s.reclaim_orphans()
    assert reset == ["batched"]
    assert s.get_run("batched")["state"] == "queued"
    assert s.get_run("interactive")["state"] == "running"      # untouched


def test_set_run_target_updates_repo_and_branch(tmp_path):
    s = _store(tmp_path)
    s.create_run("r1", "raw-url", "", "")
    s.set_run_target("r1", repo="o/r", branch="forge/x")
    row = s.get_run("r1")
    assert row["repo"] == "o/r" and row["branch"] == "forge/x"


def test_list_runs_filters_by_state(tmp_path):
    s = _store(tmp_path)
    s.create_run("q", "o/r", "t", "")
    s.create_run("run", "o/r", "t", "")
    s.set_state("run", "running")
    assert [r["run_id"] for r in s.list_runs(states=("running",))] == ["run"]
    assert {r["run_id"] for r in s.list_runs()} == {"q", "run"}
