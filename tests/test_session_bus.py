"""Every SessionManager flow publishes its TurnEvents to manager.bus (stamped
with seq + origin) so the OTHER surface can follow a turn live — the spine of
the Slack↔web interop (see docs/superpowers/specs/2026-07-02-slack-web-interop-design.md)."""
import json as _json
from pathlib import Path

from forge.config import Config, Budget
from forge.session import SessionManager
from forge.store import Store

from test_session import FakeEnv, FakeHost, PlannerEnv


def _mgr(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    return SessionManager(cfg, store, FakeHost(),
                          env_factory=lambda rid, files: FakeEnv(rid, files)), store


def _planner_mgr(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")

    def factory(rid, files):
        e = PlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e
    return SessionManager(cfg, store, FakeHost(), env_factory=factory), store, flow


def test_start_publishes_every_event_with_origin(tmp_path):
    mgr, store = _mgr(tmp_path)
    events = list(mgr.start("r1", "o/r", "github", origin="web"))
    published = mgr.bus.replay("r1")
    # every yielded event, plus the bus-only stream_end terminator
    assert [p["kind"] for p in published[:-1]] == [e.kind for e in events]
    assert [p["data"] for p in published[:-1]] == [e.data for e in events]
    assert published[-1]["kind"] == "stream_end"
    assert {p["origin"] for p in published} == {"web"}
    assert [p["seq"] for p in published] == list(range(1, len(events) + 2))


def test_flows_terminate_bus_stream_with_stream_end(tmp_path):
    # Several flows end without a `done` (wake stops at url, plan_task at
    # checkpoint) — passive followers rely on stream_end to unlock their UI.
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    kinds = [p["kind"] for p in mgr.bus.replay("r1")]
    assert kinds[-1] == "stream_end" and kinds[-2] == "checkpoint"


def test_origin_defaults_to_api(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    assert {p["origin"] for p in mgr.bus.replay("r1")} == {"api"}


def test_turn_publishes_with_slack_origin(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    before = mgr.bus.last_seq("r1")
    events = list(mgr.turn("r1", "make a change", origin="slack"))
    tail = mgr.bus.replay("r1", since=before)
    assert [p["kind"] for p in tail] == [e.kind for e in events] + ["stream_end"]
    assert {p["origin"] for p in tail} == {"slack"}


def test_respond_checkpoint_yields_checkpoint_answered_first(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web(),
                       origin="web"))
    cp = store.open_checkpoint("r1")
    before = mgr.bus.last_seq("r1")
    events = list(mgr.respond_checkpoint("r1", cp["id"], "approve", origin="slack"))
    assert events[0].kind == "checkpoint_answered"
    assert events[0].data == {"id": cp["id"], "action": "approve", "body": None}
    tail = mgr.bus.replay("r1", since=before)
    assert tail[0]["kind"] == "checkpoint_answered"
    assert tail[0]["origin"] == "slack"


def test_respond_checkpoint_no_match_does_not_emit_answered(tmp_path):
    mgr, store, flow = _planner_mgr(tmp_path)
    list(mgr.plan_task("r1", "Add logout", policy=flow.CheckpointPolicy.for_web()))
    events = list(mgr.respond_checkpoint("r1", 999, "approve"))
    kinds = [e.kind for e in events]
    assert kinds == ["error"] and "checkpoint_answered" not in kinds


def test_wake_publishes(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    assert mgr.sleep("r1") is True
    before = mgr.bus.last_seq("r1")
    list(mgr.wake("r1", origin="web"))
    tail = mgr.bus.replay("r1", since=before)
    assert tail and {p["origin"] for p in tail} == {"web"}


def test_run_autonomous_publishes_with_queue_origin(tmp_path):
    from forge import flow
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")

    def factory(rid, files):
        e = PlannerEnv(rid, files)
        e._ws = str(cfg.runs_dir / rid / "workspace")
        Path(e._ws).mkdir(parents=True, exist_ok=True)
        return e
    mgr = SessionManager(cfg, store, FakeHost(), env_factory=factory)
    batch_id, (rid,) = mgr.enqueue_batch([{"repo": "o/r", "task": "Add logout"}])
    store.claim_queued(limit=1)
    mgr.run_autonomous(rid)
    published = mgr.bus.replay(rid)
    assert published, "autonomous run must publish its events"
    assert {p["origin"] for p in published} == {"queue"}
