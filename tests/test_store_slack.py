from forge.store import Store


def test_link_and_lookup_thread(tmp_path):
    s = Store(tmp_path / "f.db")
    s.link_slack_thread("1700.1", "D123", "run-abc", "1700.1")
    assert s.run_for_thread("1700.1") == "run-abc"
    assert s.run_for_thread("nope") is None
    row = s.slack_thread_for_run("run-abc")
    assert row["channel"] == "D123"
    assert row["anchor_ts"] == "1700.1"
    assert s.slack_thread_for_run("missing") is None


def test_link_is_idempotent_on_thread_ts(tmp_path):
    s = Store(tmp_path / "f.db")
    s.link_slack_thread("1700.1", "D123", "run-1", "1700.1")
    s.link_slack_thread("1700.1", "D123", "run-2", "1700.1")  # same thread re-linked
    assert s.run_for_thread("1700.1") == "run-2"
