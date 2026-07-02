# Forge — Design Spec

- **Date:** 2026-06-19
- **Status:** Approved (brainstorm complete, review round 1 incorporated) — ready for implementation planning
- **Author:** dev@example.com (with Claude)
- **Working name:** Forge

> **Forge is not an AI coding agent. Forge is an orchestration system that gives Claude Code
> disposable, reproducible environments and turns completed work into verified pull requests.**

---

## 1. Summary

Forge is a **private, single-user, autonomous coding agent** driven from **Slack**. You send it a
task referencing a GitHub repo; it spins up a clean, disposable, containerized environment, makes the
change, verifies it (tests/lint/build, and — where applicable — runs the live app and screenshots it),
then opens a GitHub Pull Request and posts the link back to the Slack thread.

It is deliberately **not** the heavyweight multi-tenant "Devin platform." It is a tool one person
(you) will actually use on their own, trusted repositories.

**The core insight driving the architecture:** the agent loop, tool-calling, and plan/edit/test/fix
cycle are already solved by **Claude Code / the Claude Agent SDK**. Forge does **not** reimplement
them — **Claude Code is a worker, not the orchestrator.** The differentiated engineering is the
**orchestration shell**: Slack interface, disposable environments, the verification gate, resumable
runs, and PR production. Of those, **reliable disposable environments are the core product**, and
**proving Claude can run on the subscription inside an ephemeral container is the first thing to
de-risk** (see §11, §14, §15).

---

## 2. Locked decisions (brainstorm outcomes)

| Question | Decision | Consequence |
|---|---|---|
| Motivation | A tool I'll actually use | Local, low-ceremony. No multi-tenant, no horizontal scaling, no web UI. |
| Autonomy | Fully autonomous by default | Task in → PR out. No mandatory checkpoints. |
| Interface | **Slack** (Socket Mode) | No public URL/webhook. Thread = run. Progress + screenshots + `ask_user` in-thread. |
| Clarifying questions | Allowed, but **only when strictly stuck** | `ask_user` is a blocking tool the agent uses sparingly. |
| Sandbox | **Docker per run** | Containerized isolation, reproducible env, contained blast radius. Agent runs *inside* the container. |
| Environment model | **Hybrid: templates + repo override** | Platform ships versioned templates; a repo can override via committed `.forge/env.yml`. |
| Agent backend | **Claude Code CLI headless (`claude -p`)** — NOT the Python Agent SDK; **no provider abstraction** in v1 | Phase-0-verified: the SDK rejects subscription/OAuth tokens; the CLI accepts them via `claude setup-token`. Codex/multi-provider is a v2 seam. |
| Billing/usage | **My own Claude subscription, not metered API** | No per-run API invoice. Runs consume the subscription **usage window**. The system does **not** think in dollars internally. |
| Concurrency | **1 active run** (FIFO queue) for v1 | A single run can bring up a heavy stack; Mac is finite. Concurrency is a later config knob. |
| Host process | **One** Python app (Slack bot + orchestrator + MCP bridge) | Split only when it hurts. |
| State store | **SQLite** + on-disk run dirs | No Postgres, no Redis. |
| PR auth | Personal `gh` token | No GitHub App. `gh` already authed (`forge-dev`, scopes incl. `repo`,`workflow`). |
| First proven template | **Supabase** | Validates the env abstraction with a forgiving boot. |
| Next templates | **DHIS2, then CHAP — before polishing** | They carry the biggest real unknown; discover that risk early (§14). |
| Budget caps (defaults) | **30 min / 20 iterations / subscription usage window**, tunable per run | Fail early and report; extend via `continue`. Estimated cost is informational only (§11). |
| Resumability | **First-class primitive** | A run survives rate limits, sleep, Docker/host restart, Slack reconnect, agent crash — without becoming a new run (§6). |

---

## 3. Architecture

```
┌─ YOUR MAC (host) — one Python process ──────────────────────────┐
│                                                                  │
│  Slack Bot (Bolt, Socket Mode)  ◄──── outbound WS to Slack ────► │
│        │  ▲                              (no public URL needed)   │
│        ▼  │                                                       │
│  Orchestrator ── SQLite (runs, events, usage)                    │
│        │  (OWNS: success, PR, budget, verification verdict,      │
│        │         resume)                                          │
│        │           runs/<id>/ (timeline, logs, screenshots,      │
│        │                       diff, report, checkpoint/)         │
│        │                                                          │
│        ├─► Environment Manager ──► docker compose up (per run)    │
│        │     (bake cache: images + seed artifacts)               │
│        │                                                          │
│        └─► spawns Run Container ──────────────┐                   │
│                                               │                   │
│  Host MCP Bridge (HTTP/SSE) ◄─────────────────┤ agent calls:     │
│    post_progress / post_screenshot / ask_user │ post_*, ask_user │
│    (ask_user BLOCKS until Slack thread reply)  │                  │
└───────────────────────────────────────────────┼──────────────────┘
                                                 │ host.docker.internal
┌─ PER-RUN (Docker, per-run network) ─────────────────────────────┐
│  Run Container: gh clone → branch → Claude agent loop (WORKER)   │
│    tools: read/write/bash/run_verify/run_playwright/git/gh      │
│    + MCP client → host bridge (all Slack I/O)                    │
│    auth: Claude subscription creds (NOT metered API key)         │
│    emits: {status: needs_verification | ready_for_finalize | …}  │
│                                                                  │
│  Service stack (compose): supabase / dhis2 / dhis2-chap / none  │
│    fresh logical instance cloned from cached seed                │
└─────────────────────────────────────────────────────────────────┘
```

Four load-bearing moves:

1. **Socket Mode** — the host receives Slack events over an outbound WebSocket. Nothing to expose.
2. **Threads = runs** — each Slack thread is one run's entire lifecycle. Free history mapping.
3. **MCP bridge** — the single clean channel across the container boundary for *all* Slack
   interaction, including the blocking `ask_user`. The agent never holds the Slack token.
4. **Orchestrator owns the verdicts** — the agent (worker) never decides success, PR opening, budget,
   or verification pass. It only reports status; Forge decides (§9).

---

## 4. Components

Each is independently testable with a well-defined interface.

| Component | Responsibility | Depends on |
|---|---|---|
| **Slack Bot** | Parse inbound tasks; render progress/screenshots/questions into the thread; handle commands (`continue`, `cancel`, `status`) | Slack Bolt, Orchestrator |
| **Orchestrator** | Run lifecycle, queue, **all verdicts** (success/PR/budget/verification), resume, state persistence | SQLite, Env Mgr, Docker |
| **Environment Manager** | Resolve template (auto-detect or repo override) → `compose up` → health-gate → expose endpoints → teardown; manage bake cache | Docker, template library |
| **Run Container runtime** | Clone, branch, run the agent **worker**, run verification, commit, push, open PR — under orchestrator direction | Claude backend, `gh`, git |
| **Host MCP Bridge** | Expose `post_progress` / `post_screenshot` / `ask_user`; relay to Slack; block `ask_user` until reply | Slack Bot |
| **Template library** | Named, versioned env definitions (compose fragment + seed recipe + health/provision/teardown hooks) + auto-detectors | — |
| **Checkpointer** | Write/restore resumable run state to `runs/<id>/checkpoint/` (§6) | git, filesystem |
| **Timeline writer** | Append human-readable events to `runs/<id>/timeline.md` (§12) | events stream |
| **`forge bake` CLI** | Build/refresh cached images + seed artifacts out of the run hot path | Docker, template library |

---

## 5. Run lifecycle & state machine

```
Slack msg ("@forge fix the org-unit tree bug in acme/internship-portal")
  → Orchestrator: create Run (thread = run id), enqueue (FIFO)
  → Resolve repo (owner/name or alias) + env template (auto-detect; ask_user if ambiguous)
  → Env Mgr: provision fresh logical instance from cached seed → wait healthy
  → Run Container: gh repo clone → git checkout -b forge/<slug>
  → Agent WORKER loop: understand → plan → implement → emit {status}
  → Orchestrator: on needs_verification → run_verify → decide pass/fail
                  on fail → instruct worker to fix → repeat
                  on ready_for_finalize + gate passed → finalize
  → git commit → git push → gh pr create   (Orchestrator-driven)
  → Post PR link + summary + final screenshots to thread
  → Env Mgr: compose down; archive runs/<id>/; mark Run done
```

### Run states

`queued → provisioning → running → verifying → finalizing → done`

Branch/interrupt states (all **resumable**, same run_id — see §6):
`paused_rate_limit`, `awaiting_user` (blocked on `ask_user`), `interrupted` (crash / restart / sleep).

Terminal: `done`, `failed`, `stopped_budget`, `stopped_no_progress`, `cancelled`.

### Stop conditions

| Condition | Trigger | Outcome |
|---|---|---|
| **Success** | Orchestrator confirms verify gate passed | Open PR, post link |
| **Budget exhausted** | **wall-clock** or **iterations** cap hit | Stop; open **draft** PR if usable diff + post report; offer `continue` |
| **Usage window** | Subscription usage window exhausted | `paused_rate_limit`; checkpoint; post to Slack; **auto-resumable** after window resets, or via `continue` |
| **No progress** | Same failing check-set **and** unchanged diff hash for N iterations | Stop; `ask_user` or abort with report |
| **Awaiting user** | Agent calls `ask_user` | `awaiting_user`; checkpoint; resume on thread reply |
| **Interrupted** | Crash / Docker restart / host sleep / Slack reconnect | `interrupted`; auto-resume from last checkpoint |
| **No verification possible** | No real checks detected/declared (§8, §9) | Refuse non-draft PR; open **draft** + warn |

Note: caps are **wall-clock, iterations, and the usage window** — never dollars (§11).

---

## 6. Resume & durability (first-class)

A run is a **durable, resumable entity**, not a fire-and-forget process. It must survive rate limits,
Mac sleep, Docker/host restart, Slack reconnect, and agent crash **without becoming a new run** (same
`run_id`, same Slack thread).

### The sharp edge

The workspace lives **inside an ephemeral container**. So resumable state must be persisted **outside**
the container, on the host. "Tar the container" is not enough.

### Checkpoint contents (`runs/<id>/checkpoint/`)

```
checkpoint/
  run_state.json       # phase, iteration, last status, failing-check-set, diff hash, budget consumed
  workspace.patch      # uncommitted diff vs the run's WIP branch
  workspace.tar.zst    # (fuller form) untracked/non-git files; excludes node_modules (reinstalled)
```

- **v1 lightweight checkpoint** = committed WIP on the `forge/<slug>` branch **+** `workspace.patch`
  **+** `run_state.json`. Cheap and sufficient for most resumes.
- **Fuller checkpoint** = add `workspace.tar.zst` for runs with meaningful untracked state.
- **Cadence:** after each iteration, on every phase transition, and immediately before any pause
  (`paused_rate_limit` / `awaiting_user`).

### Resume semantics

```
resume(run_id):
  re-provision env (fresh logical instance — service state is NOT checkpointed in v1)
  recreate run container
  git checkout forge/<slug>; apply workspace.patch (+ untar if present); reinstall deps
  re-enter agent worker loop at run_state.json.phase/iteration
```

Service/database state inside the environment is **not** checkpointed in v1 — resume gets a *fresh*
logical instance (consistent with "disposable environments"). The agent re-establishes any needed
in-app state as part of its loop. Checkpointing live DB state is a possible post-MVP enhancement.

**Triggers:** automatic (rate-limit, crash, restart, sleep, reconnect) and manual (`continue` with
added budget). `continue` is just the user-initiated entry into this same machinery.

---

## 7. Environment layer (the core product)

**Design principle:** templates are **explicit, versioned, and aggressively cached**. The hot path of
a run must produce a **fresh logical instance** without rebuilding or re-downloading anything. Separate
the *expensive cached layer* (images + seed data, baked ahead of time) from the *cheap per-run layer*
(a fresh logical instance cloned from cache in seconds).

### Template anatomy

A template is a versioned bundle: `name@version` (the version pins images **and** seed together).

```
template: dhis2@2.43-seed3
  base_images:   [dhis2/core:2.43, postgis:14]          # pulled once, cached by Docker
  seed_artifact: dhis2-2.43-seed3.sql.gz                 # baked once, content-addressed, cached on disk
  provision:     create fresh logical instance from cached seed   # FAST, per-run
  health:        GET /api/system/info → 200 within N s
  endpoints:     { dhis2: http://dhis2:8080 }            # injected into run container env
  teardown:      drop logical instance / compose down
```

Templates live in `forge/templates/<name>/` (compose fragment + seed recipe + hooks). Auto-detectors
map a repo to a template (`d2.config.js`/`@dhis2/cli-app-scripts` → `dhis2`; `supabase/config.toml`
→ `supabase`; plain `package.json` web app → `node-web`; else `none`).

### Cost layers (the UX win)

| Layer | Cost | When | Mechanism |
|---|---|---|---|
| Base images | minutes, once | `forge bake` (offline) | `docker pull`, cached by Docker |
| Seed artifact | minutes, once per seed version | `forge bake` (offline) | boot service, import metadata/data, dump → versioned file |
| **Fresh logical instance** | **seconds, every run** | hot path | Postgres `CREATE DATABASE x TEMPLATE seed` (clone in seconds) → start service against it |

Per run, Forge does **not** re-pull or re-boot-and-seed. It clones a Postgres template DB (seconds)
and attaches a service. The service's own boot (e.g. DHIS2's JVM start) is the residual cost; a
**warm pool** (pre-booted service, swap its DB) is a post-MVP optimization that drops in behind the
same interface without changing callers.

### Repo override (the escape hatch)

A committed `.forge/env.yml` in the target repo replaces or extends the auto-detected **environment**
(services, images, seed). Auto-detection is the zero-config common case. Non-environment behavior lives
in a separate file — see §8.

### MVP scope for this layer

- Prove the abstraction end-to-end on **exactly one** real stateful stack: **Supabase**
  (clean reset / template-clone story, forgiving boot).
- `none` / `node-web` come essentially free and give immediate agent-loop utility.
- **DHIS2** then **CHAP** follow immediately (§14) — they carry the real unknown.

---

## 8. Repository configuration (`.forge/`)

Two distinct files, deliberately separated so neither becomes a dumping ground:

- **`.forge/env.yml`** — *how to stand up the environment* (services, images, seed, health). Overrides
  the auto-detected template (§7).
- **`.forge/repo.yml`** — *how to work in this repo* (non-environment behavior):

```yaml
# .forge/repo.yml
verification:
  command: yarn verify        # the real gate; Forge falls back to auto-detected scripts if absent
playwright:
  start: yarn dev             # how to bring the app up for screenshots
  screens: ["/", "/login"]    # key routes to capture
aliases:
  frontend: apps/web          # path aliases the agent/user can reference
review:
  require_screenshots: true   # finalize policy
```

Both are optional. Absent `repo.yml`, Forge auto-detects (`package.json` scripts, etc.). Absent
`env.yml`, Forge auto-detects the template. The escape hatches exist for repos that are special.

---

## 9. Agent loop — Claude as a *worker*

The worker is the **`claude` CLI in headless mode** (`claude -p --output-format json`, authed by
`CLAUDE_CODE_OAUTH_TOKEN`), which runs the **inner** tool-calling loop with Claude Code's built-in
tools (Read/Write/Edit/Bash/Grep/Glob) plus any MCP servers Forge configures (incl. the host bridge).
**The Orchestrator owns the outer state machine and every verdict.** This separation is load-bearing
and must not erode.

**The agent never decides:** when a run succeeds · when a PR is opened · when budget is exceeded ·
when verification passes. It only reports status; Forge decides everything else.

```
system prompt: task + repo conventions (.forge/repo.yml) + "you are a worker; Forge runs verification
               and decides done" + "use ask_user ONLY when strictly stuck"
tools: file read/write/edit + bash
       + git + gh
       + MCP(host): post_progress, post_screenshot, ask_user
worker emits structured status, e.g.:
   { "status": "needs_verification" }       # I think I'm done; please run the gate
   { "status": "ready_for_finalize", "summary": ..., "files": [...], "screenshots": [...] }
   { "status": "blocked", "question": ... }  # → routed through ask_user
   { "status": "no_progress" }               # → orchestrator decides

orchestrator loop:
  drive worker → on needs_verification: run_verify (orchestrator-side) → decide pass/fail
               → fail: hand failures back to worker to fix (counts an iteration)
               → pass + ready_for_finalize: finalize (commit/push/PR — orchestrator-side)
  enforce: wall-clock / iterations / usage-window caps; no-progress detector; checkpoint each iter
```

### Verification gate (the moat — owned by Forge)

- The gate = the repo's **real** checks: `.forge/repo.yml: verification.command`, else auto-detected
  `package.json` scripts (`test`, `lint`, `build`, `typecheck`), else `.forge/verify.sh`.
- **Forge — not the agent — runs the gate and decides pass/fail.**
- **Forge refuses to open a non-draft PR if it could not run any real verification.** It opens a
  **draft** PR and warns instead. No silent false victories.
- Where a template + `repo.yml` provide a live app, Playwright screenshots of `playwright.screens` are
  evidence posted to Slack and embedded in the PR body.

### Finalize (Forge-driven)

On `ready_for_finalize` + gate pass, Forge commits, pushes, and opens the PR. The agent's structured
output (summary, files, results, screenshot refs) becomes the PR body.

---

## 10. Slack UX

- **Trigger:** `@forge <task>` (channel mention) or DM. The message names the repo as `owner/name`
  or a configured alias; if ambiguous, `ask_user`.
- **Thread = run.** All progress, screenshots, questions, and the final PR link land in the thread.
- **Progress:** periodic `post_progress` (phase, current action, iteration N/cap, time used,
  est. usage — informational).
- **Screenshots:** `post_screenshot` uploads Playwright images to the thread.
- **Questions:** `ask_user` posts in-thread and **blocks** the run (checkpointed) until you reply.
- **Commands** (in-thread or DM):
  - `continue <run_id> [+20min] [+10 iters]` — manual resume with explicit added budget (§6).
  - `cancel <run_id>` — abort and tear down.
  - `status [run_id]` — current state, time/iterations used, est. usage, last action.

---

## 11. Cost, budget & usage

Forge drives Claude via **your Claude subscription** — the same auth Claude Code uses, **not** a
metered API key. **The system does not think in dollars internally.**

- **Stop/budget conditions are exactly three:** **wall-clock**, **iterations**, and **subscription
  usage-window exhaustion**. Nothing else controls execution.
- An **estimated API-equivalent cost** ("≈ $X at list price") may be shown in `status`/progress as
  **purely informational** context. It is never a stop condition and the orchestrator never branches
  on it.
- **Defaults:** 30 min / 20 iterations / usage window. Tunable per run; extendable via `continue`.
- **Usage-window exhaustion** is a first-class, **resumable** stop (§5, §6): pause, checkpoint, post to
  Slack, auto-resume when the window resets.

> ### ✅ PHASE 0 BLOCKER — RESOLVED (spike, 2026-06-19; see `spike/FINDINGS.md`)
> Confirmed end-to-end on this Mac: the **`claude` CLI headless (`claude -p`)** runs in an ephemeral
> container authed only by `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`; ~1-yr, no
> auto-refresh) — `ANTHROPIC_API_KEY` unset — fixes a failing test, and reports `num_turns` /
> `duration_ms` / `total_cost_usd` (informational) / `usage` tokens for metering. Backend decision
> locked to the CLI (the Python Agent SDK rejects subscription tokens). The original questions, now
> answered:
>
> ### ⛔ Original blocker (history)
> Before building anything else, prove (a single evening's spike) that Claude Code can run cleanly in
> an ephemeral container on the subscription. Specifically answer:
> 1. **How does authentication work** inside a container (credential mount vs. token vs. headless
>    `claude -p`)?
> 2. **Can multiple containers** use it (now serialized at concurrency 1, but does the model forbid it)?
> 3. **Does usage reporting exist** in that mode (for the iterations/usage accounting we display)?
> 4. **Does headless mode behave predictably** (deterministic non-interactive runs, exit codes,
>    structured output)?
>
> If Claude cannot run cleanly in an ephemeral container under the subscription model, **half the
> architecture changes** (the Run Container runtime and the usage meter). Resolve this first.

---

## 12. Data & state

### SQLite (sketch)

```
runs(id, slack_thread_ts, slack_channel, repo, task, branch, state,
     template, created_at, updated_at, pr_url, pr_is_draft,
     wall_secs, iterations, usage_note, est_cost_usd_informational,
     budget_secs, budget_iters, resume_count)

events(id, run_id, ts, type, payload_json)   -- progress/tool/screenshot/question/answer/verdict log
artifacts(id, run_id, kind, path, created_at) -- screenshots, diffs, logs, report, checkpoint
```

### On-disk run dir

```
runs/<run_id>/
  meta.json
  timeline.md          # human-readable, auto-generated — the FIRST thing you read on failure
  agent.log            # full agent transcript / tool calls
  diff.patch           # final diff
  report.md            # structured finalize output (→ PR body)
  screenshots/*.png
  env.log              # environment manager logs (provision/health/teardown)
  checkpoint/          # resumable state (§6): run_state.json, workspace.patch, [workspace.tar.zst]
```

`timeline.md` is generated automatically from the events stream:

```
20:14  Run created — acme/internship-portal · template node-web
20:15  Env healthy · repo cloned · branch forge/fix-orgunit-tree
20:17  Verify: tests FAILED (2)
20:19  Worker edited apps/web/OrgUnitTree.tsx
20:21  Verify: PASSED
20:22  PR opened → https://github.com/acme/internship-portal/pull/42
```

---

## 13. Secrets & trust boundary

- **Run container gets:** `GH_TOKEN` (push + PR), Claude **subscription credentials** (agent backend).
- **Run container does NOT get:** the Slack token (all Slack I/O via the host MCP bridge).
- **Environment stacks** get their own scoped credentials, isolated on a per-run Docker network.
- **Honest caveat:** the agent executes arbitrary repo code *with the gh token present*, so a malicious
  dependency could exfiltrate it. This is **acceptable under the "own, trusted repos" trust model**
  chosen for v1 — but stated, not hidden. If untrusted repos ever enter scope, revisit (network egress
  control, token-less clone via short-lived deploy keys, stronger sandbox than Docker).

---

## 14. MVP phasing

Honest estimate: **~2–3 weeks**, not a clean 2 — Docker + Playwright + a real stateful template + a
resume primitive is genuinely more than two weeks of polished work.

| Phase | Deliverable | Env |
|---|---|---|
| **0 — De-risk + skeleton** | **Resolve the §11 PHASE 0 BLOCKER first.** Then: one Python app, Slack Socket Mode round-trip, SQLite run model, run dir + `timeline.md`. | — |
| **1 — Agent spine (worker)** | Run container, `gh clone` + branch, Claude **worker** loop emitting status, **orchestrator-owned** verify gate, commit/push/`gh pr create`, PR link to Slack. **Agent is useful here.** | `none` / `node-web` |
| **2 — Slack richness + resume** | Progress streaming, Playwright screenshots, `ask_user` blocking via MCP bridge, `continue`/`cancel`/`status`, budget enforcement (time/iters/usage), **resume/checkpoint primitive (§6)**. | `node-web` |
| **3 — Env abstraction proven E2E** | Template system + `forge bake` + fresh-logical-instance + health gate, proven on one real stateful stack. | **Supabase** |
| **3a — DHIS2 template** | Stand up + verify a real DHIS2 app. **The biggest unknown — do it before polishing.** | **DHIS2** |
| **3b — CHAP template** | DHIS2 + CHAP services. | **CHAP** |
| **Post-MVP** | Warm pool; concurrency > 1; live-DB checkpointing; provider abstraction; polish. | — |

Rationale for 3a/3b before polish: the project's biggest real risk is *"can Forge reliably stand up
and verify DHIS2/CHAP apps?"* — not *"can Forge run another Node project?"* Surface that risk early.

---

## 15. Risks (ranked)

1. **Claude subscription auth inside containers** (§11 PHASE 0 BLOCKER). If Claude can't run cleanly in
   an ephemeral container on the subscription, half the architecture changes. *Mitigation:* prove it in
   one evening before building on it.
2. **Reliable disposable environments** (the core product). *Mitigation:* versioned cached seeds,
   template-DB clone for fresh logical instances, `bake` offline, health gates with timeouts, warm pool
   later.
3. **DHIS2 boot/seed time & reliability.** *Mitigation:* cache image + seed; logical-instance clone;
   tackle in Phase 3a to surface early; warm pool post-MVP.
4. **Container↔host MCP bridge + `ask_user` blocking** (pause/resume, timeouts, reconnect). *Mitigation:*
   checkpoint on block; idempotent resume (§6).
5. **False victory** (agent "done" on broken code). *Mitigation:* hard, **orchestrator-owned** verify
   gate; refuse non-draft PR without real checks.
6. **Loop / time runaway.** *Mitigation:* hard caps (30 min / 20 iters / usage window), no-progress
   detector.

---

## 16. Open questions / must-verify

- ~~**Subscription auth in container** (§11) — Phase 0 blocker~~ — **RESOLVED** 2026-06-19 (spike PASS; backend = `claude -p` CLI; token via `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`). Residual: ~1-yr token, no auto-refresh → Forge detects auth failure and re-prompts.
- **DHIS2 seed strategy** (Phase 3a) — pre-baked SQL dump vs. prebuilt image-with-data; which gives the
  fastest reliable fresh logical instance.
- **Playwright for DHIS2 apps** (Phase 3a) — dev-server proxy from inside the container to the in-stack
  DHIS2; auth/session bootstrapping for screenshots.
- **Warm pool** (post-MVP) — DHIS2 caches metadata in memory; a DB swap under a running instance needs a
  restart or cache clear. Quantify before committing.
- **Live-DB checkpointing** (post-MVP) — whether resume ever needs to restore in-environment DB state
  rather than a fresh logical instance.

---

## 17. Out of scope (v1)

Multi-tenant; horizontal scaling; web UI; Postgres/Redis; GitHub App; provider abstraction (Codex/GPT);
concurrency > 1; untrusted-repo sandboxing; warm pool; live-DB checkpointing; dollar-based budgeting.
