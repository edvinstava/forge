# Forge — Environment Layer & Clickable App URL — Design Spec

- **Date:** 2026-06-23
- **Status:** Draft (brainstorm complete) — pending user review, then implementation planning
- **Author:** dev@example.com (with Claude)
- **Builds on:** `docs/specs/2026-06-19-forge-design.md` (the original Forge design). This spec realizes
  that design's §7 *Environment layer* — called out there as **"the core product"** — and extends it
  with an explicit goal the original left implicit: **a stable, clickable URL serving the running app
  with the fix applied.**

> **This spec turns Forge from a "verification-gated code editor" (today's Phase-1 spine) into a system
> that stands up a repo's whole app stack per run, hands you a clickable URL with the fix running, keeps
> it warm to inspect, and reuses each repo's own tooling so it generalizes across stacks.**

---

## 1. Summary

Today (Phase 1) Forge clones a repo into **one** container, runs `claude -p` to make a change, runs the
repo's test/lint/build gate, and opens a PR. It never starts the application and publishes no ports — so
there is no URL to click (see the gap assessment that motivated this spec).

This spec adds the **Environment Layer**: per run, Forge brings up the repo's **full app stack** as an
isolated Docker Compose **project**, health-gates it, **seeds it and logs in**, and exposes the web
service through an always-on reverse proxy at a **stable, readable URL** (`run-<id>.forge.localhost`).
The Claude **worker** runs as a service *inside that same project network*, so it can reproduce the
reported bug against the live app, screenshot it, fix it (dev server hot-reloads off a shared volume),
and the orchestrator verifies and opens the PR. The stack **stays warm** after the PR opens so the URL
is live for inspection; an idle reaper tears it down later.

It is built **repo-first** — it reuses a repo's own `docker-compose.yml` / dev command where present, and
falls back to versioned Forge **templates** only for stacks that need assembling. It is proven first on
**Next.js + Supabase**, then on **DHIS2 + CHAP**.

---

## 2. Locked decisions (this round)

| Question | Decision | Consequence |
|---|---|---|
| Runtime / isolation model | **Compose-per-run on the host Docker engine + reverse proxy** | Each run is an isolated Compose *project* (`forge-<id>`, own network + volumes). Real multi-service support; reuses existing compose; stable URLs. Not a VM — isolation is project-scoped (consistent with the "own, trusted repos" trust model). |
| Stack recipe source | **Repo-first → template fallback → `.forge/env.yml` override** | Generalizes with least per-repo work; templates exist only for stacks that need assembly. |
| Readiness bar at the URL | **Pre-seeded + auto-logged-in where feasible** | Forge prefers the repo's own seed path; templates supply a baked seed artifact for heavy stacks; a known dev login is pre-established. Best for reproducing bugs on click. |
| Environment lifecycle | **Keep warm; reap on idle TTL (2h) / `forge down` / next run** | The URL is live after the PR opens. Heavy stacks don't run forever. Concurrency 1 means the next run reclaims resources. |
| Proving sequence | **Next+Supabase first, then DHIS2+CHAP** | Validate the engine on a forgiving, fully-scriptable stack; then take the proven engine to the heavy unknown. |
| Interface (now) | **CLI-first + auto-open browser**; Slack deferred | `forged` daemon is built Slack-ready; the Slack bot drops in later without rework. |
| Auth / billing | **Unchanged: subscription-only, no metered API anywhere** | Worker stays `claude -p` on `CLAUDE_CODE_OAUTH_TOKEN`. The env layer is pure Docker orchestration; it never touches Claude auth. |
| Verdict ownership | **Unchanged: orchestrator owns success/PR/budget/verify** | The agent remains a worker; the live app only gives it more to *report* (repro, screenshots). |
| Concurrency | **Unchanged: 1 active run** | A DHIS2+CHAP stack is heavy; one live env at a time. |

---

## 3. Architecture

Two things that are ephemeral today must become **persistent on the host**: a small daemon, and a proxy.

```
┌─ YOUR MAC (host) ─────────────────────────────────────────────────────────┐
│                                                                            │
│  forged  (NEW — one long-lived host process)                              │
│    • FIFO run queue (concurrency 1)     • env registry (SQLite)           │
│    • EnvManager: compose up/health/seed/teardown                          │
│    • proxy config writer + reaper (idle TTL)                              │
│    • (LATER: the Slack bot lands here — same process, per original §3)    │
│        │                                                                   │
│        ▼  rewrites routes                                                  │
│  Caddy  (NEW — one always-on proxy container)                             │
│    *.forge.localhost  ──►  the run's published web port on 127.0.0.1      │
│        ▲  you click http://run-ab12.forge.localhost                       │
│        │                                                                   │
│  ┌─ PER-RUN Compose project  forge-<id>  (own network + named volumes) ─┐ │
│  │                                                                       │ │
│  │   web / frontend     chap-core (+redis)      dhis2 + db              │ │
│  │   (or: next)         (CHAP only)             (or: supabase stack)    │ │
│  │        ▲ published web port → host (only the web service)            │ │
│  │                                                                       │ │
│  │   worker  (claude -p headless WORKER)                                │ │
│  │     • same network → reaches services by name (http://chap-core:8000)│ │
│  │     • shared /work volume → edits code; dev server hot-reloads       │ │
│  │     • auth: CLAUDE_CODE_OAUTH_TOKEN (subscription, NOT an API key)   │ │
│  │     • GH_TOKEN for push + PR                                          │ │
│  └───────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

Load-bearing moves added by this spec:

1. **The Compose project is the unit of isolation.** `docker compose -p forge-<id>` gives each run its own
   network, volumes, and lifecycle. Teardown is `compose -p forge-<id> down -v`.
2. **The worker is a service in that project**, not a lone container. It shares the project network (so it
   can hit the live app) and a `/work` volume (so its edits hot-reload the dev server).
3. **Exactly one web service is published** to the host per run; everything else is reachable only between
   containers. The proxy maps a stable hostname to that published port.
4. **`forged` outlives any single run.** Warm environments, the reaper, the queue, and (later) Slack all
   need a process that is alive between and across runs. Today's one-shot CLI cannot hold warm state.

---

## 4. Components

New and changed components, each independently testable.

| Component | New/Changed | Responsibility | Depends on |
|---|---|---|---|
| **`forged` daemon** | NEW | Run queue, env registry, lifecycle orchestration, proxy-config writer, reaper. Hosts the CLI's server side; later hosts Slack. | SQLite, EnvManager, Docker |
| **EnvManager** | NEW | Resolve recipe → render compose project → `up` → health-gate → seed → auto-login → register endpoints → teardown. | Recipe resolver, Docker Compose, templates |
| **Recipe resolver** (`recipe.py`) | NEW | Map a cloned repo to a concrete stack definition via the repo-first precedence (§5). | repo files, template library |
| **Template library** (`templates/`) | NEW | Versioned `next-supabase`, `dhis2-chap`, `node-web` bundles: compose fragment + detector + health + seed recipe + web-service/port + auto-login hook. | — |
| **Proxy manager** (`proxy.py`) | NEW | Keep Caddy's dynamic config in sync with live envs; reload on change. | Caddy container |
| **Reaper** (`reaper.py`) | NEW | Tear down envs on idle TTL / explicit `down` / resource pressure. | env registry, EnvManager |
| **Compose runner** (`compose.py`) | CHANGED (was `container.py`) | Pure argv + thin wrapper for `docker compose -p … up/exec/down`, port discovery, project network. | Docker Compose |
| **Orchestrator** | CHANGED | Calls EnvManager for env up/seed/teardown around the existing worker+verify loop; adds repro-first + screenshot steps; keeps all verdicts. | EnvManager, Store |
| **Worker prompts** | CHANGED | Told the live app URL(s) and to reproduce-before-fixing; uses `--resume` for cross-iteration memory. | — |
| **`forge bake` CLI** | NEW | Pre-pull base images + build seed artifacts offline (the cached layer). | Docker, template library |
| **Store** | CHANGED | `+ envs` table; `+ artifacts` for screenshots. | SQLite |
| **CLI** | CHANGED | `forge serve` / `run` / `up` / `down` / `status`; `run` becomes a thin client of `forged`. | `forged` |

---

## 5. Recipe resolution (repo-first)

For each cloned repo, the resolver picks the stack definition **in this order** (first match wins):

```
1. .forge/env.yml committed in the repo      → full override (services, web, health, seed)
2. repo's own docker-compose.yml / compose.yaml
     (or a Makefile/`dev` target/documented command) → WRAP it (merge a Forge override)
3. auto-detected Forge template:
     supabase/config.toml (+ Next markers)    → next-supabase
     CHAP markers (d2.config.js / chap deps)  → dhis2-chap
     plain package.json web app               → node-web
4. else                                       → none  (today's behavior: worker only, no app)
```

**Wrapping (case 2)** = Forge generates a `docker-compose.forge.yml` override that:
- adds the **worker** service (image = the Forge worker image, on the project network, `/work` mounted);
- mounts the cloned repo at `/work` as a shared volume so edits hot-reload;
- **publishes the web service's port** to an allocated host port (the only published port);
- injects wiring env vars the template/recipe declares (e.g. `NEXT_PUBLIC_SUPABASE_URL`, `DHIS2_BASE_URL`);
- adds healthchecks for services that lack them.

The override **never edits the repo's own compose file** — Compose merges overrides at runtime
(`-f docker-compose.yml -f docker-compose.forge.yml`), so the repo is untouched and the change is
auditable.

**Single-repo vs. multi-repo stacks.** Wrapping (case 2) fits **single-repo** stacks where one repo's
compose brings up everything (e.g. Next+Supabase: the repo holds the app *and* `supabase/`). A
**multi-repo** stack — where the running system spans repos that don't individually define the whole
thing (e.g. CHAP: chap-core and chap-frontend are separate repos, and neither alone stands up all three
pieces) — falls through to a **template** (case 3) that assembles the full stack and live-mounts only the
repo under change. The detector recognizes such a repo as a member of a known multi-repo stack.

**`.forge/env.yml` and `.forge/repo.yml`** keep the shapes from the original spec §7/§8. `env.yml`
describes the environment (services/images/web/health/seed); `repo.yml` describes how to work in the repo
(verification command, key screens for screenshots, aliases). Both optional.

---

## 6. Environment lifecycle & states

```
provisioning → seeding → healthy(URL live) → [worker loop] → warm → reaped
                                   │
              health timeout / compose error → failed (logs to runs/<id>/env.log)
```

- **provisioning** — `compose -p forge-<id> up -d`; pull is a cache hit if `bake`d.
- **seeding** — run the seed recipe; clone the baked template DB (`CREATE DATABASE … TEMPLATE seed`) for
  heavy stacks; establish the dev-login session.
- **healthy** — declared health endpoint(s) return ready within the timeout; URL registered with the
  proxy; (CLI may auto-open the browser).
- **warm** — after the PR opens, the stack stays up; URL stays live.
- **reaped** — torn down on **idle TTL (default 2h)**, explicit **`forge down <id>`**, or when the **next
  run** needs the resources (concurrency 1). Reaping = `compose down -v` + drop the proxy route + mark the
  env `reaped` in the registry.

`forge up <id>` re-provisions a reaped env from the same branch + cached images/seed (fast), in case you
want to look again later.

**Resume interplay (from original §6):** resuming a run re-provisions a *fresh logical instance* (service
state is not checkpointed in v1); the env layer is the mechanism that makes that re-provision cheap.

---

## 7. Seed + auto-login (the "ready on click" layer)

General rule: **prefer the repo's declared seed**; templates supply a baked artifact only when the repo
can't seed itself fast enough (heavy stacks).

- **Next+Supabase:** `supabase db reset` applies `supabase/migrations/*` + `supabase/seed.sql`. Forge
  injects `NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321` + the anon key into `.env.local`. The local
  anon/`service_role` keys are **fixed, deterministic JWTs** (signed from the default local JWT secret) —
  identical on every machine — so the template can **hardcode** them instead of parsing `supabase start`
  output. "Logged in" = a seeded user in `seed.sql` plus a session the template establishes.
- **DHIS2+CHAP:** the demo data is the **Sierra Leone metadata/demo DB** (`databases.dhis2.org`, restored
  on first boot via the `dhis2-db-dump` job → Postgres `initdb`). `forge bake dhis2-chap` does this import
  **once** and snapshots the result; per run, Postgres clones it via `CREATE DATABASE … TEMPLATE <seed>` in
  **seconds** (not a re-import). The known dev login (`admin` / `district`) is pre-established as a session
  so the proxied URL opens *inside* the app, not at a login wall. The CHAP↔DHIS2↔frontend wiring (the
  Route record + analytics) is created by the template's bootstrap job (§11b) — not left to the user.
  *(Gotcha to carry into the plan: the demo dump is served uncompressed despite its `.sql.gz` name.)*
- **Degradation:** if auto-login or seed can't be bootstrapped for a stack, the env still comes up healthy
  and the URL is delivered — Forge reports "running, log in / seed manually" rather than failing the run.

---

## 8. URL exposure

- One always-on **Caddy** container listens on a fixed host port. `*.forge.localhost` resolves to
  `127.0.0.1` automatically in browsers/macOS, so **no `/etc/hosts` edits** and **no port memorization**.
- Per run, `forged` writes a route `run-<id>.forge.localhost → 127.0.0.1:<allocated web port>` and reloads
  Caddy. On reap, the route is removed.
- **Only the web service** is published to the host. Internal services (chap-core, dhis2, db, redis) are
  reachable between containers by service name but not exposed — smaller surface, fewer port collisions.
- The URL is printed by `forge run`, recorded in `runs/<id>/meta.json` + the SQLite `envs` row, and
  surfaced by `forge status`. CLI may auto-open the browser when the env turns healthy.

---

## 9. Caching — `forge bake` (the cost-layer win)

Per the original §7 cost model, split the **expensive cached layer** (built offline) from the **cheap
per-run layer** (the hot path):

| Layer | Cost | When | Mechanism |
|---|---|---|---|
| Base images | minutes, once | `forge bake` | `docker pull` (Docker layer cache) |
| Seed artifact | minutes, once per seed version | `forge bake` | boot service, import metadata/data, dump → versioned, content-addressed file |
| **Fresh logical instance** | **seconds, every run** | hot path | Postgres `CREATE DATABASE x TEMPLATE seed` clone → attach service |

The residual per-run cost is the service's own boot (DHIS2's JVM start is the floor). A **warm pool**
(pre-booted service, swap its DB) is a post-MVP optimization that drops in behind the same interface.

---

## 10. What the live app unlocks (reqs: reproduce, screenshots, memory, commit-as-you)

These follow naturally once the app is running and reachable from the worker's network, and are folded in:

- **Reproduce-first.** The worker is instructed (and able) to hit the live app and **reproduce the
  reported bug before fixing**, capturing a failing screenshot/log as evidence. (Reproduction was
  impossible in Phase 1 — there was no running app.)
- **Screenshots.** Playwright runs against the proxied URL (or in-network URL); images are saved to
  `runs/<id>/screenshots/` and embedded in the PR body. (Slack delivery deferred.)
- **Worker session continuity.** Today each fix iteration is a fresh `claude -p` with **no memory**
  (`orchestrator.py:82` passes `None`). Switch to `--resume <session_id>` so the worker retains context
  across iterations within a run — a small change with large payoff for multi-iteration fixes.
- **Commit as you.** Today commits are authored as `Forge <forge@localhost>` (`commands.py:29-30`). Change
  the git author to **your** name/email (from config, defaulting to `gh api user` / host git config) so PR
  commits are yours, satisfying the "commit in my name" requirement. The PR is already opened under your
  `gh` identity.

> **Out of scope here (noted):** *durable cross-run* repo memory (the agent accumulating learned notes
> about a repo across runs) is a larger, separate gap from the original "learn and retain knowledge" goal.
> The `--resume` change above is the cheap within-run slice; the cross-run version gets its own spec.

---

## 11. The two proving templates

Concrete manifests. Exact image tags / ports are **pinned in the implementation plan** against upstream
(see Open Questions §17); the shapes below are the design.

### 11a. `next-supabase` (proving ground — built first)

```
template: next-supabase
  detect:    supabase/config.toml present  AND  next in package.json deps
  services (Supabase local stack, started via the Supabase CLI):
    kong       API gateway        host :54321   ← fronts auth/rest/realtime/storage
    postgres   database           host :54322
    studio     dashboard          host :54323
    mailpit    email testing      host :54324
    (gotrue/postgrest/realtime/storage sit BEHIND kong on :54321 — no own host ports)
    web:       next dev (the repo)               ← published web service (the URL)
    worker:    forge worker (claude -p)
  web_port:    3000 (next dev)
  health:      GET http://web:3000/ → 200   AND   GET http://kong:54321/ reachable
  seed:        supabase db reset   (supabase/migrations/* then supabase/seed.sql)
  wire:        NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321
               NEXT_PUBLIC_SUPABASE_ANON_KEY=<fixed deterministic local JWT>   → web/.env.local
  login:       seeded user + session (template hook)
  ci-gotcha:   exclude the flaky analytics stack: supabase start -x logflare,vector
               (Logflare/vector is the #1 local-startup failure point)
```

Why first: fully scriptable via the Supabase CLI, fast boot, forgiving, and the local keys are fixed/
known — validates the entire engine (compose-per-run, proxy, URL, seed, health, screenshots, warm/reap,
commit-as-you) cheaply, with almost no per-repo guesswork.

### 11b. `dhis2-chap` (the real unknown — built second)

CHAP is a **multi-repo** stack: no single repo's compose brings up all three pieces (chap-core's own
`compose.yml` is backend-only; the frontend is a separate repo). So CHAP legitimately uses the
**template path** (§5 case 3) rather than pure wrapping — and the template is essentially a generalization
of the canonical "all three together" stack that already lives in **`chap-frontend/docker/`**
(`compose.dhis2.yml` + `compose.chap.yml` + `dhis.conf`), which is the authoritative reference.

```
template: dhis2-chap@<dhis2 ver>-seed<n>     (e.g. dhis2/core:2.42)
  detect:    repo is chap-core (compose.yml + chap_core/) OR chap-frontend (d2.config.js id a29851f9…)
  services:
    dhis2-db    ghcr.io/baosystems/postgis        internal       (DHIS2's database)
    dhis2-web   dhis2/core:2.42                    host :8080     (platform + app + login)
    chap        ghcr.io/dhis2-chap/chap-core       :8000          (FastAPI; /docs, /health)
    worker      ghcr.io/dhis2-chap/chap-worker     internal       (Celery; heavy: R+INLA)
    redis       valkey/valkey:8                    internal       (Celery broker)
    chap-db     postgres:17                        internal       (chap-core database)
    frontend    modeling-app dev server (pnpm/Vite, d2-app-scripts start)  :3000
    worker(forge) forge worker (claude -p)
  published web (the URL) = the dev surface of the repo under change (see below)
  health:    poll DHIS2  GET /api/system/info.json → 200   (NO compose healthcheck exists — Forge polls)
             AND chap   GET /health → 200   AND frontend served
  seed:      clone baked DHIS2 Sierra-Leone demo DB (CREATE DATABASE … TEMPLATE) ; chap-db migrate
  bootstrap (one-shot job, mirrors chap's dhis2-analytics job):
             1. generate DHIS2 analytics tables
             2. create DHIS2 Route  code=chap → http://chap:8000/**
             3. require  route.remote_servers_allowed = http://chap:8000  in dhis.conf
  wire:      frontend → DHIS2 base (proxy /api → dhis2-web:8080); frontend → chap-core ONLY via
             {dhis2}/api/routes/chap/run/...  (a DHIS2 Route, never a direct URL)
  login:     DHIS2 admin/district session pre-established
  caching:   use chap's compose.ghcr.yml (prebuilt images) so per-run skips local builds
```

**The Route is the load-bearing integration fact:** the modeling-app never calls chap-core directly — it
calls `{dhis2}/api/routes/chap/run/...` and DHIS2 server-side-proxies to `http://chap:8000`. So the
template's bootstrap job *must* create the `chap` Route and the `dhis.conf` allowlist, or the app can't
reach the backend. This is exactly what `chap-frontend/docker/`'s `dhis2-analytics` job does today.

**Fixing chap-core vs chap-frontend** is the same template with a different target repo:
- **Fix chap-frontend** → mount the frontend repo live at `/work`, publish its dev server (**:3000**, hot
  reload), which proxies `/api` to `dhis2-web:8080`. chap-core + DHIS2 come from prebuilt GHCR images +
  baked seed.
- **Fix chap-core** → mount the chap-core repo live at `/work` (its `compose.yml` service, source-mounted
  per `compose.dev.yml`), publish **DHIS2 :8080** with the modeling-app installed so you exercise the
  fixed backend through the real app; expose chap `:8000/docs` too. DHIS2 + frontend come from
  images/seed.

Either way the **full three-piece stack is always up**, only the repo under change is live-mounted, and
exactly the chosen web surface is published to the host as the clickable URL.

> **Heaviness to plan for (verified):** DHIS2 first boot is *minutes* (restore demo DB → Flyway → PostGIS
> → caches) and wants real RAM (Postgres + a JVM; dev can run `-Xmx2g–4g`); the chap `worker` image bundles
> R + INLA and is large. This is why `bake` (prebuilt GHCR images + a snapshotted seed DB) and the warm/
> reap lifecycle matter most here, and why concurrency stays at 1.

---

## 12. CLI surface

```
forge serve                      # start the forged daemon (proxy + queue + reaper + registry)
forge run <owner/repo> "<task>"  # enqueue a run; prints URL when healthy; auto-opens browser
forge status [run_id]            # state, URL, time/iters used, last action; lists live envs
forge up <run_id>                # re-provision a reaped env (same branch, cached images/seed)
forge down <run_id>              # tear an env down now
forge bake <template>            # build cached images + seed artifacts offline
```

`forge run` is a thin client: it submits to `forged` and streams progress. (When Slack lands, the same
submission path is driven by a Slack message instead.)

---

## 13. Data model changes (SQLite)

Extend today's `store.py` schema:

```
envs(run_id PK, project, web_url, web_host_port, state,
     template, created_at, last_seen_at, reaped_at)
artifacts(id, run_id, kind, path, created_at)   -- screenshots, diffs, logs, report
```

`runs` gains (from the original §12, as the system grows toward it): `template`, `pr_is_draft`,
`wall_secs`, `iterations`. The `events` table is unchanged.

---

## 14. Verification gate

Unchanged in ownership and mechanism (orchestrator-owned; `.forge/repo.yml: verification.command` →
auto-detected `package.json` scripts → `.forge/verify.sh`; refuse non-draft PR without real
verification). **Added evidence**, not a new gate: where the template/`repo.yml` declares key screens,
Playwright screenshots of those screens against the live app are attached as evidence in the PR body. The
app being *healthy* is a precondition for the run, not a substitute for the test/lint/build gate.

---

## 15. Security & trust

Unchanged trust model from original §13 ("own, trusted repos"): the worker executes arbitrary repo code
with `GH_TOKEN` present; acceptable for v1, stated not hidden. Additions:
- The per-run Compose project gets its **own network**; service credentials are scoped to it.
- Only **one** port is published per run; the proxy is the single host ingress.
- The Slack token is still never in any container (it will live in `forged`).

---

## 16. Phasing (this spec's build order)

| Step | Deliverable |
|---|---|
| **E0 — Daemon + proxy skeleton** | `forged` long-lived process, env registry table, Caddy container, `*.forge.localhost` routing, `forge serve/status/down`. Prove the URL plumbing with a trivial static service. |
| **E1 — EnvManager + compose-per-run** | Recipe resolver (repo-first), compose project up/health/teardown, worker-as-service on the project network + shared `/work`, published web port → URL. Prove on `node-web` (a plain web app). |
| **E2 — Next+Supabase template (proving ground)** | `next-supabase` template, `supabase db reset` seed, wiring env, auto-login, warm/reap (TTL), `forge up`. Full engine validated end to end on a real stateful stack. |
| **E3 — Live-app affordances** | Reproduce-first worker flow, Playwright screenshots → PR body, `--resume` session continuity, commit-as-you. |
| **E4 — `forge bake` + caching** | Offline base-image pull + seed-artifact build; per-run DB-template clone. |
| **E5 — DHIS2+CHAP template** | `dhis2-chap` template: three-service stack, baked DHIS2 seed, admin session, fix-core-or-frontend. The heavy unknown, on a proven engine. |
| **Later (not this spec)** | Slack delivery; warm pool; concurrency > 1; durable cross-run repo memory; live-DB checkpointing. |

---

## 17. Open questions / to verify in the implementation plan

The CHAP/Supabase topology was verified from upstream sources (see §19); the manifests in §11 reflect it.
What remains genuinely open:

- **chap-core model execution needs Docker-daemon access.** chap-core runs models as *separate
  containers* via the Python `docker` SDK — no `docker.sock` mount is visible in its published compose, so
  the exact mechanism is unconfirmed. *Likely resolution:* for the clickable-URL/fix goal we do **not**
  need to execute real model runs (the app/UI works without one), so v1 can leave model execution
  unsupported and flag it; revisit if a task requires running a model.
- **modeling-app dev proxy flow** — the repo relies on its Docker e2e stack rather than documenting
  `d2-app-scripts start --proxy <DHIS2 URL>`; confirm the exact dev-server proxy/`baseUrl` wiring for the
  chap-frontend live-mount case.
- **DHIS2/CHAP boot time on this Mac** — quantify the residual service-boot cost after `bake`; decide
  whether a warm pool is needed sooner than "post-MVP."
- **chap-core live-mount** — confirm source-mounting via the repo's own `compose.dev.yml` (which
  bind-mounts source and publishes 5432) is the right hot-reload path for a chap-core fix.
- **Supabase "logged-in" bootstrap** — cleanest way to land authenticated (seeded session vs. test-mode
  auth bypass) without per-repo custom code; and confirm classic anon-key naming vs. the newer
  `sb_publishable_…` keys for the target app.
- **Auto-open browser** — confirm the macOS `open <url>` hook and whether to gate it behind a flag.

---

## 18. Non-goals (this spec)

Slack delivery (deferred, but `forged` is built Slack-ready); durable cross-run repo memory; warm pool;
concurrency > 1; live-DB checkpointing; untrusted-repo sandboxing; multi-tenant; provider abstraction;
dollar-based budgeting. (All consistent with the original spec's out-of-scope list.)

---

## 19. Research basis (verified 2026-06-23)

The §11 template manifests are grounded in primary sources, not memory:

- **chap-core** — `github.com/dhis2-chap/chap-core`: `compose.yml` / `compose.dev.yml` / `compose.ghcr.yml`,
  `pyproject.toml`, `Dockerfile.worker`; docs `chap.dhis2.org/.../docker-compose-doc/`. FastAPI :8000,
  Celery worker, Valkey + Postgres 17 (internal).
- **chap-frontend** — `github.com/dhis2-chap/chap-frontend`: `apps/modeling-app/{package.json,d2.config.js}`,
  `src/features/route-api/SetChapUrl.tsx`, and the canonical `docker/{compose.dhis2.yml,compose.chap.yml,
  dhis.conf}` e2e stack. pnpm/Vite App Platform app, :3000, chap calls via the DHIS2 Route.
- **DHIS2 core** — `github.com/dhis2/dhis2-core/blob/master/docker-compose.yml`; `hub.docker.com/r/dhis2/core`;
  `databases.dhis2.org` (Sierra-Leone demo); login admin/district; :8080; DB = `ghcr.io/baosystems/postgis`;
  no web healthcheck (poll `/api/system/info.json`).
- **Supabase** — `supabase.com/docs/guides/local-development/*`, `supabase/cli` `config.toml` template:
  Kong 54321, Postgres 54322, Studio 54323, Mailpit 54324; fixed local JWT keys; `supabase db reset`;
  Logflare/vector as the known CI failure point.

Items still unconfirmed are tracked in §17.
