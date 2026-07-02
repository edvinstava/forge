from forge.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "o/r", "do x", "forge/x")
    return s


def test_lifecycle_state_roundtrips(tmp_path):
    s = _store(tmp_path)
    assert s.get_run("r1").get("lifecycle_state") in (None, "")
    s.set_lifecycle_state("r1", "planning")
    assert s.get_run("r1")["lifecycle_state"] == "planning"


def test_plan_roundtrips(tmp_path):
    s = _store(tmp_path)
    s.set_plan("r1", '{"goal":"x"}')
    assert s.get_run("r1")["plan_json"] == '{"goal":"x"}'


def test_create_and_get_open_checkpoint(tmp_path):
    s = _store(tmp_path)
    cid = s.create_checkpoint("r1", "plan_approval", {"plan": {"goal": "x"}})
    cp = s.open_checkpoint("r1")
    assert cp["id"] == cid
    assert cp["ctype"] == "plan_approval"
    assert cp["payload"]["plan"]["goal"] == "x"
    assert cp["status"] == "open"


def test_answer_closes_checkpoint(tmp_path):
    s = _store(tmp_path)
    cid = s.create_checkpoint("r1", "plan_approval", {})
    s.answer_checkpoint(cid, {"action": "approve"})
    assert s.open_checkpoint("r1") is None


def test_at_most_one_open_checkpoint(tmp_path):
    s = _store(tmp_path)
    first = s.create_checkpoint("r1", "plan_approval", {})
    second = s.create_checkpoint("r1", "ambiguity", {})
    cp = s.open_checkpoint("r1")
    assert cp["id"] == second          # newest open
    assert first != second             # the first was superseded (cancelled)
