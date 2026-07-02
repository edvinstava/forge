import uuid

from fastapi.testclient import TestClient
from forge.webapp import create_app
from forge.config import Config
from forge.store import Store


class FakeManager:
    from forge.providers import ClaudeProvider
    provider = ClaudeProvider()
    ended = []

    def __init__(self, store):
        self.store = store

    def can_start(self):
        return (True, "")

    def diff(self, run_id):
        return ""

    def enqueue_batch(self, items):
        # delegate to the real store so the endpoint's persistence is exercised
        bid = "batch-test"
        ids = []
        for it in items:
            rid = uuid.uuid4().hex
            self.store.create_run(rid, it["repo"], it.get("task", ""), "")
            self.store.set_queue_fields(rid, model=it.get("model") or "auto", batch_id=bid)
            ids.append(rid)
        return bid, ids

    def end(self, run_id, reason="manual"):
        type(self).ended.append(run_id)


def _client(tmp_path):
    FakeManager.ended = []
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    return TestClient(create_app(cfg, store, FakeManager(store))), store


def test_batch_creates_queued_runs_no_409(tmp_path):
    client, store = _client(tmp_path)
    r = client.post("/api/batch", json={"items": [
        {"repo": "o/a", "task": "t1"}, {"repo": "o/b", "task": "t2"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] and len(body["run_ids"]) == 2
    assert all(store.get_run(i)["state"] == "queued" for i in body["run_ids"])


def test_batch_rejects_empty(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/batch", json={"items": []}).status_code == 400


def test_batch_rejects_over_limit(tmp_path):
    client, _ = _client(tmp_path)
    items = [{"repo": "o/r", "task": f"t{i}"} for i in range(51)]   # default max 50
    assert client.post("/api/batch", json={"items": items}).status_code == 400


def test_delete_cancels_queued_run(tmp_path):
    client, store = _client(tmp_path)
    ids = client.post("/api/batch", json={"items": [{"repo": "o/a", "task": "t"}]}).json()["run_ids"]
    assert client.delete(f"/api/sessions/{ids[0]}").json() == {"canceled": True}
    assert store.get_run(ids[0])["state"] == "canceled"
    assert ids[0] not in FakeManager.ended            # never went through end()


def test_delete_non_queued_falls_through_to_end(tmp_path):
    client, store = _client(tmp_path)
    store.create_run("live", "o/r", "t", "")
    store.set_state("live", "running")
    assert client.delete("/api/sessions/live").json() == {"ended": True}
    assert "live" in FakeManager.ended


def test_delete_batch_bulk_cancels(tmp_path):
    client, store = _client(tmp_path)
    body = client.post("/api/batch", json={"items": [
        {"repo": "o/a", "task": "t1"}, {"repo": "o/b", "task": "t2"}]}).json()
    r = client.delete(f"/api/batch/{body['batch_id']}")
    assert set(r.json()["canceled"]) == set(body["run_ids"])
    assert all(store.get_run(i)["state"] == "canceled" for i in body["run_ids"])
