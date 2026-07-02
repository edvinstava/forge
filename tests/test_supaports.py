import socket

import pytest

from forge.supaports import (
    NoFreePortBlock,
    SupabaseAllocator,
    base_ports,
    default_is_free,
    find_free_offset,
    read_project_id,
    required_ports,
    rewrite_config,
)


def test_base_api_port_constant():
    from forge.supaports import SUPABASE_BASE_API_PORT
    assert SUPABASE_BASE_API_PORT == 54321


class FakeStore:
    """In-memory stand-in for the store's supabase_ports table."""

    def __init__(self):
        self.rows = {}  # run_id -> {"run_id","offset","project"}

    def list_supabase(self):
        return list(self.rows.values())

    def reserve_supabase(self, run_id, offset, project):
        self.rows[run_id] = {"run_id": run_id, "offset": offset, "project": project}

    def release_supabase(self, run_id):
        self.rows.pop(run_id, None)

    def get_supabase(self, run_id):
        return dict(self.rows.get(run_id) or {})

# A config.toml shaped like webapp's: the 543xx host-bound cluster, an
# edge_runtime inspector port, plus a remote SMTP port (465) that must NOT move.
CONFIG = """\
project_id = "webapp"

[api]
enabled = true
port = 54321

[db]
port = 54322
shadow_port = 54320

[db.pooler]
enabled = false
port = 54329

[studio]
port = 54323

[inbucket]
port = 54324
# smtp_port = 54325

[auth.email.smtp]
port = 465

[edge_runtime]
inspector_port = 8083

[analytics]
port = 54327
"""


def test_base_ports_collects_allowlisted_only():
    assert set(base_ports(CONFIG)) == {
        54321, 54322, 54320, 54329, 54323, 54324, 8083, 54327
    }
    assert 465 not in base_ports(CONFIG)  # remote SMTP, not a host bind


def test_required_ports_applies_offset():
    assert set(required_ports(CONFIG, 100)) == {
        54421, 54422, 54420, 54429, 54423, 54424, 8183, 54427
    }


def test_rewrite_shifts_allowlisted_ports():
    out = rewrite_config(CONFIG, "webapp-abc123de", 100)
    assert "port = 54421" in out          # api
    assert "port = 54422" in out          # db
    assert "shadow_port = 54420" in out   # db shadow
    assert "port = 54429" in out          # pooler
    assert "port = 54423" in out          # studio
    assert "port = 54424" in out          # inbucket
    assert "port = 54427" in out          # analytics
    assert "inspector_port = 8183" in out # edge_runtime


def test_rewrite_sets_project_id():
    out = rewrite_config(CONFIG, "webapp-abc123de", 100)
    assert 'project_id = "webapp-abc123de"' in out
    assert 'project_id = "webapp"' not in out


def test_rewrite_leaves_smtp_and_comments_untouched():
    out = rewrite_config(CONFIG, "x-1", 100)
    assert "port = 465" in out               # remote SMTP unchanged
    assert "# smtp_port = 54325" in out      # comment unchanged (not 54425)
    assert "enabled = true" in out           # non-port keys unchanged


def test_rewrite_zero_offset_only_changes_project_id():
    out = rewrite_config(CONFIG, "x-1", 0)
    assert "port = 54321" in out and "port = 54322" in out
    assert 'project_id = "x-1"' in out


def test_read_project_id():
    assert read_project_id(CONFIG) == "webapp"


def test_read_project_id_missing_returns_none():
    assert read_project_id("[api]\nport = 54321\n") is None


def test_find_free_offset_starts_at_one_block():
    # all free, nothing reserved -> first block is k=1 (never touches base ports)
    off = find_free_offset(base_ports(CONFIG), set(), lambda p: True, stride=100)
    assert off == 100


def test_find_free_offset_skips_busy_block():
    busy = {54421}  # one port in the k=1 block is taken

    def is_free(p):
        return p not in busy

    off = find_free_offset(base_ports(CONFIG), set(), is_free, stride=100)
    assert off == 200


def test_find_free_offset_skips_reserved_block():
    off = find_free_offset(base_ports(CONFIG), {100}, lambda p: True, stride=100)
    assert off == 200


def test_find_free_offset_raises_when_exhausted():
    with pytest.raises(NoFreePortBlock):
        find_free_offset(base_ports(CONFIG), set(), lambda p: False,
                         stride=100, max_blocks=5)


def test_allocator_reserve_returns_first_free_block():
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: True, stride=100)
    assert a.reserve("r1", CONFIG, "p-r1") == 100
    assert st.get_supabase("r1")["offset"] == 100
    assert st.get_supabase("r1")["project"] == "p-r1"


def test_allocator_reserve_distinct_blocks_for_concurrent_runs():
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: True, stride=100)
    o1 = a.reserve("r1", CONFIG, "p1")
    o2 = a.reserve("r2", CONFIG, "p2")
    assert {o1, o2} == {100, 200}


def test_allocator_reserve_is_idempotent_for_existing_run():
    # Re-provision (wake) must REUSE the run's existing block. Allocating a fresh
    # block instead desyncs config.toml/Supabase (rewritten to the new offset)
    # from the stored offset the proxy routes by -> Caddy proxies /auth to a dead
    # port -> 502. See the offset-desync login bug.
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: True, stride=100)
    first = a.reserve("r1", CONFIG, "p1")
    again = a.reserve("r1", CONFIG, "p1")
    assert again == first
    assert len(st.list_supabase()) == 1


def test_allocator_reserve_not_bumped_by_other_runs():
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: True, stride=100)
    o1 = a.reserve("r1", CONFIG, "p1")   # 100
    a.reserve("r2", CONFIG, "p2")        # 200
    assert a.reserve("r1", CONFIG, "p1") == o1   # r1 keeps its block, not bumped


def test_allocator_release_frees_block_for_reuse():
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: True, stride=100)
    a.reserve("r1", CONFIG, "p1")
    a.release("r1")
    assert st.get_supabase("r1") == {}
    assert a.reserve("r2", CONFIG, "p2") == 100  # block reusable after release


def test_allocator_reconcile_drops_stale_reservations():
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: True, stride=100)
    a.reserve("r1", CONFIG, "p1")
    a.reserve("r2", CONFIG, "p2")
    released = a.reconcile(active_run_ids={"r2"})
    assert released == ["r1"]
    assert st.get_supabase("r1") == {}
    assert st.get_supabase("r2")["offset"] == 200


def test_allocator_raises_when_exhausted():
    st = FakeStore()
    a = SupabaseAllocator(st, is_free=lambda p: False, stride=100, max_blocks=3)
    with pytest.raises(NoFreePortBlock):
        a.reserve("r1", CONFIG, "p1")


def test_default_is_free_detects_bound_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    bound = s.getsockname()[1]
    try:
        assert default_is_free(bound) is False
    finally:
        s.close()
