from forge.config import Config
from forge.store import Store
from forge import webapp


class _Mgr:
    def __init__(self, store, admit):
        self.store, self._admit = store, admit

    def admit_count(self):
        return self._admit


def _store(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs")
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    return cfg, Store(cfg.runs_dir / "forge.db")


def test_drain_once_dispatches_exactly_admit_count(tmp_path):
    cfg, store = _store(tmp_path)
    for i in range(5):
        store.create_run(f"r{i}", "o/r", f"t{i}", "")
    dispatched = []
    got = webapp.drain_once(cfg, store, _Mgr(store, admit=2), dispatched.append)
    assert got == ["r0", "r1"] == dispatched                # FIFO, exactly 2
    assert store.get_run("r0")["state"] == "running"
    assert store.get_run("r2")["state"] == "queued"


def test_drain_once_noop_at_zero_capacity(tmp_path):
    cfg, store = _store(tmp_path)
    store.create_run("r0", "o/r", "t", "")
    assert webapp.drain_once(cfg, store, _Mgr(store, admit=0), lambda r: None) == []
    assert store.get_run("r0")["state"] == "queued"


def test_drain_once_dispatches_each_claimed_row_once(tmp_path):
    cfg, store = _store(tmp_path)
    for i in range(3):
        store.create_run(f"r{i}", "o/r", f"t{i}", "")
    seen = []
    webapp.drain_once(cfg, store, _Mgr(store, admit=10), seen.append)
    assert sorted(seen) == ["r0", "r1", "r2"]               # no double-dispatch
