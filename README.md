# Forge

**Autonomous software engineering, running on your own machine.**
(Yes, it's *Edvin, not Devin*.)

Point it at a GitHub repo and a task from Slack or a web chat; it plans the change,
brings the app up in a disposable Docker environment, edits the code, verifies it
(tests / lint / build), browser-tests the result with screenshots, and opens a pull
request that reads like a colleague's — all on **your Claude or ChatGPT
subscription**, never a metered API key. The fixed app is left running at a
clickable URL so you can see the change live.

> **What this is.** A hobby project I built for my own use, to learn and play with
> Slack integrations, GitHub Apps and webhooks, spinning apps up in isolation
> inside Docker containers, Cloudflare tunnels, and driving AI agent CLIs — not a
> product, and largely vibe-coded. It's single-user, local, and low-ceremony: a
> power tool for one developer's laptop, not a multi-tenant platform. Read the
> [security model](#security-model) before pointing it at repositories you don't
> trust, and use it at your own risk.

---

## What it does

```
you: "fix the off-by-one in the date picker on the webapp repo"
  │
  ├─ plan the change            (optional approval gate)
  ├─ stand up the stack         disposable docker compose project, app + worker
  ├─ edit + hot-reload          the agent works inside the container
  ├─ verify                     runs the repo's own tests/lint/build; repairs until green
  ├─ browser QA                 drives the running app, attaches before/after screenshots
  ├─ self-review + fix          forge reviews its own diff before shipping
  ├─ open a PR                  with an agent-written title + body
  └─ learn                      records durable per-repo lessons for next time
```

- **Any stack, understood by AI.** It detects how to bring a repo up from a
  deterministic recipe chain, and an AI probe fills the gaps (see [Recipes](#recipes-how-a-repos-stack-comes-up)).
- **Plan → build → verify → repair**, Devin-style: the orchestrator owns the
  pass/fail verdict; the agent only reports. It won't open a non-draft PR it couldn't
  actually verify.
- **Gets better as it goes.** After every PR a retrospective records env quirks, build
  gotchas and conventions into a per-repo knowledge overlay that future runs read.
- **Claude or OpenAI**, subscription-first — your plan, not surprise API billing.

---

## Prerequisites

- **Docker** with Compose v2 (`docker compose version`)
- **Python ≥ 3.11**
- **`gh` CLI**, authenticated (`gh auth login`) — used to clone, push, and open PRs
- An agent CLI for your provider:
  - Claude Code (`claude`) — `claude setup-token`, or
  - OpenAI Codex (`codex`) — `codex login`
- Optional: **`cloudflared`** (public URLs for Slack), **Supabase CLI** (the
  `next-supabase` recipe), **Node ≥ 18** (only if you modify the web UI — the built
  `web/dist` is committed)

---

## Install

```bash
git clone https://github.com/edvinstava/forge && cd forge

# 1. Install forge with the extras you want (web UI + Slack shown here).
pip install -e ".[web,slack]"        # or: uv sync --extra web --extra slack

# 2. Build the worker image (the container the agent runs in).
#    Heads up: it bundles node, git, gh, the claude + codex CLIs, and
#    Playwright + Chromium — the first build is several minutes and ~2 GB.
docker build -t forge-worker worker-image/

# 3. Authenticate — your subscription, no API key.
export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)
export GH_TOKEN=$(gh auth token)
```

**Tip: keep config in a file.** Instead of exporting variables every shell, put them
in `~/.forge/config.env` (`KEY=value` lines) — forge loads it automatically, and the
environment always wins over the file. `chmod 600` it, since it holds tokens.

---

## Quick start — web UI

```bash
forge web                     # → http://127.0.0.1:8099  (opens your browser on macOS)
forge web --port 8099 --no-open
```

Pick a repo (from your workspace folder or by pasting an `owner/repo` slug), type a
task, and watch the agent stream its work. Open the PR on demand with the **Open PR**
button — it does not happen automatically. Sessions run concurrently up to
`FORGE_MAX_SESSIONS` (default 4); each gets its own warm Docker environment and a
clickable URL once the app is healthy.

The `forge web` process also runs the Caddy proxy and idle reaper in-process, so you
don't need a separate `forge serve`.

## Quick start — one-shot CLI

```bash
forge run owner/repo "fix the off-by-one in the date picker"
#   → proposes a plan and pauses for approval, then clones, stands up the
#     stack, fixes it, verifies, opens a PR, and prints: app: http://localhost:<port>

forge run owner/repo "…" --yes      # skip the plan gate (fire-and-forget)
forge run owner/repo "…" --no-plan  # don't propose a plan at all
forge status                        # list live environments + URLs
forge down <run_id>                 # tear an environment down now
forge attach <run_id>               # answer an open checkpoint from the terminal
```

The one-shot path keeps a single live environment: a new `forge run` supersedes and
reaps the previous one. (Concurrent sessions are a `forge web` feature.)

## Slack bot

Drive it from a Slack DM or channel: *"fix the date picker on the landing repo"* →
live progress in-thread, before/after screenshots, a public URL to the running app,
and an **Open PR** button. Uses **Socket Mode**, so there's no inbound endpoint to
expose.

**One-time Slack app setup:**

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps) → enable
   **Socket Mode** (generate an App-level token with `connections:write` →
   `SLACK_APP_TOKEN=xapp-…`).
2. **OAuth & Permissions → Bot Token Scopes:**
   - DMs: `chat:write`, `files:read`, `files:write`, `im:history`, `im:read`, `im:write`
   - Channels (optional): also add `app_mentions:read`, `channels:history`,
     `groups:history`
   - `files:write` lets the bot attach screenshots; `files:read` lets it receive
     image attachments — without them uploads/downloads fail with `missing_scope`.

   Install to your workspace → `SLACK_BOT_TOKEN=xoxb-…`.
3. **Event Subscriptions → Subscribe to bot events:** `message.im` (DMs); add
   `app_mention` and `message.channels` for channels.
4. Find your Slack user id (Profile → ⋯ → Copy member ID) → `SLACK_ALLOWED_USER=U…`.
   The bot answers only you and ignores everyone else.

```bash
brew install cloudflared          # optional: public URLs (falls back to localhost)
forge web --slack                 # needs SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_ALLOWED_USER
```

In a channel, `@forge` to start and reply in-thread to continue. See
[`docs/slack-channel-setup.md`](docs/slack-channel-setup.md) for the channel details.

**Repo aliases (optional):** create `~/.forge/aliases.yml` with `shorthand: owner/repo`
lines for repos whose names aren't guessable; otherwise forge fuzzy-matches your
push-access repos and asks when ambiguous.

```yaml
app: acme/webapp
lp:  acme/landing-page
```

---

## Providers & billing

An agent CLI runs *inside* the worker container and always prefers your plan
over metered API usage. Only the active provider's credential enters the container.

| Provider | `FORGE_PROVIDER` | Auth | Models |
|---|---|---|---|
| **Claude Code** (default) | `claude` | `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) — no `ANTHROPIC_API_KEY` is ever passed in | `auto \| opus \| sonnet \| haiku` |
| **OpenAI Codex** | `codex` | a ChatGPT-plan `codex login` (forge mounts `~/.codex` and suppresses `OPENAI_API_KEY` so usage bills the plan), or `OPENAI_API_KEY` as fallback (`FORGE_CODEX_AUTH=api` forces it) | `auto \| gpt-5-codex \| gpt-5` |

`auto` picks a model per task. The web model picker reads the active provider's list
from `GET /api/config`. Both CLIs ship in the worker image — rebuild it after
upgrading them. (Metered Anthropic API-key billing is intentionally not supported;
Claude runs on the subscription only.)

## Recipes (how a repo's stack comes up)

The tool resolves a **recipe** — how to stand the app up — by inspecting the cloned
repo. Fast deterministic markers first, and when none match, an **AI probe reads the
repo like a developer would** — README, manifests of any ecosystem, the code itself —
and synthesizes a recipe from what it learns. First match wins:

1. a committed `.forge/env.yml` that declares the app → **synthesized** from it
2. **CHAP** markers → `dhis2-chap` (a multi-repo DHIS2 + chap-core stack)
3. `supabase/config.toml` + a Next.js `package.json` → `next-supabase`
4. any `package.json` → `node-web`
5. the repo's own `docker-compose.yml` → wrap it
6. no marker at all → the AI probe inspects the repo and emits an overlay
   (base image, apt packages, setup commands, dev command, port, extra service
   containers like postgres/redis) → **synthesized**
7. a probe that finds nothing servable → `none` (worker only)

That synthesis step is what lets forge spin up **just about anything** — Python, Go,
Rust, Ruby, JVM, PHP, static sites — not only stacks it has templates for. The probe
persists what it learns to the repo's per-repo knowledge overlay, so the next run
starts instantly; the same overlay-delta loop repairs the environment when a start
fails (wrong port, missing system lib, missing db container).

| Recipe | What it stands up | Status |
|---|---|---|
| `node-web` | the repo's dev server + a worker | ✅ validated end-to-end on real Docker |
| `synthesized` | any stack, from the AI probe's (or a committed `.forge/env.yml`) description: app container + optional db/cache containers | ⚙️ generated + unit-tested; quality tracks the probe |
| `none` | worker only — edit + verify + PR, no live URL | ✅ validated |
| `next-supabase` | Next dev server + worker; Supabase via the local CLI, with a per-session `project_id` + port block | ⚙️ generated + unit-tested; a live run needs your Supabase CLI + app |
| `dhis2-chap` | DHIS2 + chap-core + the modeling app on one network, seeded with demo data | ⚙️ generated from verified upstream topology; needs `forge bake` + live validation |

> **Scope, honestly:** synthesized recipes cover the common case — one app process
> plus supporting containers. A repo whose dev environment needs host hardware,
> licensed SDKs, or a cloud account still lands on `none` (the *edit → verify → PR*
> loop works there regardless). Repos with exotic topologies can commit a
> `.forge/env.yml` (same keys as the overlay) or their own compose file and skip
> inference entirely.

**Heavy stacks** need cached seed data so a per-run instance comes up fast:

```bash
forge bake dhis2-chap     # downloads the DHIS2 demo DB into the seed cache
```

## Verify, QA & pull requests

- **Verify** discovers the repo's own checks — `.forge/verify.sh`, a `.forge/repo.yml`
  `command:`, or `package.json` scripts (`test`, `lint`, `build`) — and runs them. It
  is tri-state: pass / fail / *no checks configured* (never a misleading green).
- **Repair loop**: on failure the agent iterates, capped by `FORGE_MAX_REPAIR_ITERS`
  (default 4) and a wall-clock budget. Persistent red either raises a
  **repair-escalation** checkpoint (supervised) or opens a flagged **draft PR**
  (autonomous mode) — it never silently ships broken work.
- **Browser QA** drives the running app against the plan's acceptance criteria with
  Playwright, saving PNG evidence. If it hits a login wall it asks you for credentials
  once (a `needs_input` checkpoint), remembers them per-repo, and keeps them out of
  logs and transcripts.
- **Self-review**: forge reviews its own diff and fixes issues before opening the PR
  (`FORGE_SELF_REVIEW=0` to disable).
- **The PR** carries an agent-written title (issue keys like `ABC-123` carried
  through) and a Summary / Changes / Testing body. Runtime scaffolding and lockfile
  churn are kept out of the diff. Commits are authored as you (with a
  `Co-Authored-By` bot trailer in the default mode); with a GitHub App configured
  the push and the PR itself come from `<app-slug>[bot]`, without one they come
  from your PAT.

## Learning

It keeps a per-repo knowledge overlay under `~/.forge/knowledge/<owner>/<repo>.yml`
(never inside the repo, so it can't leak into a PR). It records env facts learned by
the probe and durable lessons written by the post-PR retrospective; the planner **and**
executor get them on every future run. Teach it directly:

```
remember: always run `bun run gen` after schema changes      (in a session thread)
remember for owner/repo: staging login is on /admin/login    (anywhere)
forget creds                                                  (drop saved QA logins)
```

User-taught lessons are pinned and never evicted. Disable learning with `FORGE_LEARN=0`.

## Reviewing pull requests

It can also review an existing PR and post a summary + inline comments as a neutral
`COMMENT` (it never approves or blocks):

```bash
forge review owner/repo#123
forge review https://github.com/owner/repo/pull/123
```

From Slack, mention a PR (`review owner/repo#123` or paste a URL). To have reviews
post as a bot identity (**`forge[bot]`**) and to credit forge on commits, set up an
optional GitHub App — see [GitHub App](#github-app-recommended).

---

## GitHub App (recommended)

Everything works without one — but a GitHub App buys you three things:

- **Security:** worker pushes/PRs run on a short-lived token scoped to the single
  target repo, minted per run, instead of falling back to your full-scope PAT
  (see [security model](#security-model)).
- **Identity:** reviews post as `<app-slug>[bot]` (instead of your own account with
  a "🔨 Forge Review" header), and branch pushes / PRs come from the bot — which
  also means you can review and approve the PRs yourself.
- **Automation:** the `@<app-slug> review` comment-command trigger.

1. Create a GitHub App; upload an avatar. Permissions: **Pull requests: Read & write**,
   **Contents: Read & write** (write is what lets the worker push on the App token),
   **Metadata: Read**, **Issues: Read** (Write only if you want the
   👀 ack reaction). Subscribe to the **Issue comment** event.
2. Generate a private key (PEM), note the App ID, install the App on your repos.
3. Configure forge and install the JWT-signing extra:

   ```bash
   pip install -e ".[gh-app]"
   export FORGE_GH_APP_ID=123456
   export FORGE_GH_APP_KEY=~/.forge/forge-app.pem
   export FORGE_GH_APP_SLUG=your-app        # → your-app[bot]
   ```

4. Run the webhook listener:

   ```bash
   forge web --slack --github
   ```

   On boot forge opens a cloudflared quick tunnel (or uses `FORGE_PUBLIC_URL`) and
   re-points the App's webhook at itself; the HMAC secret persists in
   `~/.forge/webhook.secret`. **Only the webhook path is exposed publicly** — the rest
   of the API stays local (see [security model](#security-model)). Comments are
   accepted only from users whose `author_association` is OWNER / MEMBER /
   COLLABORATOR; bot and edited comments are ignored and redeliveries are deduped.

---

## Configuration reference

Everything is read from the environment (or `~/.forge/config.env`).

**Credentials & identity**

| Variable | Default | Effect |
|---|---|---|
| `FORGE_PROVIDER` | `claude` | Agent CLI: `claude` or `codex` |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Claude subscription token (`claude setup-token`) |
| `OPENAI_API_KEY` / `FORGE_CODEX_AUTH` | — / `auto` | Codex API-key fallback; `api` forces the key over plan auth |
| `GH_TOKEN` | — | GitHub PAT (`gh auth token`): host-side clones, and the worker push/PR fallback when no GitHub App is configured (never resident in the container) |
| `FORGE_GIT_NAME` / `FORGE_GIT_EMAIL` | from `git`/`gh` | Commit author identity |
| `FORGE_COMMIT_IDENTITY` | `auto` | `auto` (you + `Co-Authored-By: forge[bot]`) / `forge` / `user` |

**Web & sessions**

| Variable | Default | Effect |
|---|---|---|
| `FORGE_WORKSPACE_DIR` | `~/forge-repos` | Folder the repo picker lists / clones into |
| `FORGE_MAX_SESSIONS` | `4` | Max concurrent live sessions |
| `FORGE_WEB_URL` | serving addr | Public base URL used for Slack deep-links |
| `FORGE_ENV_TTL_SECS` | `3600` | Idle env → warm-sleep after this long |
| `FORGE_DORMANT_TTL_SECS` | `259200` | Asleep env → deleted after this long |
| `FORGE_MEM_BUDGET_MB` | `0` (off) | Batch-queue memory budget (else cap = max sessions) |
| `FORGE_WEB_MEM_LIMIT` / `FORGE_WEB_NODE_MAX_OLD_SPACE_MB` | `8g` / `4096` | Contain a leaky dev server |

**Behavior**

| Variable | Default | Effect |
|---|---|---|
| `FORGE_MAX_REPAIR_ITERS` | `4` | Repair-loop iteration cap |
| `FORGE_SELF_REVIEW` | `1` | Self-review-and-fix before forge's own PR |
| `FORGE_QA_GATING` | `1` | Gate the PR on the plan's acceptance criteria |
| `FORGE_LEARN` | `1` | Run a retrospective and record lessons after a PR |
| `FORGE_SELF_HEAL` | `1` | Let the AI probe repair a stuck environment |
| `FORGE_KNOWLEDGE_DIR` | `~/.forge/knowledge` | Per-repo knowledge overlays |

**Slack & GitHub App**

| Variable | Effect |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_ALLOWED_USER` | Bot token / Socket-Mode token / your user id (the only user answered) |
| `FORGE_REPO_ALIASES` / `FORGE_REPO_CACHE_TTL` | Alias file path / repo-list cache TTL |
| `FORGE_GH_APP_ID` / `FORGE_GH_APP_KEY` / `FORGE_GH_APP_SLUG` | App ID / PEM path / slug → `<slug>[bot]` |
| `FORGE_GH_WEBHOOK_SECRET` / `FORGE_PUBLIC_URL` | Webhook HMAC secret / stable public URL (skips the quick tunnel) |

---

## Security model

This is designed for a **single developer running it on their own machine against
repositories they trust.** Understand these properties before exposing it more widely:

- **The agent runs with permission gates disabled inside the worker container**
  (`claude --dangerously-skip-permissions` / `codex --dangerously-bypass-…`). The
  container is the sandbox — it has no `--privileged`, no Docker socket, and published
  ports bind to `127.0.0.1` — but the agent executes whatever the repo and task
  direct. A **prompt-injecting repository** (malicious README / code comments) is a
  real threat: the agent's provider credential (Claude/Codex) is in the container,
  and the agent can be steered into hostile edits. Only point it at repos you trust.
- **The worker container holds no GitHub token.** The container-resident `GH_TOKEN`
  is empty; a token is injected only into forge's own `git push` / `gh pr create`
  executions, one exec at a time. With the [GitHub App](#github-app-recommended)
  configured, that token is minted **per run, scoped to the single target repo, and
  expires within an hour** — a hostile repo that manages to capture it during a push
  (e.g. via a planted git hook or `.git/config` credential-helper trick) can reach
  only itself, briefly. Without the App the fallback is your full-scope `GH_TOKEN`
  PAT — still per-exec rather than resident, but one more reason to set the App up.
- **Host-side git against agent-modified workspaces is hardened.** A workspace's
  `.git/config` could otherwise make git execute arbitrary commands *on the host*
  (via `core.fsmonitor`, `core.hooksPath`, or `credential.helper`); every host-side
  git invocation against a run workspace overrides all three.
- **The web/`/api` surface is unauthenticated** (single-user, local). Keep it bound to
  loopback. In `--github` mode a public tunnel fronts the app, but the gate is an
  allowlist that exposes **only** the signed `POST /api/github/webhook` — any other
  Host, including an unknown or absent one, is refused (fail-closed).
- **Secrets** are passed to compose as `${VAR}` process-env references (never written
  into the repo), and QA credentials are stored `chmod 600` under `~/.forge/knowledge`
  and redacted from logs, transcripts, and Slack. The GitHub webhook uses timing-safe
  HMAC verification with replay dedup.
- **Repo Q&A is containerized too.** The Slack "ask a question about a repo" path
  runs its one-shot agent in a disposable worker container with the clone mounted
  read-only — not on the host.
- **Known rough edge** (documented, not fixed): `~/.codex` is mounted read-write
  into worker containers (including the Q&A one-shot) when using the Codex
  provider (its CLI refreshes the auth file in place).

Found a security issue? It's a personal project — please open an issue.

## Troubleshooting

- **`forge web` exits with an import error** → install the web extra: `pip install -e ".[web]"`.
- **Slack uploads fail with `missing_scope`** → add `files:write` (screenshots) /
  `files:read` (attachments) and reinstall the app.
- **No public Slack URL** → install `cloudflared`; without it forge posts the raw
  `localhost` URL. Dead `trycloudflare.com` links usually mean your resolver is
  NXDOMAIN-ing `*.trycloudflare.com` — test with `dig @1.1.1.1`.
- **Agent behaves oddly after upgrading a CLI** → rebuild the worker image
  (`docker build -t forge-worker worker-image/`); both CLIs are baked in.
- **A dev server eats memory** → capped by `FORGE_WEB_MEM_LIMIT`; lower it for tight hosts.

## Project layout

```
src/forge/
  cli.py                    forge run / status / down / serve / bake / web / review / attach
  session.py                SessionManager: concurrent session lifecycle, turn guard
  compose_orchestrator.py   the one-shot run loop: clone → up → worker → verify → PR
  recipe.py                 repo → recipe (node-web / next-supabase / dhis2-chap /
                            synthesized-from-overlay / none)
  compose*.py               multi-service Compose engine (per-run project)
  envprobe.py, knowledge.py AI environment probe + per-repo learning overlays
  verify.py, qa.py          verify-gate + acceptance/browser QA
  webapp.py                 FastAPI REST + SSE API; serves the web/dist SPA
  slackbot.py, slackroute.* Slack Socket-Mode ingress
  ghapp.py, ghwebhook.py    GitHub App auth + @<slug> review webhook
  store.py                  SQLite: runs, events, env registry, batch queue
web/                        React + Vite SPA (source in src/, built output in dist/)
worker-image/Dockerfile     the worker image (node, git, gh, claude, codex, playwright)
docs/                       user docs + design specs
```

`docs/`: [config file](docs/config-file.md) · [Slack channels](docs/slack-channel-setup.md) ·
[SSE event contract](docs/sse-events.md) · [warm sleep/wake](docs/warm-wake.md) · design
specs in [`docs/specs/`](docs/specs).

## Development

```bash
pip install -e ".[dev,web,slack,gh-app]"
pytest                      # Python suite
cd web && npm install && npm run build && npm test   # web UI (rebuild dist after src edits)
```

## License

MIT — see [LICENSE](LICENSE).
