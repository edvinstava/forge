from forge.store import Store


def test_create_and_get_env(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "forge-r1", "http://localhost:5051", 5051, "live")
    e = s.get_env("r1")
    assert e["state"] == "live"
    assert e["web_url"] == "http://localhost:5051"
    assert e["web_port"] == 5051


def test_list_envs_filters_by_state(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "p1", None, None, "live")
    s.create_env("r2", "p2", None, None, "reaped")
    assert [e["run_id"] for e in s.list_envs(states=("live",))] == ["r1"]


def test_set_env_state_updates_url(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "p1", None, 3000, "starting")
    s.set_env_state("r1", "live", "http://localhost:9")
    e = s.get_env("r1")
    assert e["state"] == "live" and e["web_url"] == "http://localhost:9"


def test_mark_reaped(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "p1", "u", 1, "live")
    s.mark_reaped("r1")
    assert s.get_env("r1")["state"] == "reaped"


def test_get_env_missing_returns_empty(tmp_path):
    assert Store(tmp_path / "f.db").get_env("nope") == {}


def test_mark_asleep_syncs_both_tables_and_stamps(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_run("r1", "o/r", "", "forge/session-r1")
    s.create_env("r1", "forge-r1", "u", 3000, "live")
    s.mark_asleep("r1")
    assert s.get_env("r1")["state"] == "asleep"
    assert s.get_env("r1")["asleep_at"] is not None
    assert s.get_run("r1")["state"] == "asleep"


def test_mark_deleted_syncs_both_tables(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_run("r1", "o/r", "", "forge/session-r1")
    s.create_env("r1", "forge-r1", "u", 3000, "asleep")
    s.mark_deleted("r1")
    assert s.get_env("r1")["state"] == "deleted"
    assert s.get_run("r1")["state"] == "deleted"


def test_snapshot_lockhash_roundtrips(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_env("r1", "forge-r1", None, 3000, "live", web_service="web")
    s.set_snapshot_lockhash("r1", "abc123")
    assert s.get_env("r1")["snapshot_lockhash"] == "abc123"


def test_runtime_facts_roundtrips(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_env("r1", "forge-r1", None, 3000, "live", web_service="web",
                 runtime_facts='{"app":"http://web:3000"}')
    assert s.get_env("r1")["runtime_facts"] == '{"app":"http://web:3000"}'


def test_runtime_facts_defaults_null(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_env("r1", "forge-r1", None, 3000, "live")
    assert s.get_env("r1")["runtime_facts"] is None
