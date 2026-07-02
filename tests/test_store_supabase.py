from forge.store import Store


def test_reserve_and_get_supabase(tmp_path):
    s = Store(tmp_path / "f.db")
    s.reserve_supabase("r1", 100, "webapp-r1")
    row = s.get_supabase("r1")
    assert row["offset"] == 100
    assert row["project"] == "webapp-r1"


def test_list_supabase_returns_all_reservations(tmp_path):
    s = Store(tmp_path / "f.db")
    s.reserve_supabase("r1", 100, "p1")
    s.reserve_supabase("r2", 200, "p2")
    offsets = sorted(r["offset"] for r in s.list_supabase())
    assert offsets == [100, 200]


def test_reserve_is_idempotent_per_run(tmp_path):
    s = Store(tmp_path / "f.db")
    s.reserve_supabase("r1", 100, "p1")
    s.reserve_supabase("r1", 300, "p1b")  # re-reserve same run replaces
    assert s.get_supabase("r1")["offset"] == 300
    assert len(s.list_supabase()) == 1


def test_release_supabase(tmp_path):
    s = Store(tmp_path / "f.db")
    s.reserve_supabase("r1", 100, "p1")
    s.release_supabase("r1")
    assert s.get_supabase("r1") == {}
    assert s.list_supabase() == []


def test_get_supabase_missing_returns_empty(tmp_path):
    assert Store(tmp_path / "f.db").get_supabase("nope") == {}
