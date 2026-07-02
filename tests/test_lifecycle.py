from forge.lifecycle import reap_env, reap_superseded
from forge.store import Store


class FakeRunner:
    def __init__(self):
        self.stopped = []

    def stop(self, cid):
        self.stopped.append(cid)


def test_reap_env(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "forge-r1", "u", 1, "live")
    r = FakeRunner()
    reap_env(r, s, "r1")
    assert r.stopped == ["forge-r1"]
    assert s.get_env("r1")["state"] == "reaped"


def test_reap_superseded_keeps_one(tmp_path):
    s = Store(tmp_path / "f.db")
    for rid in ("a", "b", "keep"):
        s.create_env(rid, f"forge-{rid}", "u", 1, "live")
    r = FakeRunner()
    reaped = reap_superseded(r, s, "keep")
    assert set(reaped) == {"a", "b"}
    assert s.get_env("keep")["state"] == "live"
    assert set(r.stopped) == {"forge-a", "forge-b"}


def test_reap_superseded_none_when_only_keeper(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("keep", "forge-keep", "u", 1, "live")
    assert reap_superseded(FakeRunner(), s, "keep") == []


def test_idle_run_ids():
    from datetime import datetime

    from forge.lifecycle import idle_run_ids
    now = datetime(2026, 6, 23, 12, 0, 0)
    envs = [
        {"run_id": "old", "last_seen_at": "2026-06-23 09:00:00"},    # 3h ago
        {"run_id": "fresh", "last_seen_at": "2026-06-23 11:59:00"},  # 1m ago
        {"run_id": "nols"},                                          # no timestamp
    ]
    assert idle_run_ids(envs, now, 7200) == ["old"]   # ttl 2h


def test_dormant_run_ids():
    from datetime import datetime
    from forge.lifecycle import dormant_run_ids
    now = datetime(2026, 6, 27, 12, 0, 0)
    envs = [
        {"run_id": "stale", "asleep_at": "2026-06-23 12:00:00"},   # 4d ago
        {"run_id": "recent", "asleep_at": "2026-06-27 06:00:00"},  # 6h ago
        {"run_id": "nots"},                                        # never slept
    ]
    assert dormant_run_ids(envs, now, 259200) == ["stale"]   # ttl 3d


def test_reap_superseded_projects(tmp_path):
    from forge.lifecycle import reap_superseded_projects
    s = Store(tmp_path / "f.db")
    for rid in ("a", "b", "keep"):
        s.create_env(rid, f"forge-{rid}", "u", 1, "live")
    downed = []
    ids = reap_superseded_projects(s, "keep", downer=downed.append)
    assert set(ids) == {"a", "b"}
    assert set(downed) == {"forge-a", "forge-b"}
    assert s.get_env("a")["state"] == "reaped"
    assert s.get_env("keep")["state"] == "live"
