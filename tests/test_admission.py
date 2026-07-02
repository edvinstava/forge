from forge.config import Config, parse_mem_mb
from forge.store import Store
from forge.session import SessionManager


class _Host:  # admission never touches the host
    pass


def _mgr(tmp_path, **cfg_kw):
    cfg = Config(runs_dir=tmp_path / "runs", **cfg_kw)
    store = Store(cfg.runs_dir / "forge.db")
    return SessionManager(cfg, store, _Host()), store


def test_parse_mem_mb():
    assert parse_mem_mb("8g") == 8192
    assert parse_mem_mb("512m") == 512
    assert parse_mem_mb("2G") == 2048
    assert parse_mem_mb("") is None
    assert parse_mem_mb("0") is None
    assert parse_mem_mb("lots") is None


def test_admit_defaults_to_max_live_sessions_when_budget_disabled(tmp_path):
    mgr, _ = _mgr(tmp_path, max_live_sessions=4, mem_budget_mb=0)
    assert mgr.admit_count() == 4


def test_admit_budget_caps_below_max(tmp_path):
    # 10g budget / 8g per session = 1 slot, below max_live_sessions=4
    mgr, _ = _mgr(tmp_path, max_live_sessions=4, mem_budget_mb=10240,
                  web_mem_limit="8g")
    assert mgr.admit_count() == 1


def test_admit_unparseable_mem_limit_disables_budget(tmp_path):
    mgr, _ = _mgr(tmp_path, max_live_sessions=3, mem_budget_mb=99999,
                  web_mem_limit="lots")
    assert mgr.admit_count() == 3          # falls back to max_live_sessions


def test_admit_subtracts_running_runs_and_live_envs_deduped(tmp_path):
    mgr, store = _mgr(tmp_path, max_live_sessions=4, mem_budget_mb=0)
    # one batched worker claimed but not yet provisioned (running, no env)
    store.create_run("claiming", "o/r", "t", "")
    store.set_state("claiming", "running")
    # one worker live AND mid-execute (running run + live env) → counts ONCE
    store.create_run("live", "o/r", "t", "")
    store.set_state("live", "running")
    store.create_env("live", "forge-live", None, 3000, "live")
    assert mgr.admit_count() == 4 - 2      # not 4 - 3


def test_admit_never_negative(tmp_path):
    mgr, store = _mgr(tmp_path, max_live_sessions=1, mem_budget_mb=0)
    for i in range(3):
        store.create_env(f"e{i}", f"forge-e{i}", None, 3000, "live")
    assert mgr.admit_count() == 0
