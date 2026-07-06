from fastapi.testclient import TestClient

from forge.config import Config
from forge.store import Store
from forge.webapp import (attach_public_gate, create_app,
                          public_request_allowed)


class FakeManager:
    from forge.providers import ClaudeProvider
    provider = ClaudeProvider()
    def __init__(self, store): self.store = store
    def can_start(self): return (True, "")
    def diff(self, run_id): return ""


def _gated_client(tmp_path, public_host="tun.trycloudflare.com"):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    app = create_app(cfg, store, FakeManager(store))
    attach_public_gate(app, public_host)
    # A real local browser talks to forge over loopback; default the client's
    # Host to 127.0.0.1 so "local traffic" cases mirror production (the gate
    # allowlists loopback and restricts everything else to the webhook).
    return TestClient(app, base_url="http://127.0.0.1")


def test_public_request_allowed_pure():
    pub = "tun.trycloudflare.com"
    # local traffic untouched, any path
    assert public_request_allowed("localhost:8099", "GET", "/api/sessions", pub)
    # tunnel host: only the webhook POST
    assert public_request_allowed(pub, "POST", "/api/github/webhook", pub)
    assert not public_request_allowed(pub, "GET", "/api/sessions", pub)
    assert not public_request_allowed(pub, "GET", "/api/github/webhook", pub)
    assert not public_request_allowed(f"{pub}:443", "GET", "/", pub)  # port stripped
    assert public_request_allowed("TUN.trycloudflare.com", "POST",
                                  "/api/github/webhook", pub)  # case-insensitive
    # loopback is the only Host trusted with the full API
    assert public_request_allowed("127.0.0.1:8099", "POST", "/api/sessions", pub)
    # fail closed: an unknown or absent Host is restricted to the webhook, not
    # allowed through (the previous denylist let any non-public Host in).
    assert not public_request_allowed("evil.example", "GET", "/api/sessions", pub)
    assert not public_request_allowed("", "GET", "/api/sessions", pub)
    assert public_request_allowed("evil.example", "POST", "/api/github/webhook", pub)
    # no public host configured => gate inert
    assert public_request_allowed(pub, "GET", "/api/sessions", "")
    # *.localhost is loopback by definition (RFC 6761): the workspace serves
    # itself from forge.localhost so its app iframe is same-site (cookies
    # survive login) — that Host must keep full API access.
    assert public_request_allowed("forge.localhost:8099", "GET",
                                  "/api/sessions", pub)
    assert public_request_allowed("run-abc.forge.localhost", "GET",
                                  "/api/sessions", pub)
    # ...but only as a true suffix label, not a lookalike public domain
    assert not public_request_allowed("forge.localhost.evil.example", "GET",
                                      "/api/sessions", pub)


def test_public_request_allowed_strips_trailing_dot():
    # "host." is the same origin as "host" (FQDN root form) — a classic
    # Host-ACL bypass if unnormalized.
    pub = "tun.trycloudflare.com"
    assert not public_request_allowed("tun.trycloudflare.com.", "GET",
                                      "/api/sessions", pub)
    assert public_request_allowed("tun.trycloudflare.com.:443", "POST",
                                  "/api/github/webhook", pub)


def test_gate_blocks_tunnel_host_off_webhook(tmp_path):
    client = _gated_client(tmp_path)
    r = client.get("/api/sessions", headers={"Host": "tun.trycloudflare.com"})
    assert r.status_code == 403


def test_gate_leaves_local_traffic_alone(tmp_path):
    client = _gated_client(tmp_path)
    assert client.get("/api/sessions").status_code == 200


import hashlib
import hmac
import json
import threading
import uuid

from forge.events import TurnEvent
from forge.webapp import attach_github_webhook

SECRET = "whsec-test"


class RecordingGhApp:
    def __init__(self):
        self.calls = []
        self.commented = threading.Event()

    def installation_token(self, owner, repo):
        return "ghs_tok"

    def http(self, method, url, token, data=None):
        self.calls.append((method, url, token, data))
        if "/comments" in url and url.endswith("/comments"):
            self.commented.set()
        return {}


class ReviewingManager(FakeManager):
    def __init__(self, store, fail=False, capacity=True):
        super().__init__(store)
        self.reviews = []
        self.reviewed = threading.Event()
        self.fail = fail
        self.capacity = capacity

    def can_start(self):
        return (self.capacity, "" if self.capacity else "max_live_sessions reached (4)")

    def review(self, run_id, pr, model="auto", origin="api"):
        self.reviews.append((run_id, pr, model, origin))
        self.reviewed.set()
        if self.fail:
            yield TurnEvent("error", {"kind": "clone", "detail": "boom"})
        else:
            yield TurnEvent("review", {"ok": True})


def _wh_client(tmp_path, fail=False, capacity=True):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws",
                 gh_app_slug="acme-forge")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = ReviewingManager(store, fail=fail, capacity=capacity)
    gh = RecordingGhApp()
    app = create_app(cfg, store, mgr)
    attach_github_webhook(app, cfg, mgr, gh, SECRET)
    return TestClient(app), mgr, gh


def _payload(body="@acme-forge review"):
    return {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "x"}},
        "comment": {"id": 991, "body": body, "author_association": "OWNER"},
        "sender": {"type": "User"},
        "repository": {"full_name": "acme/app"},
    }


def _post(client, payload, event="issue_comment", secret=SECRET, guid=None):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/github/webhook", content=raw, headers={
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": guid or uuid.uuid4().hex,
        "Content-Type": "application/json",
    })


def test_command_comment_starts_review_with_github_origin(tmp_path):
    client, mgr, gh = _wh_client(tmp_path)
    r = _post(client, _payload())
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True and body["run_id"]
    assert mgr.reviewed.wait(5)
    run_id, pr, model, origin = mgr.reviews[0]
    assert (pr, model, origin) == ("acme/app#7", "auto", "github")
    assert run_id == body["run_id"]
    # 👀 ack reaction reached the command comment
    assert any("/issues/comments/991/reactions" in c[1] for c in gh.calls)


def test_bad_signature_401_and_no_review(tmp_path):
    client, mgr, _ = _wh_client(tmp_path)
    assert _post(client, _payload(), secret="wrong").status_code == 401
    assert not mgr.reviews


def test_ping_ok(tmp_path):
    client, _, _ = _wh_client(tmp_path)
    assert _post(client, {"zen": "..."}, event="ping").status_code == 200


def test_duplicate_delivery_runs_once(tmp_path):
    client, mgr, _ = _wh_client(tmp_path)
    guid = "d-1"
    assert _post(client, _payload(), guid=guid).json()["accepted"] is True
    assert _post(client, _payload(), guid=guid).json() == {"duplicate": True}
    assert mgr.reviewed.wait(5)
    assert len(mgr.reviews) == 1


def test_non_command_comment_ignored(tmp_path):
    client, mgr, _ = _wh_client(tmp_path)
    assert _post(client, _payload(body="lgtm")).json() == {"ignored": True}
    assert not mgr.reviews


def test_capacity_refusal_comments_and_skips_review(tmp_path):
    client, mgr, gh = _wh_client(tmp_path, capacity=False)
    body = _post(client, _payload()).json()
    assert body["accepted"] is False
    assert gh.commented.wait(5)
    assert not mgr.reviews
    m, url, tok, data = next(c for c in gh.calls if c[1].endswith("/issues/7/comments"))
    assert "capacity" in data["body"]


def test_review_error_posts_failure_comment(tmp_path):
    client, mgr, gh = _wh_client(tmp_path, fail=True)
    assert _post(client, _payload()).json()["accepted"] is True
    assert gh.commented.wait(5)
    m, url, tok, data = next(c for c in gh.calls if c[1].endswith("/issues/7/comments"))
    assert "failed" in data["body"]


def test_bad_json_is_400(tmp_path):
    client, _, _ = _wh_client(tmp_path)
    raw = b"not-json"
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = client.post("/api/github/webhook", content=raw, headers={
        "X-Hub-Signature-256": sig, "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": "g"})
    assert r.status_code == 400


def test_no_secret_is_503(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = ReviewingManager(store)
    app = create_app(cfg, store, mgr)
    attach_github_webhook(app, cfg, mgr, RecordingGhApp(), "")
    client = TestClient(app)
    assert client.post("/api/github/webhook", content=b"{}").status_code == 503


def test_gate_and_webhook_together_tunnel_host_can_only_review(tmp_path):
    # The production wiring: gate + webhook on one app. Also plants an
    # explicit catch-all Mount so the route-before-mount insertion is
    # exercised deterministically (the SPA mount only exists when web/dist
    # is built in this checkout).
    from starlette.routing import Mount, Router
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws",
                 gh_app_slug="acme-forge")
    store = Store(cfg.runs_dir / "forge.db")
    mgr = ReviewingManager(store)
    gh = RecordingGhApp()
    app = create_app(cfg, store, mgr)
    app.router.routes.append(Mount("/", app=Router()))
    attach_public_gate(app, "tun.trycloudflare.com")
    attach_github_webhook(app, cfg, mgr, gh, SECRET)
    client = TestClient(app)
    host = {"Host": "tun.trycloudflare.com"}
    assert client.get("/api/sessions", headers=host).status_code == 403
    raw = json.dumps(_payload()).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = client.post("/api/github/webhook", content=raw, headers={
        **host, "X-Hub-Signature-256": sig, "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert mgr.reviewed.wait(5)
