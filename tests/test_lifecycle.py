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


# --- project-network cleanup (subnet leak: down can't rm a net the proxy holds) ---

def test_compose_down_project_detaches_proxy_and_removes_network(monkeypatch):
    from forge import lifecycle
    calls = []
    monkeypatch.setattr(
        lifecycle.subprocess, "run",
        lambda argv, *a, **k: calls.append(" ".join(argv)))
    lifecycle.compose_down_project("forge-r1")
    assert any("compose -p forge-r1 down -v --remove-orphans" in c for c in calls)
    assert any("network disconnect -f forge-r1_default forge-proxy" in c
               for c in calls)
    assert any("network rm forge-r1_default" in c for c in calls)


def test_network_run_id():
    from forge.lifecycle import network_run_id
    assert network_run_id("forge-ab12_default") == "ab12"
    assert network_run_id("bridge") is None
    assert network_run_id("supabase_network_x_default") is None
    assert network_run_id("forge-_default") is None


def test_dead_networks_spares_active_and_populated_projects():
    from forge.lifecycle import dead_networks
    nets = ["forge-live1_default", "forge-sleepy_default",
            "forge-stopped_default", "forge-gone_default", "bridge"]
    dead = dead_networks(nets,
                         active_run_ids={"live1", "sleepy"},
                         container_run_ids={"live1", "stopped"})
    assert dead == ["forge-gone_default"]


def test_sweep_dead_networks_removes_only_orphans(tmp_path):
    from forge.lifecycle import sweep_dead_networks
    s = Store(tmp_path / "f.db")
    s.create_env("live1", "forge-live1", "u", 1, "live")
    s.create_env("sleepy", "forge-sleepy", "u", 1, "asleep")
    s.create_env("gone", "forge-gone", "u", 1, "live")
    s.mark_reaped("gone")

    calls = []

    def fake_run(argv):
        calls.append(" ".join(argv))
        if "network ls" in calls[-1]:
            return ("forge-live1_default\nforge-sleepy_default\n"
                    "forge-gone_default\nforge-orphan_default\nbridge\n")
        if "ps -a" in calls[-1]:
            # stopped containers still count: warm snapshots keep their net
            return "forge-live1\nforge-sleepy\n\n"
        return ""

    removed = sweep_dead_networks(s, run=fake_run)
    # 'gone' is reaped in the registry, 'orphan' was never registered — both dead
    assert set(removed) == {"forge-gone_default", "forge-orphan_default"}
    for net in removed:
        assert any(f"network disconnect -f {net} forge-proxy" in c for c in calls)
        assert any(f"network rm {net}" in c for c in calls)
    # live and warm-slept networks are never touched
    assert not any("rm forge-live1_default" in c for c in calls)
    assert not any("rm forge-sleepy_default" in c for c in calls)


def test_sweep_dead_networks_no_docker_is_quiet(tmp_path):
    from forge.lifecycle import sweep_dead_networks
    s = Store(tmp_path / "f.db")
    assert sweep_dead_networks(s, run=lambda argv: None) == []
