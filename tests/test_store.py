from forge.store import Store


def test_create_and_get_run(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "do x", "forge/do-x-r1")
    run = s.get_run("r1")
    assert run["repo"] == "a/b"
    assert run["state"] == "queued"
    assert run["pr_url"] is None


def test_set_state_and_pr_url(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "t", "br")
    s.set_state("r1", "done", pr_url="https://github.com/a/b/pull/1")
    run = s.get_run("r1")
    assert run["state"] == "done"
    assert run["pr_url"].endswith("/pull/1")


def test_events_round_trip(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "t", "br")
    s.add_event("r1", "verify", {"passed": False})
    s.add_event("r1", "verify", {"passed": True})
    evs = s.list_events("r1")
    assert [e["type"] for e in evs] == ["verify", "verify"]
    assert evs[1]["payload"]["passed"] is True


def test_auto_draft_defaults_false_and_persists(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "t", "br")
    assert not s.get_run("r1").get("auto_draft")   # default: supervised
    s.set_auto_draft("r1", True)
    assert bool(s.get_run("r1")["auto_draft"]) is True
    s.set_auto_draft("r1", False)
    assert bool(s.get_run("r1")["auto_draft"]) is False


def test_count_checkpoints_by_type(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "t", "br")
    assert s.count_checkpoints("r1", "needs_input") == 0
    cid = s.create_checkpoint("r1", "needs_input", {"q": "creds?"})
    assert s.count_checkpoints("r1", "needs_input") == 1
    # Answering it must NOT drop the count — it records that we already asked.
    s.answer_checkpoint(cid, {"action": "edit"})
    assert s.count_checkpoints("r1", "needs_input") == 1
    # A different type is counted separately.
    s.create_checkpoint("r1", "plan_approval", {})
    assert s.count_checkpoints("r1", "needs_input") == 1
    assert s.count_checkpoints("r1", "plan_approval") == 1


def test_set_attachments_roundtrip(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "o/r", "", "b")
    s.set_attachments("r1", '["1-a.png"]')
    assert s.get_run("r1")["attachments_json"] == '["1-a.png"]'
