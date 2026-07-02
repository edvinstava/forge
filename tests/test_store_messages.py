from forge.store import Store


def test_messages_roundtrip(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_run("r1", "o/r", "task", "forge/x")
    mid = s.add_message("r1", "user", "fix the date picker")
    s.add_message("r1", "assistant", "done", meta={"cost": 0.12, "diff_files": 3})
    msgs = s.list_messages("r1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["meta"]["diff_files"] == 3
    assert isinstance(mid, int)


def test_session_fields_and_listing(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_run("r1", "o/r", "task", "forge/x")
    s.set_session_fields("r1", claude_session_id="sess-9", repo_source="github:o/r",
                         title="Date picker fix")
    run = s.get_run("r1")
    assert run["claude_session_id"] == "sess-9"
    assert run["repo_source"] == "github:o/r"
    assert run["title"] == "Date picker fix"
    s.create_env("r1", "forge-r1", "http://localhost:5051", 3000, "live", web_service="web")
    rows = s.list_sessions()
    assert rows[0]["run_id"] == "r1"
    assert rows[0]["web_url"] == "http://localhost:5051"
    assert rows[0]["title"] == "Date picker fix"


def test_migration_is_idempotent_and_preserves_rows(tmp_path):
    db = tmp_path / "f.db"
    s = Store(db)
    s.create_run("r1", "o/r", "task", "forge/x")
    s2 = Store(db)            # re-open → migration runs again, no error
    assert s2.get_run("r1")["repo"] == "o/r"
