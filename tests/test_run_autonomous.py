from pathlib import Path

from forge.config import Config, Budget
from forge.store import Store
from forge.session import SessionManager
# pytest prepend import mode puts tests/ on sys.path → import sibling test module.
from test_session import PlannerEnv, FakeHost


def _mgr(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.runs_dir / "forge.db")

    def factory(rid, files):
        e = PlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e

    return SessionManager(cfg, store, FakeHost(), env_factory=factory), store


def test_enqueue_batch_creates_queued_runs_no_can_start(tmp_path):
    mgr, store = _mgr(tmp_path)
    batch_id, ids = mgr.enqueue_batch([
        {"repo": "o/a", "task": "t1"},
        {"repo": "o/b", "task": "t2", "model": "opus"},
    ])
    assert len(ids) == 2
    rows = {r["run_id"]: r for r in store.list_runs()}
    assert all(rows[i]["state"] == "queued" for i in ids)
    assert all(rows[i]["batch_id"] == batch_id for i in ids)
    assert rows[ids[1]]["model"] == "opus"
    assert rows[ids[0]]["model"] == "auto"                 # default
    assert rows[ids[0]]["repo_source"] == "github:o/a"     # default source


def test_run_autonomous_drives_to_done_and_frees_env(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    _, ids = mgr.enqueue_batch([{"repo": "o/a", "task": "do it"}])
    rid = ids[0]
    store.claim_queued(limit=1)                            # queued → running (scheduler)
    reaped = []

    def fake_reap(st, run_id, **kw):                       # hermetic: no docker
        reaped.append(run_id)
        st.mark_reaped(run_id)
    monkeypatch.setattr("forge.lifecycle.reap_project", fake_reap)
    events = []
    mgr.run_autonomous(rid, on_event=events.append)
    assert store.get_run(rid)["state"] == "done"
    assert store.get_env(rid).get("state") == "reaped"     # env freed, slot released
    assert reaped == [rid]                                 # teardown ran
    assert any(e.kind == "done" for e in events)           # sink got the terminal event


def test_run_autonomous_uses_no_gate(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    _, ids = mgr.enqueue_batch([{"repo": "o/a", "task": "do it"}])
    rid = ids[0]
    store.claim_queued(limit=1)
    monkeypatch.setattr("forge.lifecycle.reap_project", lambda st, rid, **kw: st.mark_reaped(rid))
    mgr.run_autonomous(rid)
    assert store.open_checkpoint(rid) is None              # never gated


def test_run_autonomous_hard_failure_sets_failed_and_queue_error(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    _, ids = mgr.enqueue_batch([{"repo": "o/a", "task": "do it"}])
    rid = ids[0]
    store.claim_queued(limit=1)
    monkeypatch.setattr("forge.lifecycle.reap_project", lambda st, rid, **kw: st.mark_reaped(rid))

    def boom(*a, **k):
        raise RuntimeError("provision exploded")
        yield  # pragma: no cover — make it a generator
    monkeypatch.setattr(mgr, "_boot", boom)
    mgr.run_autonomous(rid)
    assert store.get_run(rid)["state"] == "failed"
    assert "provision exploded" in (store.get_run(rid)["queue_error"] or "")


def test_run_autonomous_sink_exception_never_propagates(tmp_path, monkeypatch):
    mgr, store = _mgr(tmp_path)
    _, ids = mgr.enqueue_batch([{"repo": "o/a", "task": "do it"}])
    rid = ids[0]
    store.claim_queued(limit=1)
    monkeypatch.setattr("forge.lifecycle.reap_project", lambda st, rid, **kw: st.mark_reaped(rid))

    def bad_sink(ev):
        raise ValueError("slack down")
    mgr.run_autonomous(rid, on_event=bad_sink)             # must not raise
    assert store.get_run(rid)["state"] in ("done", "failed")


def test_event_sink_registry_pop_is_one_shot(tmp_path):
    mgr, _ = _mgr(tmp_path)
    sink = lambda ev: None
    mgr.set_event_sink("r1", sink)
    assert mgr._pop_sink("r1") is sink
    assert mgr._pop_sink("r1") is None                     # popped → gone
    assert mgr._pop_sink("never") is None                  # unknown → None
