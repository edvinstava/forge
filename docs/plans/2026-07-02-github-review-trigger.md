# GitHub Comment-Command Review Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `@acme-forge review` commented on a GitHub PR triggers forge's existing PR-review flow via a signed webhook, with the review posted back as `acme-forge[bot]`.

**Architecture:** A new pure-logic module `ghwebhook.py` (HMAC verification, command parsing, delivery dedup, best-effort GitHub acks), a webhook route + public-host gate attached to the existing FastAPI app, and `forge web --github` CLI wiring that mints a dedicated cloudflared quick tunnel and re-points the App's own webhook config at it on every boot (`PATCH /app/hook/config`).

**Tech Stack:** Python stdlib only (`hmac`, `hashlib`, `secrets`, `collections.deque`) — no new dependencies. FastAPI routes/middleware, existing `GhApp` (injected `http`/`signer`), existing `TunnelManager`, existing `SessionManager.review`.

**Spec:** `docs/specs/2026-07-02-github-review-trigger-design.md` — read it first.

## Global Constraints

- **Never modify `src/forge/store.py` or `tests/test_store.py`** — they carry uncommitted changes from a concurrent session on master; touching them makes the final merge unsafe.
- No new runtime dependencies; stdlib only for the new module.
- All GitHub side-effects that answer a human (reaction, comment) are **best-effort**: wrap in try/except, log at debug, never raise into the engine (same rule as Slack renders).
- Webhook handler must return in well under GitHub's 10s timeout: all heavy work in daemon threads.
- Fail closed: no secret → 503; bad signature → 401.
- Tests are hermetic: no network, no real cloudflared, fakes follow `tests/test_ghapp.py` (`FakeHttp`) and `tests/test_webapp.py` (`FakeManager`, `TestClient`) patterns.
- Test runner: `uv run pytest <file> -q` from the worktree root; full suite `uv run pytest -q` must pass before the final commit.
- Commit after every task with the message given in the task.

---

### Task 1: Config fields for webhook secret and public URL

**Files:**
- Modify: `src/forge/config.py` (dataclass fields near `gh_app_slug` ~line 107; `from_env` entries near `gh_app_slug=` ~line 165)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `Config.gh_webhook_secret: str` (env `FORGE_GH_WEBHOOK_SECRET`, default `""`), `Config.public_url: str` (env `FORGE_PUBLIC_URL`, default `""`). Consumed by Tasks 7–8.

- [ ] **Step 1: Write the failing test** — append to `tests/test_config.py`:

```python
def test_github_webhook_env_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))  # isolate
    monkeypatch.setenv("FORGE_GH_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("FORGE_PUBLIC_URL", "https://forge.example.com")
    from forge.config import Config
    cfg = Config.from_env(tmp_path)
    assert cfg.gh_webhook_secret == "whsec"
    assert cfg.public_url == "https://forge.example.com"


def test_github_webhook_fields_default_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.delenv("FORGE_GH_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FORGE_PUBLIC_URL", raising=False)
    from forge.config import Config
    cfg = Config.from_env(tmp_path)
    assert cfg.gh_webhook_secret == ""
    assert cfg.public_url == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `TypeError` / `AttributeError` (unknown fields).

- [ ] **Step 3: Implement** — in the `Config` dataclass, directly under `gh_app_slug: str = "forge"`:

```python
    gh_webhook_secret: str = ""   # HMAC secret for /api/github/webhook
    public_url: str = ""          # stable public base URL (skips the quick tunnel)
```

and in `from_env`, directly under the `gh_app_slug=...` line:

```python
            gh_webhook_secret=os.environ.get("FORGE_GH_WEBHOOK_SECRET", ""),
            public_url=os.environ.get("FORGE_PUBLIC_URL", ""),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_config.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/config.py tests/test_config.py
git commit -m "feat(config): FORGE_GH_WEBHOOK_SECRET + FORGE_PUBLIC_URL fields"
```

---

### Task 2: ghwebhook core — signature verification, delivery dedup, secret file

**Files:**
- Create: `src/forge/ghwebhook.py`
- Create: `tests/test_ghwebhook.py`

**Interfaces:**
- Produces:
  - `verify_signature(secret: str, body: bytes, header: str) -> bool`
  - `DeliveryLog(maxlen=1024)` with `seen(guid: str) -> bool` (records; True only on repeat)
  - `load_or_create_secret(path: Path) -> str`
  Consumed by Tasks 7–8.

- [ ] **Step 1: Write the failing tests** — create `tests/test_ghwebhook.py`:

```python
import hashlib
import hmac

from forge import ghwebhook


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    body = b'{"a":1}'
    assert ghwebhook.verify_signature("s", body, _sig("s", body)) is True


def test_verify_signature_rejects_wrong_secret_or_body():
    body = b'{"a":1}'
    assert ghwebhook.verify_signature("s", body, _sig("wrong", body)) is False
    assert ghwebhook.verify_signature("s", b'{"a":2}', _sig("s", body)) is False


def test_verify_signature_rejects_missing_or_malformed_header():
    assert ghwebhook.verify_signature("s", b"x", "") is False
    assert ghwebhook.verify_signature("s", b"x", "sha1=abc") is False


def test_verify_signature_fails_closed_without_secret():
    body = b"x"
    assert ghwebhook.verify_signature("", body, _sig("", body)) is False


def test_delivery_log_dedups_and_evicts():
    log = ghwebhook.DeliveryLog(maxlen=2)
    assert log.seen("g1") is False
    assert log.seen("g1") is True          # repeat
    assert log.seen("g2") is False
    assert log.seen("g3") is False         # evicts g1
    assert log.seen("g1") is False         # forgotten after eviction


def test_load_or_create_secret_creates_0600_and_reuses(tmp_path):
    p = tmp_path / "sub" / "webhook.secret"
    s1 = ghwebhook.load_or_create_secret(p)
    assert len(s1) == 64                   # token_hex(32)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert ghwebhook.load_or_create_secret(p) == s1   # stable across boots
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ghwebhook.py -q`
Expected: FAIL — `ModuleNotFoundError: forge.ghwebhook`.

- [ ] **Step 3: Implement** — create `src/forge/ghwebhook.py`:

```python
"""GitHub webhook ingress: comment-command trigger for PR reviews.

Pure logic (HMAC verification, command parsing, delivery dedup) plus thin
best-effort GitHub side-effects that ride an injected GhApp. The FastAPI
route lives in webapp.attach_github_webhook; the CLI wiring in cli._cmd_web.
"""
import hashlib
import hmac
import logging
import secrets as _secrets
from collections import deque
from pathlib import Path

logger = logging.getLogger("forge.ghwebhook")

_API = "https://api.github.com"


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """HMAC-SHA256 over the raw request body vs `X-Hub-Signature-256`.
    Fails closed: empty secret, absent or non-sha256 header all reject."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", header)


class DeliveryLog:
    """Remember recent X-GitHub-Delivery GUIDs — GitHub delivers
    at-least-once, and a redelivered command must not start a second run.
    In-memory on purpose: a daemon restart forgetting old GUIDs at worst
    re-runs one review, and this keeps store.py untouched."""

    def __init__(self, maxlen: int = 1024):
        self._maxlen = maxlen
        self._order: deque = deque()
        self._seen: set = set()

    def seen(self, guid: str) -> bool:
        if guid in self._seen:
            return True
        self._seen.add(guid)
        self._order.append(guid)
        if len(self._order) > self._maxlen:
            self._seen.discard(self._order.popleft())
        return False


def load_or_create_secret(path: Path) -> str:
    """The webhook secret must survive restarts (GitHub keeps the copy we
    PATCH into the App config); generate once, 0600, reuse thereafter."""
    p = Path(path).expanduser()
    try:
        text = p.read_text().strip()
        if text:
            return text
    except OSError:
        pass
    tok = _secrets.token_hex(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok)
    p.chmod(0o600)
    return tok
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ghwebhook.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/ghwebhook.py tests/test_ghwebhook.py
git commit -m "feat(ghwebhook): signature verification, delivery dedup, secret file"
```

---

### Task 3: ghwebhook — parse_command

**Files:**
- Modify: `src/forge/ghwebhook.py`
- Test: `tests/test_ghwebhook.py` (append)

**Interfaces:**
- Produces: `ReviewCommand` dataclass (`owner: str, repo: str, number: int, comment_id: int`, property `slug -> "owner/repo"`), `parse_command(event: str, payload: dict, slug: str) -> ReviewCommand | None`. Consumed by Task 7.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_ghwebhook.py`:

```python
def _payload(body="@acme-forge review", assoc="OWNER", sender="User",
             is_pr=True, action="created"):
    issue = {"number": 7}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": action,
        "issue": issue,
        "comment": {"id": 991, "body": body, "author_association": assoc},
        "sender": {"type": sender},
        "repository": {"full_name": "acme/app"},
    }


def test_parse_command_happy_path():
    cmd = ghwebhook.parse_command("issue_comment", _payload(), "acme-forge")
    assert (cmd.owner, cmd.repo, cmd.number, cmd.comment_id) == \
        ("acme", "app", 7, 991)
    assert cmd.slug == "acme/app"


def test_parse_command_is_case_insensitive_and_mid_comment():
    assert ghwebhook.parse_command(
        "issue_comment", _payload(body="please @Acme-Forge REVIEW this"),
        "acme-forge") is not None


def test_parse_command_rejects_lookalikes():
    for body in ("@acme-forge reviews", "see @acme-forgex review",
                 "acme-forge review", "@acme-forge  please review"):
        assert ghwebhook.parse_command(
            "issue_comment", _payload(body=body), "acme-forge") is None, body


def test_parse_command_gates():
    p = _payload
    cases = [
        ("pull_request", p(), "wrong event"),
        ("issue_comment", p(action="edited"), "edited not created"),
        ("issue_comment", p(is_pr=False), "plain issue"),
        ("issue_comment", p(sender="Bot"), "bot sender"),
        ("issue_comment", p(assoc="NONE"), "no association"),
        ("issue_comment", p(assoc="CONTRIBUTOR"), "contributor insufficient"),
        ("issue_comment", p(body="lgtm!"), "no command"),
    ]
    for event, payload, why in cases:
        assert ghwebhook.parse_command(event, payload, "acme-forge") is None, why


def test_parse_command_allows_member_and_collaborator():
    for assoc in ("MEMBER", "COLLABORATOR"):
        assert ghwebhook.parse_command(
            "issue_comment", _payload(assoc=assoc), "acme-forge") is not None


def test_parse_command_never_raises_on_malformed_payload():
    assert ghwebhook.parse_command("issue_comment", {}, "acme-forge") is None
    assert ghwebhook.parse_command(
        "issue_comment", {"action": "created", "issue": None}, "acme-forge") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ghwebhook.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'parse_command'`.

- [ ] **Step 3: Implement** — add to `src/forge/ghwebhook.py` (after the imports, add `import re` and `from dataclasses import dataclass`):

```python
_ASSOC_OK = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass(frozen=True)
class ReviewCommand:
    owner: str
    repo: str
    number: int
    comment_id: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_command(event: str, payload: dict, slug: str):
    """A ReviewCommand iff this delivery is a freshly created PR comment by a
    non-bot with owner/member/collaborator association whose body mentions
    `@{slug} review`. Anything else — including malformed payloads — is None,
    never an exception (webhooks are untrusted input)."""
    if event != "issue_comment":
        return None
    try:
        if payload.get("action") != "created":
            return None
        issue = payload["issue"]
        if "pull_request" not in issue:
            return None
        if payload["sender"]["type"] == "Bot":
            return None
        comment = payload["comment"]
        if comment.get("author_association") not in _ASSOC_OK:
            return None
        pat = re.compile(rf"(?:^|\s)@{re.escape(slug)}\s+review\b", re.I)
        if not pat.search(comment.get("body") or ""):
            return None
        owner, repo = payload["repository"]["full_name"].split("/", 1)
        return ReviewCommand(owner, repo, int(issue["number"]),
                             int(comment["id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
```

Note: `"@acme-forge  please review"` must NOT match — the regex requires `review` immediately after the mention (single whitespace run). `\s+` matches multiple spaces, so "@acme-forge  review" (double space) DOES match — that's intended; only intervening *words* break the command.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ghwebhook.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/ghwebhook.py tests/test_ghwebhook.py
git commit -m "feat(ghwebhook): parse @slug review comment-commands with permission gates"
```

---

### Task 4: ghwebhook — best-effort reaction ack and PR comment

**Files:**
- Modify: `src/forge/ghwebhook.py`
- Test: `tests/test_ghwebhook.py` (append)

**Interfaces:**
- Consumes: `GhApp.installation_token(owner, repo) -> str`, `GhApp.http(method, url, token, data) -> dict` (existing).
- Produces: `ack_comment(app, owner, repo, comment_id) -> None`, `post_comment(app, owner, repo, number, body) -> None`. Consumed by Task 7.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_ghwebhook.py`:

```python
class FakeGhApp:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def installation_token(self, owner, repo):
        if self.fail:
            raise RuntimeError("no installation")
        return "ghs_tok"

    def http(self, method, url, token, data=None):
        self.calls.append((method, url, token, data))
        return {}


def test_ack_comment_posts_eyes_reaction():
    app = FakeGhApp()
    ghwebhook.ack_comment(app, "o", "r", 991)
    m, url, tok, data = app.calls[0]
    assert m == "POST"
    assert url.endswith("/repos/o/r/issues/comments/991/reactions")
    assert tok == "ghs_tok" and data == {"content": "eyes"}


def test_post_comment_targets_issue_number():
    app = FakeGhApp()
    ghwebhook.post_comment(app, "o", "r", 7, "hello")
    m, url, tok, data = app.calls[0]
    assert m == "POST" and url.endswith("/repos/o/r/issues/7/comments")
    assert data == {"body": "hello"}


def test_ack_and_comment_swallow_errors():
    app = FakeGhApp(fail=True)
    ghwebhook.ack_comment(app, "o", "r", 1)     # must not raise
    ghwebhook.post_comment(app, "o", "r", 1, "x")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ghwebhook.py -q`
Expected: FAIL — `AttributeError: ... 'ack_comment'`.

- [ ] **Step 3: Implement** — append to `src/forge/ghwebhook.py`:

```python
def ack_comment(app, owner: str, repo: str, comment_id: int) -> None:
    """👀 on the command comment: 'forge heard you'. Best-effort — an ack
    must never abort the run (same rule as Slack renders)."""
    try:
        tok = app.installation_token(owner, repo)
        app.http("POST",
                 f"{_API}/repos/{owner}/{repo}/issues/comments/"
                 f"{comment_id}/reactions",
                 tok, {"content": "eyes"})
    except Exception:
        logger.debug("reaction ack failed for %s/%s#c%s",
                     owner, repo, comment_id, exc_info=True)


def post_comment(app, owner: str, repo: str, number: int, body: str) -> None:
    """Best-effort PR conversation comment (capacity refusals, failure
    notices). Failures are logged and dropped."""
    try:
        tok = app.installation_token(owner, repo)
        app.http("POST", f"{_API}/repos/{owner}/{repo}/issues/{number}/comments",
                 tok, {"body": body})
    except Exception:
        logger.debug("comment post failed for %s/%s#%s",
                     owner, repo, number, exc_info=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ghwebhook.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/ghwebhook.py tests/test_ghwebhook.py
git commit -m "feat(ghwebhook): best-effort reaction ack + PR comment helpers"
```

---

### Task 5: GhApp.update_webhook_config

**Files:**
- Modify: `src/forge/ghapp.py` (add method to `GhApp`, after `bot_identity`)
- Test: `tests/test_ghapp.py` (append)

**Interfaces:**
- Produces: `GhApp.update_webhook_config(url: str, secret: str) -> None`. Consumed by Task 8.

- [ ] **Step 1: Write the failing test** — append to `tests/test_ghapp.py`. The existing `FakeHttp` raises `AssertionError(url)` on unknown URLs — extend it first by adding a `/app/hook/config` branch **inside `FakeHttp.__call__`**, before the final `raise`:

```python
        if url.endswith("/app/hook/config"):
            return {"url": (data or {}).get("url", "")}
```

then append the test:

```python
def test_update_webhook_config_patches_with_jwt(tmp_path):
    http = FakeHttp()
    app = ghapp.GhApp(_cfg(tmp_path), signer=lambda *a: "JWT", http=http,
                      clock=lambda: 1000.0)
    app.update_webhook_config("https://x.trycloudflare.com/api/github/webhook",
                              "whsec")
    m, url, tok, data = http.calls[-1]
    assert (m, url, tok) == ("PATCH", "https://api.github.com/app/hook/config",
                             "JWT")
    assert data == {"url": "https://x.trycloudflare.com/api/github/webhook",
                    "content_type": "json", "secret": "whsec"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ghapp.py -q`
Expected: FAIL — `AttributeError: 'GhApp' ... 'update_webhook_config'`.

- [ ] **Step 3: Implement** — add to `GhApp` in `src/forge/ghapp.py`:

```python
    def update_webhook_config(self, url: str, secret: str) -> None:
        """Re-point the App's own webhook (PATCH /app/hook/config, JWT-authed).
        Quick tunnels rotate hostnames every boot; the App rewriting its own
        webhook config is what makes them viable — no manual settings edit."""
        self.http("PATCH", f"{_API}/app/hook/config", self._jwt(),
                  {"url": url, "content_type": "json", "secret": secret})
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ghapp.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/ghapp.py tests/test_ghapp.py
git commit -m "feat(ghapp): App rewrites its own webhook config (PATCH /app/hook/config)"
```

---

### Task 6: webapp — public-host gate middleware

**Files:**
- Modify: `src/forge/webapp.py`
- Create: `tests/test_webapp_github.py`

**Interfaces:**
- Produces: `public_request_allowed(host: str, method: str, path: str, public_host: str) -> bool` and `attach_public_gate(app, public_host: str) -> None`. Consumed by Task 8.

- [ ] **Step 1: Write the failing tests** — create `tests/test_webapp_github.py`:

```python
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
    return TestClient(app)


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
    # no public host configured => gate inert
    assert public_request_allowed(pub, "GET", "/api/sessions", "")


def test_gate_blocks_tunnel_host_off_webhook(tmp_path):
    client = _gated_client(tmp_path)
    r = client.get("/api/sessions", headers={"Host": "tun.trycloudflare.com"})
    assert r.status_code == 403


def test_gate_leaves_local_traffic_alone(tmp_path):
    client = _gated_client(tmp_path)
    assert client.get("/api/sessions").status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_webapp_github.py -q`
Expected: FAIL — `ImportError: cannot import name 'attach_public_gate'`.

- [ ] **Step 3: Implement** — add to `src/forge/webapp.py` (module level, near `tunnel_reconcile`):

```python
def public_request_allowed(host: str, method: str, path: str,
                           public_host: str) -> bool:
    """The public (tunnel) hostname may only reach the GitHub webhook.
    Everything else the tunnel could expose is the unauthenticated forge
    API — sessions, stop, batch — so requests arriving under the public
    Host are refused wholesale. Local hosts are unaffected."""
    h = (host or "").split(":", 1)[0].lower()
    if not public_host or h != public_host.lower():
        return True
    return method.upper() == "POST" and path == "/api/github/webhook"


def attach_public_gate(app, public_host: str) -> None:
    @app.middleware("http")
    async def _gate(request, call_next):
        if not public_request_allowed(request.headers.get("host", ""),
                                      request.method, request.url.path,
                                      public_host):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_webapp_github.py -q` — Expected: PASS.
Also run: `uv run pytest tests/test_webapp.py -q` — Expected: PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add src/forge/webapp.py tests/test_webapp_github.py
git commit -m "feat(webapp): public-host gate — tunnel Host reaches only the GitHub webhook"
```

---

### Task 7: webapp — /api/github/webhook route

**Files:**
- Modify: `src/forge/webapp.py`
- Test: `tests/test_webapp_github.py` (append)

**Interfaces:**
- Consumes: Task 2 (`verify_signature`, `DeliveryLog`), Task 3 (`parse_command`), Task 4 (`ack_comment`, `post_comment`); `manager.can_start()`, `manager.review(run_id, "owner/repo#N", model, origin=...)` (existing generator flow); `cfg.gh_app_slug`.
- Produces: `attach_github_webhook(app, cfg, manager, ghapp_client, secret, delivery_log=None) -> None`. Consumed by Task 8.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_webapp_github.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_webapp_github.py -q`
Expected: FAIL — `ImportError: cannot import name 'attach_github_webhook'`.

- [ ] **Step 3: Implement** — add to `src/forge/webapp.py` (module level, after `attach_public_gate`; `json`, `uuid`, `Request`, `JSONResponse` are already imported at the top):

```python
def attach_github_webhook(app, cfg, manager, ghapp_client, secret,
                          delivery_log=None) -> None:
    """POST /api/github/webhook: the @<slug> review comment-command trigger.
    Fast-ack (GitHub times out at 10s; provisioning takes minutes): all real
    work — reaction ack, review run, failure comment — happens on a daemon
    thread. Events still reach the bus via @published, so the run is watchable
    live in the web UI like any other."""
    import threading
    from forge import ghwebhook

    log = delivery_log or ghwebhook.DeliveryLog()

    def _run_review(cmd, run_id):
        ghwebhook.ack_comment(ghapp_client, cmd.owner, cmd.repo, cmd.comment_id)
        failed = None
        try:
            for ev in manager.review(run_id, f"{cmd.slug}#{cmd.number}",
                                     "auto", origin="github"):
                if ev.kind == "error":
                    failed = ev.data.get("detail") or ev.data.get("kind") or "error"
        except Exception as e:  # noqa: BLE001 - thread boundary: report, don't die
            logger.exception("github-triggered review crashed (run %s)", run_id)
            failed = str(e)[:200]
        if failed:
            ghwebhook.post_comment(
                ghapp_client, cmd.owner, cmd.repo, cmd.number,
                f"⚠️ forge review failed: {failed}")

    @app.post("/api/github/webhook")
    async def github_webhook(req: Request):
        body = await req.body()
        if not secret:
            return JSONResponse({"error": "webhook secret not configured"},
                                status_code=503)
        if not ghwebhook.verify_signature(
                secret, body, req.headers.get("X-Hub-Signature-256", "")):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        event = req.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return {"ok": True}
        guid = req.headers.get("X-GitHub-Delivery", "")
        if guid and log.seen(guid):
            return {"duplicate": True}
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        cmd = ghwebhook.parse_command(event, payload, cfg.gh_app_slug)
        if cmd is None:
            return {"ignored": True}
        ok, msg = manager.can_start()
        if not ok:
            threading.Thread(
                target=ghwebhook.post_comment,
                args=(ghapp_client, cmd.owner, cmd.repo, cmd.number,
                      f"⏳ forge is at capacity ({msg}) — try again shortly."),
                daemon=True).start()
            return {"accepted": False, "reason": "capacity"}
        run_id = uuid.uuid4().hex
        threading.Thread(target=_run_review, args=(cmd, run_id),
                         daemon=True).start()
        return {"accepted": True, "run_id": run_id}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_webapp_github.py tests/test_webapp.py tests/test_webapp_events.py tests/test_webapp_batch.py -q` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/webapp.py tests/test_webapp_github.py
git commit -m "feat(webapp): /api/github/webhook — @slug review comment-command trigger"
```

---

### Task 8: CLI — `forge web --github`

**Files:**
- Modify: `src/forge/cli.py` (`_cmd_web` ~line 174, and the `web` subparser ~line 309)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: Task 1 (`cfg.gh_webhook_secret`, `cfg.public_url`), Task 2 (`load_or_create_secret`), Task 5 (`update_webhook_config`), Task 6 (`attach_public_gate`), Task 7 (`attach_github_webhook`), existing `TunnelManager`, `ghapp.is_configured`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py` (mirror the `--slack` tests at lines 62–81):

```python
def test_web_github_requires_app_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    monkeypatch.delenv("FORGE_GH_APP_ID", raising=False)
    monkeypatch.delenv("FORGE_GH_APP_KEY", raising=False)
    rc = main(["web", "--github", "--no-open", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "FORGE_GH_APP_ID" in capsys.readouterr().err


def test_web_github_rejects_reload(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    rc = main(["web", "--github", "--reload", "--no-open",
               "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "reload" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -q`
Expected: the two new tests FAIL (`--github` is an unrecognized argument).

- [ ] **Step 3: Implement** — three edits in `src/forge/cli.py`:

(a) Subparser — after the `--slack` argument of the `web` subparser:

```python
    webp.add_argument("--github", action="store_true",
                      help="accept @<app-slug> review PR comment-commands via a "
                           "GitHub App webhook (needs FORGE_GH_APP_ID/KEY)")
```

(b) Early validation in `_cmd_web` — directly after the existing `if args.slack:` validation block (before `import uvicorn`):

```python
    if args.github:
        if args.reload:
            print("error: --github cannot be combined with --reload",
                  file=sys.stderr)
            return 1
        from forge import ghapp as ghappmod
        if not ghappmod.is_configured(cfg):
            print("error: --github needs FORGE_GH_APP_ID and FORGE_GH_APP_KEY "
                  "(and the key file present)", file=sys.stderr)
            return 1
```

(c) Wiring — in `_cmd_web`, after the `if args.slack:` wiring block (right before `uvicorn.run(app, ...)`):

```python
    if args.github:
        from urllib.parse import urlparse
        from forge import ghapp as ghappmod, ghwebhook
        from forge.webapp import attach_github_webhook, attach_public_gate
        secret = cfg.gh_webhook_secret or ghwebhook.load_or_create_secret(
            Path.home() / ".forge" / "webhook.secret")
        public = cfg.public_url
        if not public:
            from forge import tunnel as tunnelmod
            # Dedicated instance: the per-run TunnelManager is swept by
            # tunnel_sweep, which reaps any tunnel whose id isn't a live env —
            # a shared instance would kill this tunnel within 30s.
            wh_tunnel = tunnelmod.TunnelManager(probe=tunnelmod.http_probe)
            public = wh_tunnel.start("github-webhook",
                                     f"http://127.0.0.1:{args.port}")
        if not public:
            print("error: --github needs a public URL; cloudflared quick "
                  "tunnel failed (install cloudflared or set FORGE_PUBLIC_URL)",
                  file=sys.stderr)
            return 1
        public = public.rstrip("/")
        hook_url = f"{public}/api/github/webhook"
        gh = ghappmod.GhApp(cfg)
        try:
            gh.update_webhook_config(hook_url, secret)
            print(f"forge github webhook → {hook_url}  (App config updated)")
        except Exception as e:  # noqa: BLE001 - startup boundary: degrade, don't die
            print(f"warning: could not update the App webhook config ({e}); "
                  f"set it manually in the App settings — URL {hook_url}, "
                  f"secret in ~/.forge/webhook.secret", file=sys.stderr)
        attach_public_gate(app, urlparse(public).hostname or "")
        attach_github_webhook(app, cfg, manager, gh, secret)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/cli.py tests/test_cli.py
git commit -m "feat(cli): forge web --github — webhook tunnel + self-updating App config"
```

---

### Task 9: README section + full-suite verification

**Files:**
- Modify: `README.md` (find the existing GitHub App section — `grep -n "GitHub App" README.md` — and append after it; if none exists, add before any "development"/"testing" section)

**Interfaces:** none (docs + verification only).

- [ ] **Step 1: Add the README section** (adjust heading level to match neighbors):

```markdown
## Review PRs from GitHub (`@<app-slug> review`)

With the forge GitHub App installed, a repo owner/member/collaborator can
comment `@acme-forge review` on any pull request and forge reviews it,
posting findings as `acme-forge[bot]` — same output as `forge review
owner/repo#N`.

Run the daemon with the webhook enabled:

    forge web --slack --github

On boot forge starts a dedicated cloudflared quick tunnel (or uses
`FORGE_PUBLIC_URL` if set), then re-points the App's webhook at itself
(`PATCH /app/hook/config`) — quick-tunnel hostname rotation needs no manual
step. The HMAC secret persists in `~/.forge/webhook.secret`
(`FORGE_GH_WEBHOOK_SECRET` overrides). The public hostname is gated to the
webhook path only; the rest of the forge API stays local.

One-time App settings (github.com → Settings → Developer settings → GitHub
Apps → your app):

- **Webhook**: set Active (any placeholder URL — forge overwrites URL and
  secret on every boot).
- **Subscribe to events**: Issue comment.
- **Permissions**: Pull requests Read & write (already required for posting
  reviews); Issues Read (needed for issue_comment delivery; Write only if
  you want the 👀 ack reaction and capacity/failure comments — both are
  best-effort and silently skipped without it).

Commands are accepted only from comments by users whose
`author_association` is OWNER/MEMBER/COLLABORATOR; bot comments and
edited comments are ignored, and redelivered webhooks are deduped.
```

- [ ] **Step 2: Full test suite**

Run: `uv run pytest -q`
Expected: all tests pass, zero failures. If anything unrelated fails, STOP and report — do not "fix" unrelated tests.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): GitHub @slug review comment-command setup"
```

---

## Post-plan verification (performed by the dispatching session, not the implementer)

1. Merge review of the branch (code-review pass).
2. Live check after merge: `forge web --github`, confirm boot prints the hook URL and App-config update, comment `@acme-forge review` on a real PR, watch the 👀 ack and the posted review.
