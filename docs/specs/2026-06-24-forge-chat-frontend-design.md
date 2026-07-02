# Forge Chat Frontend — Design

**Date:** 2026-06-24
**Status:** Approved (brainstorming) → planning
**Supersedes/extends:** `docs/specs/2026-06-23-forge-env-layer-clickable-url-design.md`

## Summary

A local, single-user **web chat workspace** on top of Forge. Each chat **session** owns one
warm Docker Compose environment and one live inspection URL. You pick a repo (from a local
workspace folder or by typing `owner/repo`), describe a change, and watch Claude work **live**
(streamed narration) inside the per-session container. You iterate by sending more prompts in
the same session — the agent resumes context, edits the same working tree, and the app
hot-reloads at the same URL. A **PR is opened only on demand**, when you click a button. Multiple
sessions run concurrently, each with its own environment and URL.

This replaces the need to drive `forge run` by hand for the interactive
"make-a-change → inspect → refine" loop, while leaving the existing CLI untouched.

### Goals

- A chatbot-style UI: type a task, watch the agent work in real time, inspect the result.
- One session = one isolated environment = one stable inspection URL.
- Multi-turn iteration within a session (resume the same agent + env; cumulative working-tree edits).
- On-demand PR creation (no half-baked PRs pushed to GitHub).
- Concurrent sessions, each independent, with a resource cap.
- Favor **minimal, simple, well-crafted** code changes from the agent.
- Maximally reuse the existing, tested Forge building blocks.

### Non-goals (v1, YAGNI)

- Auth / multi-user / remote or hosted deployment.
- Per-turn commits; auto-fix-on-failure by default.
- Screenshot capture; code-search across repos (the agent searches inside the container already).
- Concurrency beyond the configurable soft cap.

## Architecture

### One new entry point: `forge web`

`forge web` starts a **FastAPI** server bound to `127.0.0.1` that:

1. Serves the **React SPA** (built by Vite to static files, served as static assets).
2. Serves the **JSON + SSE API** under `/api`.
3. Absorbs the responsibilities of `forge serve` — runs the **Caddy proxy** and the
   **idle-TTL reaper** in-process (background task), so a single command runs everything.

The existing CLI verbs (`run`, `status`, `down`, `bake`, `serve`) keep working unchanged. `forge
web` is an interactive sibling, not a replacement.

### The orchestrator is decomposed, not rewritten

A new `SessionManager` exposes the run pipeline as discrete, individually-callable steps. Each
step reuses the *existing, tested* building blocks (`hostops.LocalHost.clone`, `recipe.resolve`,
`composeenv.ComposeEnv`, `commands.*`, `verify.parse_verify`, the finalize helpers,
`health.health_poll_argv`, `envreg.web_url`). Today's `ComposeOrchestrator.run()` stays for the
CLI path; `SessionManager` is the interactive sibling calling the same lower-level pieces.

```
SessionManager
  start(repo, source)    clone → recipe → compose up → health-gate    → env live, URL registered (no agent, no PR)
  turn(session, prompt)  worker --resume (stream-json) → verify        → narration streamed, diff + verdict (repeatable)
  open_pr(session)       commit (as you) → push → gh pr create         → PR url (on demand, button)
  end(session)           docker compose -p forge-<id> down -v          → reaped
```

### Key semantic decisions

- **`session_id == run_id`.** A session reuses **one `runs` row + one `envs` row** for its whole
  life. Turns resume the same Claude session (worker `--resume <claude_session_id>`), so context
  carries across follow-ups. The env stays warm; the dev server hot-reloads, so the URL is stable
  across turns.
- **Git is orchestrator-owned; turns do not commit.** Each turn edits only the working tree. The
  **diff viewer = `git diff <base-branch>`** in the workspace — always the cumulative change.
  Commit → push → PR happen only in `open_pr()`. The worker prompt instructs the agent to edit
  only (never commit/push/PR) and to favor **minimal, simple, well-crafted** changes.
- **Verify reports; the user decides.** A turn runs the verify gate once and reports pass/fail; it
  does **not** auto-loop fixes (the user iterates by sending another prompt). Auto-fix remains
  available behind a config toggle, **default off** in chat mode.

### Concurrency & resource model (change from today's concurrency=1)

- Sessions are **independent**: starting a session does **not** reap the others. The interactive
  path bypasses `lifecycle.reap_superseded_projects` (that logic remains for the CLI `run` path).
- A configurable soft cap **`max_live_sessions` (default 4)** guards the machine. Hitting it
  blocks a new session with a message naming which idle session to reap first.
- Heavy recipes (DHIS2/CHAP, multi-GB images) show a resource warning at session start.
- The in-process idle-TTL reaper auto-cleans sessions idle beyond `env_ttl_secs` (reuses
  `lifecycle.reap_idle`).
- **Restart-safe:** sessions + envs persist in SQLite. On `forge web` startup, reconcile the Store
  against `docker compose ps` — reconnect to still-running envs, mark dead ones `failed`.

## Data model

Minimal additions to the existing SQLite `Store`. Sessions reuse `runs` + `envs`.

- **`runs`** gains three columns:
  - `claude_session_id TEXT` — for worker `--resume`.
  - `repo_source TEXT` — `local:/abs/path` or `github:owner/repo`.
  - `title TEXT` — editable session name; defaults to the first prompt (truncated).
  - Existing columns (`state`, `pr_url`, `created_at`, `updated_at`) cover the rest.
- **`envs`** is unchanged — already holds `web_url`, `web_service`, `web_port`, `state`,
  `last_seen_at`.
- **New `messages` table** — the chat transcript:
  ```sql
  CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,              -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    meta TEXT                        -- JSON: cost, diff_files, verify summary, etc.
  );
  ```
- **`events`** keeps its current job: the phase timeline (clone → up → verify → live).
- **Streaming deltas are ephemeral** — they flow over SSE for the live feel; only the agent's
  *final* summary is persisted to `messages`. A browser refresh replays a clean transcript from
  the Store (`messages` + `events`) without having needed to catch every token.

Schema migration: additive `ALTER TABLE` guarded by a "column exists" check (SQLite has no
`ADD COLUMN IF NOT EXISTS`); `messages` via `CREATE TABLE IF NOT EXISTS`. Existing rows/tests
unaffected.

## API surface

All endpoints under `/api`, server bound to `127.0.0.1` only.

| Method + path | Purpose |
|---|---|
| `GET /api/repos?q=` | List/search the local workspace folder of repos |
| `POST /api/sessions` `{repo, source}` | Start a session (provision); returns `session_id` |
| `GET /api/sessions` | Sessions sidebar data (repo, state, url, last_active) |
| `GET /api/sessions/{id}` | Detail: state, url, pr_url, full message history |
| `POST /api/sessions/{id}/messages` `{prompt}` | Kick off a turn |
| `GET /api/sessions/{id}/stream` (SSE) | Live event stream (phases, narration, verify, url, done) |
| `GET /api/sessions/{id}/diff` | Working-tree diff vs base branch (diff viewer) |
| `GET /api/sessions/{id}/verify` | Latest verify result (verify panel) |
| `POST /api/sessions/{id}/pr` | Open/update the PR (button) |
| `POST /api/sessions/{id}/stop` | Cancel an in-flight turn (kills the worker exec) |
| `DELETE /api/sessions/{id}` | End + reap the env |

### SSE event types

A single SSE stream per session carries both **provisioning** (during `start`) and **turn**
events, so the sidebar and chat update from one connection:

```
event: phase     data: {"name":"agent","label":"Agent working"}
event: narration data: {"text":"Reading src/datepicker.tsx…"}      # streamed live, not persisted
event: verify    data: {"ok":false,"failed":["test"],"output":"…"}
event: url       data: {"web_url":"http://run-ab12.forge.localhost:8088"}
event: done      data: {"message":"…final summary…","diff_files":3} # message persisted
event: error     data: {"kind":"auth|clone|health|…","detail":"…"}
```

On disconnect the client reconnects and re-fetches `GET /api/sessions/{id}` for the authoritative
transcript; persisted `events` + `messages` mean nothing is lost.

## Streaming mechanics

The worker invocation changes from "run and capture full stdout" to
`claude -p --output-format stream-json --verbose`, read **line-by-line as produced**. This needs
one new method on the env abstraction:

- **`ComposeEnv.exec_stream(argv, service)`** — yields stdout lines as they are produced (today's
  `exec` buffers and returns on completion). Implemented via `docker compose exec` with a streamed
  pipe. The existing `exec` stays for non-streaming steps (setup, verify, git).

`SessionManager.turn()` consumes that iterator, parses each stream-json event, persists the final
result to `messages`, and re-emits typed SSE events (above). The `stop` endpoint terminates the
streaming exec process.

## Frontend (React + Vite → static, served by FastAPI)

Three-pane workspace:

```
┌────────────┬─────────────────────────────┬──────────────────────────┐
│ SESSIONS   │  CHAT                        │  INSPECTOR (tabs)        │
│            │                              │  [Preview][Diff][Verify] │
│ + New      │  ▸ you: "fix the date picker"│                          │
│ ───────    │  ▸ claude: reading files…    │  Preview: <iframe src=   │
│ ● datepick │    (streaming narration)     │   run-ab12.forge.local>  │
│   live ↗   │  ▸ ✅ verified · 3 files      │   ↳ open in new tab      │
│ ○ chap-fe  │                              │                          │
│   working  │  ┌─────────────────────────┐ │  Diff: file tree +       │
│ ○ analytics│  │ type a follow-up…       │ │   line diff vs base      │
│   idle     │  └─────────────────────────┘ │                          │
│            │  [Open PR] [Stop] [End]      │  Verify: ✅/❌ per cmd +  │
│            │                              │   expandable output      │
└────────────┴─────────────────────────────┴──────────────────────────┘
```

- **Sessions sidebar** — "+ New" opens the repo picker (searchable local-folder list + a
  "paste `owner/repo`" field). Each session shows a state badge
  (`provisioning / working / live / idle / failed`) and a click-to-open URL. Clicking a session
  switches the center + right panes; other sessions keep running in the background.
- **Chat** — message bubbles with live-streamed narration; input box; action buttons
  (Open PR, Stop, End).
- **Inspector tabs** —
  - **Preview**: iframe of the live URL, with an "open in new tab" fallback when the app sends
    `X-Frame-Options`/CSP that blocks framing (common for DHIS2 — the link always works).
  - **Diff**: file tree + per-file line diff vs base.
  - **Verify**: per-command pass/fail with expandable output.

Repo picker source: a configurable local **workspace folder** (default e.g. `~/forge-repos/`)
that the UI lists/searches, plus a free-text `owner/repo` field for GitHub clone-on-demand.

## Error handling

Every failure surfaces as a `system` chat message **and** a state badge — never a silent hang:

- clone / recipe / `compose up` / health-gate failure → session `failed`, message with captured
  stderr, a **Retry** action.
- agent auth/usage error (subscription) → explicit "Claude auth/usage problem" message (mirrors
  the worker's existing `auth_error` path).
- `max_live_sessions` reached → new-session blocked with a message naming which idle session to
  reap first.
- SSE drop → client auto-reconnects + re-fetches transcript (authoritative state from the Store).
- `forge web` restart → reconcile Store vs `docker compose ps`: reconnect live envs, mark dead
  ones `failed`.
- iframe blocked → detected client-side, falls back to the plain link.

## Security

- Server binds `127.0.0.1` only.
- `CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN` stay server-side; **never** sent to the browser.
- The SPA only ever sees URLs, diffs, and messages.

## Testing

Mirrors the existing style: injected `host` + fake `env_factory`; real-Docker smokes gated on
image presence.

- **Unit:** `SessionManager.start/turn/open_pr/end` with fakes; `messages` table CRUD; stream-json
  → SSE event parsing; API routes via FastAPI `TestClient` with a mocked SessionManager.
- **Integration:** full session lifecycle (start → 2 turns → `open_pr` → end) against fakes —
  assert state transitions, cumulative diff, and that PR creation fires **only** on `open_pr()`.
- **Smoke (real Docker, gated):** start a `node-web` session, send a turn making a visible change,
  assert the live URL serves the changed content, assert `git diff` is non-empty, and that PR
  creation only fires on `open_pr()`.

## Component / file plan (new code lives beside existing modules)

```
src/forge/
  session.py        SessionManager: start/turn/open_pr/end (reuses host, recipe, ComposeEnv, verify, commands)
  webapp.py         FastAPI app: routes, SSE, static-file serving, startup reconcile + reaper task
  composeenv.py     + exec_stream(argv, service) streaming variant   (edit)
  store.py          + runs columns, messages table + CRUD             (edit)
  cli.py            + `forge web` subcommand                          (edit)
web/                React + Vite SPA
  src/…             Sidebar, Chat, Inspector (Preview/Diff/Verify), SSE client, API client
  dist/             built static output served by FastAPI
tests/
  test_session.py           SessionManager lifecycle (fakes)
  test_webapp.py            API routes (TestClient, mocked manager)
  test_store_messages.py    messages CRUD + runs migration
  test_session_smoke.py     real-Docker node-web session lifecycle (gated)
```

## Open questions / future

- Stable per-session proxy URLs already exist via Caddy (`run-<id>.forge.localhost`); the iframe
  uses them when `forge web`'s proxy is up, else the raw `localhost:<port>`.
- CHAP dual-URL behavior (chap-frontend → :3000 app; chap-core → :8080 DHIS2) is handled by the
  recipe; the UI just displays whatever URL the recipe registers.
- Future: cancel/rewind a turn to a prior working-tree state; multi-repo workspace search;
  warm pool to cut DHIS2 cold-start.
