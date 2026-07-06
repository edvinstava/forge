# Self-heal corrupted Next dev servers

**Date:** 2026-07-06
**Status:** approved

## Problem

A forge-served Next.js app runs `next dev` (Turbopack) with a persistent cache
under `/work/.next/dev/cache/turbopack/`. That cache can end up internally
inconsistent — a `.meta` file references an `.sst` cache file that no longer
exists:

```
Failed to open SST file /work/.next/dev/cache/turbopack/<hash>/00000072.sst
  → No such file or directory (os error 2)
Error: ENOENT ... /work/.next/dev/server/app/(auth-pages)/sign-in/page/build-manifest.json
```

When this happens Turbopack panics and **every route 500s** ("Internal Server
Error") indefinitely. The container is up (returns 500/307, not
connection-refused) but the app is unusable. In the live workspace
(`#live=<id>`) this surfaces as a bare "Internal Server Error" in the app pane,
and it silently blocks the agent's own QA/verify.

Observed 2026-07-06 on `dev-otta/opplandstaal` (#3eba…): the agent's verification
run did `rm -rf .next` against a live `next dev`, leaving the cache
inconsistent. Recovery required a human to stop the web container, delete
`.next`, and restart — a plain `docker restart` does **not** fix it, because the
web entrypoint only clears `.next/dev/lock`, not the corrupted cache.

## Goal

forge detects a live app stuck in this corruption state and auto-recovers it
(clear `.next` + restart the dev server), so the workspace never shows a dead
app and the agent's QA isn't silently blocked. Recovery must be **surgical** —
it must not restart apps that 5xx for legitimate reasons (a real bug the agent
wrote), and it must not loop forever.

## Design

### Detection — where

Piggyback on the existing `reap_loop` daemon thread in `webapp.py` (30 s tick).
After the idle/dormant passes each tick, call a new
`manager.heal_corrupted_web()`. No new thread, no new interval — the steady-state
cost is one health probe per live web app per 30 s.

### Detection — how

For each live env with a `web_service` (from `store.list_envs(states=("live",))`,
which records `web_service` and `web_port`), reuse the compose-network exec
pattern already used by provisioning's health poll (`session.py` `_register`,
which execs `curl` from the `forge` worker against the `web` service):

```
curl -s -o /dev/null -w '%{http_code}' http://<web_service>:<web_port><health_path>
```

run via `env.exec(..., service="forge")`. Interpret:

- **5xx** → *candidate*. Fetch `env.logs(web_service)` (tail the last ~20 KB) and
  test for the corruption signature. Heal only if it matches.
- **2xx / 3xx** → healthy; reset that run's heal counter (a new episode later
  gets a fresh attempt budget).
- **connection error / empty status** → skip. A container that's down or
  mid-restart is a different failure (handled by `reconcile`/restart policy), not
  this heal.

### Corruption signature (pure function)

`webheal.is_corruption(log_text: str) -> bool` returns True when the tailed log
contains any of:

- `Failed to open SST file`
- `Unable to open static sorted file`
- `TurbopackInternalError`
- an ENOENT referencing `.next/dev/` … `build-manifest.json`

Pure and unit-tested against the real corrupt-log sample plus negatives (a normal
500 stack trace, empty logs).

### Recovery

Clear the cache via the `forge` worker (it shares the `/work` bind mount, so it
works regardless of the web container's state), then restart just the web
service:

```python
env.exec(["sh", "-lc", "rm -rf /work/.next"], service="forge")
env.restart(web_service)          # new ComposeEnv method
```

`env.restart(service)` runs `docker compose … restart <service>`, which
SIGTERM/SIGKILLs the web container's PID 1 and reruns its entrypoint
(`rm -f .next/dev/lock; bun install && bun run dev`). With `.next` already
removed, Turbopack rebuilds from clean (~5 s to first 200, verified manually).
The small window between the `rm` and the restart is benign: a fresh `.next`
built by the new process carries no dangling references (the corruption came
from a specific rm-during-write, not from a partial cache).

We deliberately do **not** clear `.next` in the entrypoint on every
restart/wake — that would throw away a healthy cache and force a slow recompile
on every routine wake. Healing is the exception, triggered only by the
signature.

### Loop guard

In-memory state on the manager, keyed by run_id: `{attempts, last_ts}`.

- Heal only if `attempts < max_attempts` (default **2**) **and**
  `now - last_ts > cooldown` (default **180 s**).
- A run observed healthy resets its entry (counter → 0).
- If a run is still 5xx + signature after `max_attempts`, stop and log — clearing
  `.next` isn't fixing it, so no infinite churn.

State is in-memory (not persisted): a forge restart re-arms healing, which is the
correct behaviour (a fresh process should try again).

### Turn-safety

Heal proceeds regardless of any running agent turn. The `web`/dev service is a
separate container from the `forge` worker, so restarting it never kills an
in-flight turn — and a dead dev server was already blocking the agent's own QA.
Each heal emits a store event + bus event (`self_heal`, message e.g. "cleared
corrupted Next cache, restarted dev server") so it is visible in the chat/live
feed.

### Config

- Gated by the existing `cfg.self_heal` master switch (off → monitor does
  nothing).
- `FORGE_WEB_HEAL_MAX_ATTEMPTS` (default 2), `FORGE_WEB_HEAL_COOLDOWN_SECS`
  (default 180).

## Components

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `webheal.is_corruption(text)` | pure signature match | — |
| `webheal.status_probe_argv(host, port, path)` | build the curl-status argv | — |
| `ComposeEnv.restart(service)` | per-service compose restart | `compose` cmd builder |
| `SessionManager.heal_corrupted_web()` | iterate live envs, probe, gate, heal, emit events | `_env_for`, `store`, `bus`, `webheal` |
| `webapp.py` `reap_loop` | call `heal_corrupted_web()` once per tick | manager |

## Testing

- `is_corruption`: real corrupt-log sample (positive) + normal-500 trace and
  empty (negative).
- `status_probe_argv`: shape/URL correctness.
- `heal_corrupted_web` with a fake env/store/bus:
  - heals only when probe is 5xx **and** signature matches;
  - skips healthy (2xx/3xx) and resets the counter;
  - skips unreachable (empty status);
  - respects `cooldown` and `max_attempts` (no heal past the budget);
  - emits a `self_heal` event on heal.
- `ComposeEnv.restart` builds the expected compose argv (fake subprocess).

## Out of scope

- Fixing the agent's verify step that does `rm -rf .next` on a live dev server
  (the upstream trigger). Separate change.
- Non-Next apps / other corruption modes — the signature is Next/Turbopack
  specific by design.
