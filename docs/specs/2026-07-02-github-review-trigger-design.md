# GitHub comment-command review trigger — design

**Date:** 2026-07-02
**Status:** approved (direction pre-approved by user; details fixed in this spec)

## Goal

"Ask forge to review this PR" from inside GitHub. A repo collaborator comments
`@acme-forge review` on a pull request; forge receives the `issue_comment`
webhook, acks with a 👀 reaction, runs the existing PR-review flow
(`SessionManager.review`), and the findings land on the PR as an
`acme-forge[bot]` review — the same output as `forge review owner/repo#N` or
`POST /api/review`, but triggered from GitHub itself. This is the standard
comment-command pattern used by third-party review bots.

## Approaches considered

1. **Webhook comment-command (chosen).** Real-time, standard pattern, uses the
   GitHub App forge already has. Needs a public URL — solved below.
2. **Polling PR comments/notifications.** No public ingress needed, but laggy,
   burns rate limit, and needs cursor state per repo. Rejected.
3. **smee.io relay.** Stable webhook URL without a tunnel, but routes private
   repo events through a third-party relay. Rejected.

The classic objection to (1) with cloudflared *quick* tunnels is that the
hostname changes on every restart. GitHub Apps can rewrite their own webhook
configuration (`PATCH /app/hook/config`, JWT-authenticated), so forge
re-points the webhook at its fresh tunnel URL on every boot. No manual step,
no fixed infrastructure.

## Architecture

```
GitHub (issue_comment webhook)
   │  POST https://<random>.trycloudflare.com/api/github/webhook
   ▼
cloudflared quick tunnel (dedicated instance, NOT the per-run TunnelManager)
   │  → http://127.0.0.1:8099
   ▼
forge web (FastAPI)
   ├─ public-host gate: tunnel Host may ONLY reach POST /api/github/webhook
   ├─ /api/github/webhook: verify HMAC → dedup delivery → parse command
   │     → permission gate → capacity gate → 200 fast
   └─ daemon thread: 👀 reaction ack → drain manager.review(run_id, "o/r#N",
         "auto", origin="github") → on error, best-effort PR comment
```

Events published by `@published` on `review()` flow to the shared EventBus, so
the run is watchable live in the web UI (`/#s=<run_id>`) like any other run.

## Components

### 1. `src/forge/ghwebhook.py` (new — pure logic + thin GhApp helpers)

- `verify_signature(secret: str, body: bytes, header: str) -> bool` —
  HMAC-SHA256 over the **raw** request body, compared against
  `X-Hub-Signature-256: sha256=<hex>` with `hmac.compare_digest`. Empty/absent
  header or malformed prefix → False. Empty secret → False (fail closed).
- `@dataclass ReviewCommand: owner, repo, number, comment_id` (slug property,
  mirroring `prref.PRRef`).
- `parse_command(event: str, payload: dict, slug: str) -> ReviewCommand | None`
  Returns a command only when ALL hold:
  - `event == "issue_comment"` and `payload["action"] == "created"`
    (not `edited`/`deleted` — no re-fires on edits);
  - the issue is a PR (`payload["issue"]["pull_request"]` present);
  - `payload["sender"]["type"] != "Bot"` (no self/bot trigger loops);
  - `payload["comment"]["author_association"]` in
    `{"OWNER", "MEMBER", "COLLABORATOR"}` (drive-by users can't burn compute);
  - the body matches `(?i)(?:^|\s)@{slug}\s+review\b` — mention of the App
    slug (`cfg.gh_app_slug`, e.g. `acme-forge`), case-insensitive; trailing
    text after `review` is ignored in v1.
  Anything else → None. Malformed payloads (missing keys) → None, never raise.
- `class DeliveryLog(maxlen=1024)` — `seen(guid) -> bool` recording
  `X-GitHub-Delivery` GUIDs (set + deque eviction). GitHub redelivers
  at-least-once; in-memory is enough (a redelivery after a daemon restart just
  re-runs one review — harmless, and avoids touching store.py).
- `load_or_create_secret(path: Path) -> str` — read the webhook secret from
  `path`, else generate (`secrets.token_hex(32)`), write with mode 0600, and
  return it. Env `FORGE_GH_WEBHOOK_SECRET` overrides the file entirely.
- `ack_comment(app: GhApp, owner, repo, comment_id)` — POST
  `/repos/{o}/{r}/issues/comments/{id}/reactions` `{"content": "eyes"}` with an
  installation token. **Best-effort**: any exception is swallowed (same rule as
  Slack renders — an ack must never abort a turn).
- `post_comment(app: GhApp, owner, repo, number, body)` — POST
  `/repos/{o}/{r}/issues/{n}/comments`, best-effort; used for capacity refusal
  and run-failure notices.

### 2. `src/forge/ghapp.py` — one new method

- `GhApp.update_webhook_config(url: str, secret: str) -> None` —
  `PATCH /app/hook/config` with the App JWT, body
  `{"url": url, "content_type": "json", "secret": secret}`. Uses the injected
  `self.http`, so tests stay hermetic.

### 3. `src/forge/webapp.py` — attach functions (same pattern as `attach_background`)

- `attach_github_webhook(app, cfg, manager, ghapp, secret, delivery_log=None)`
  registers `POST /api/github/webhook`:
  1. `body = await req.body()`; missing/invalid signature → **401**. No secret
     configured → **503** (fail closed; should be unreachable given CLI wiring).
  2. `X-GitHub-Event == "ping"` → 200 `{"ok": true}` (GitHub sends it on
     config changes — must succeed).
  3. Duplicate `X-GitHub-Delivery` → 200 `{"duplicate": true}`.
  4. `parse_command(...)` → None → 200 `{"ignored": true}` (wrong event type,
     not a PR, no command, bot sender, insufficient association — all silent).
  5. `manager.can_start()` false → best-effort `post_comment` ("⏳ forge is at
     capacity — try again shortly") **in a thread**, 200 `{"accepted": false}`.
  6. Else mint `run_id = uuid4().hex`, spawn a daemon thread:
     `ack_comment(...)` then drain
     `manager.review(run_id, f"{owner}/{repo}#{number}", "auto",
     origin="github")`; if a terminal `error` TurnEvent (or an exception)
     occurs, best-effort `post_comment` with a one-line failure notice
     (the happy path needs no comment — the posted review IS the response).
     Return 200 `{"accepted": true, "run_id": run_id}` immediately (GitHub's
     10s timeout; provisioning takes minutes).
- `attach_public_gate(app, public_host: str)` — HTTP middleware: requests whose
  `Host` header (port stripped) equals `public_host` may only reach
  `POST /api/github/webhook`; everything else on that host → **403**. Local
  traffic (`localhost:8099` etc.) is untouched. This keeps the tunnel from
  exposing the whole unauthenticated forge API (sessions, stop, batch, …).
  Gate logic is a pure function `public_request_allowed(host, method, path,
  public_host) -> bool` for direct testing.

### 4. `src/forge/cli.py` — `forge web --github`

New flag, composable with `--slack` (independent of it). Incompatible with
`--reload` (same as `--slack`). Startup sequence:

1. `ghapp.is_configured(cfg)` false → exit 1: `--github needs FORGE_GH_APP_ID
   and FORGE_GH_APP_KEY (and the key file present)`.
2. Resolve secret: `cfg.gh_webhook_secret` (env) or
   `load_or_create_secret(~/.forge/webhook.secret)`.
3. Resolve public base URL: `cfg.public_url` (env `FORGE_PUBLIC_URL`, for
   users running stable named tunnels/ingress) — else start a **dedicated**
   `TunnelManager(probe=http_probe)` instance with a sentinel id (e.g.
   `"github-webhook"`) targeting `http://127.0.0.1:{port}`. This instance is
   NOT passed to `attach_tunnel_lifecycle`, so `tunnel_sweep` (which reaps any
   tunnel whose id isn't a live env run) can't kill it. No URL obtainable
   (cloudflared missing / all attempts fail) → exit 1 with a clear message —
   an explicitly requested `--github` must not half-start silently.
4. `GhApp(cfg).update_webhook_config(f"{public}/api/github/webhook", secret)`.
   On failure: **warn and continue** (print the URL and the secret file path
   with manual App-settings instructions — the endpoint still works once the
   user pastes the config; never print the secret value).
5. `attach_public_gate(app, <public host>)` + `attach_github_webhook(...)`.
6. Print `forge github webhook → <public>/api/github/webhook`.

### 5. `src/forge/config.py` — two fields

- `gh_webhook_secret: str = ""` ← `FORGE_GH_WEBHOOK_SECRET`
- `public_url: str = ""` ← `FORGE_PUBLIC_URL`

### 6. GitHub App settings (manual, one-time — documented in README section)

- Subscribe to **Issue comment** events (and optionally **Pull request** for
  future auto-review-on-open; v1 ignores those payloads gracefully).
- Ensure **Issues: Read** permission (required to receive `issue_comment`;
  write on Issues only if reaction/comment posting 403s — both are
  best-effort, so nothing breaks without it; **Pull requests: Read & write**
  already granted for posting reviews).
- Webhook: set Active with any placeholder URL — forge overwrites URL+secret
  on every boot.

## Explicitly out of scope (v1)

- `pull_request` event handling (auto-review on open/synchronize) — payloads
  accepted and ignored with 200.
- Command arguments (`model=`, focus areas, extra instructions after `review`).
- Queueing review requests when at capacity (store queue is task-run-shaped).
- Slack mirroring of GitHub-triggered runs beyond what the EventBus already
  provides.
- **No store.py / schema changes** (also keeps clear of the dirty files on
  master).

## Error handling summary

| Condition | Response |
|---|---|
| Bad/absent HMAC signature | 401, no processing |
| No secret configured on server | 503 (fail closed) |
| `ping` event | 200 ok |
| Redelivered GUID | 200 duplicate, no re-run |
| Not a command (any reason) | 200 ignored |
| At capacity | 200 accepted:false + best-effort PR comment |
| Review run fails mid-flight | best-effort PR comment with one-line notice |
| Reaction/comment API failures | swallowed (never abort the run) |
| Tunnel unobtainable at boot | exit 1 |
| `PATCH /app/hook/config` fails at boot | warn + manual instructions, continue |

## Testing

Hermetic throughout (fake GhApp `http`, fake signer, `TestClient`, FakeManager
extended with a recording `review()`), following existing `test_ghapp.py` /
`test_webapp.py` patterns:

- **ghwebhook unit**: signature valid/invalid/missing/malformed/empty-secret;
  `parse_command` matrix (created vs edited, PR vs plain issue, each
  author_association, bot sender, slug case-insensitivity, mention embedded
  mid-comment, `review` as prefix of another word rejected); DeliveryLog dedup
  + eviction at maxlen; secret file created 0600 / reused / env override.
- **ghapp unit**: `update_webhook_config` sends PATCH to `/app/hook/config`
  with JWT and exact payload.
- **webapp integration**: signed command comment → 200 accepted + FakeManager
  records `review("owner/repo#N", origin="github")` (thread synchronized via
  `threading.Event` set inside the fake); bad signature → 401; dup delivery →
  no second review; ping → 200; non-command comment → ignored; at capacity →
  no review + comment recorded; error TurnEvent → failure comment recorded;
  public gate: tunnel Host on `/api/sessions` → 403, on webhook POST → passes,
  local host unaffected.
- **cli**: `--github` without App config → exit 1; `--github --reload` → exit 1.
- **Live verification** (manual, post-merge): boot `forge web --github`,
  confirm webhook config updated in App settings, comment `@acme-forge
  review` on a real PR, watch the review land.
