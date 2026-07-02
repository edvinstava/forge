# Per-run Supabase isolation — concurrent same-repo sessions

**Date:** 2026-06-24
**Status:** approved (design)
**Scope:** `forge web` (SessionManager) only. `forge run` (CLI, concurrency-1) is unchanged.

## Problem

Forge should run multiple concurrent sessions of the *same* repo (e.g. webapp)
without port or state collisions. Investigation shows this **already works** for every
recipe except `next-supabase`:

- **node-web / none / dhis2-chap** run entirely inside a per-run Compose project
  (`forge-<run_id>`, own network + volumes). App host ports are published as
  `127.0.0.1::<port>`, so Docker auto-assigns a free host port per instance and Forge
  reads it back via `docker compose port`. Concurrent same-repo instances already work.
- **`forge web`** already runs up to `FORGE_MAX_SESSIONS` (default 4) sessions at once.

The lone blocker is **Supabase**, which the `next-supabase` recipe runs on the *host* via
the `supabase` CLI (`host_pre: supabase start`), not in the Compose project. The CLI:

1. Names containers by `project_id` in `config.toml` → two sessions of the same repo share
   the *same* containers, and `supabase db reset` wipes the shared DB.
2. Binds **fixed host ports** from `config.toml` (54321 API, 54322 DB, 54320 shadow,
   54329 pooler, 54323 studio, 54324 inbucket, 54327 analytics) → any second Supabase
   collides — including with the developer's own running dev Supabase.
3. Bakes `NEXT_PUBLIC_SUPABASE_URL=http://host.docker.internal:54321` into the app
   container, so even moving the port would not reconnect the app.

## Goal

Each `next-supabase` session gets its own Supabase stack on a **unique `project_id`** and a
**unique, free port block**, with the app wired to that block. Starting at a non-zero block
also means Forge **never touches the developer's own dev Supabase** on the base ports — this
also resolves the destructive `db reset` hazard for a single session.

Non-goals: changing `forge run` concurrency; self-hosting Supabase in Compose (rejected
approach B); reading per-run JWT secrets (deterministic local anon key is sufficient for v1).

## Approach (chosen: A — config rewrite + host CLI)

Before `supabase start`, rewrite the *cloned* workspace's `supabase/config.toml`: a unique
`project_id` and every host-bound local port shifted by a per-run offset chosen by probing
for a free block. Bake the resulting API URL into the generated Compose so the app connects
to the right Supabase. On teardown, `supabase stop` that project and release the block.

`api_port = 54321 + offset` is known the instant the offset is chosen (no need to start
Supabase first), so the app URL is baked deterministically at Compose-generation time.

### Session start flow (`SessionManager.start`)

```
clone → build_probe
  └─ if next-supabase (has supabase/config.toml + Next):
       1. read cloned supabase/config.toml
       2. offset = allocator.reserve(run_id, config_text)        [new]
       3. write rewrite_config(text, project_id, offset) back     [new]
       4. recipe = resolve(..., supabase_offset=offset)           [changed]
            → Compose bakes NEXT_PUBLIC_SUPABASE_URL=
              http://host.docker.internal:<54321+offset>
  create_env → host_pre (supabase start/db reset --workdir ws)    [now binds the offset block]
  compose up → register URL
```

`project_id` becomes `<original>-<run_id[:8]>` (matches Supabase's id charset).

## Components

### `supaports.py` (new) — pure, fully unit-tested

- `PORT_KEYS: set[tuple[str, str]]` — the allowlist of `(section, key)` that are host
  binds: `(api,port)`, `(db,port)`, `(db,shadow_port)`, `(db.pooler,port)`,
  `(studio,port)`, `(inbucket,port)`, `(analytics,port)`, `(edge_runtime,inspector_port)`.
  Deliberately excludes `(auth.email.smtp,port)` = 465 (a remote SMTP setting).
- `base_ports(config_text) -> list[int]` — the declared values for the allowlisted keys.
- `required_ports(config_text, offset) -> list[int]` — `base_ports` each `+ offset`.
- `rewrite_config(config_text, project_id, offset) -> str` — section-aware line rewrite:
  sets `project_id` and shifts every allowlisted port by `offset`. Leaves comments,
  formatting, and non-allowlisted values (e.g. 465) untouched.
- `find_free_offset(base, reserved, is_free, stride=100, max_blocks=20) -> int` — smallest
  `k*stride` for `k≥1` where every `p+offset` is `is_free` **and** `offset ∉ reserved`.
  Raises `NoFreePortBlock` after `max_blocks`. Starting at `k=1` keeps block 0 (the dev's
  base ports) untouched. `stride=100` keeps each ~10-wide block non-overlapping and readable
  (544xx, 545xx, …).
- `default_is_free(port) -> bool` — attempts a `127.0.0.1:<port>` TCP bind; True iff free.

### `SupabaseAllocator` (new, in `supaports.py`) — stateful, lock-guarded

The FastAPI server is a single process, so an in-process `threading.Lock` plus the store
closes the start-time race where two sessions probe the same free block.

- `reserve(run_id, config_text) -> int` — under the lock: read reserved offsets from the
  store, `find_free_offset`, persist `(run_id, offset, project)` to the store, return offset.
- `release(run_id)` — under the lock: delete the reservation.
- `reconcile(active_run_ids)` — drop reservations whose run is no longer active.

Injectable `is_free` and `lock` for tests.

### `store.py` — dedicated reservation table

```sql
CREATE TABLE IF NOT EXISTS supabase_ports (
  run_id TEXT PRIMARY KEY, offset INTEGER NOT NULL, project TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
```
A dedicated table (not columns on `envs`) because reservation happens *before* `create_env`.

- `reserve_supabase(run_id, offset, project)` — `INSERT OR REPLACE`.
- `release_supabase(run_id)` — `DELETE`.
- `list_supabase_offsets() -> list[int]` — currently-reserved offsets.
- `get_supabase(run_id) -> dict` — used by teardown to know it must `supabase stop`.

### `recipe.py` — `next_supabase_recipe(..., offset=0)`

- `api_port = 54321 + offset`; bake `NEXT_PUBLIC_SUPABASE_URL=http://host.docker.internal:<api_port>`
  as a literal into the Compose (replacing the hardcoded `:54321`).
- Anon key stays `${FORGE_SUPABASE_ANON_KEY}` (deterministic local JWT).
- `host_pre`/`host_post` unchanged (`supabase start`/`db reset`/`stop --workdir ws`); they now
  act on the rewritten config's project + ports.
- `resolve(..., supabase_offset=0)` threads the offset through.

### `session.py`

- `__init__` constructs a `SupabaseAllocator(store)`.
- `_recipe_for`: when the probe is `next-supabase`, read config → `reserve` → `rewrite_config`
  → write back → `resolve(supabase_offset=offset)`.
- `end(run_id)`: if `store.get_supabase(run_id)` exists, run `supabase stop --workdir <ws>`
  (host), then `allocator.release(run_id)`, then `reap_project`. **This also fixes a
  pre-existing leak**: session teardown currently never ran `host_post`, so host Supabase
  stacks were orphaned.
- `reconcile`: also `allocator.reconcile(active_run_ids)`.
- Block exhaustion (`NoFreePortBlock`) surfaces as a clean `error` TurnEvent, like the
  `can_start` cap message.

### `config.py`

- Optional `FORGE_SUPABASE_PORT_STRIDE` (default 100) and `FORGE_SUPABASE_MAX_BLOCKS`
  (default 20). No behavior change when unset.

## Data flow

```
config.toml (cloned) ──base_ports──▶ allocator.reserve ──find_free_offset──▶ offset
       │                                     │ (TCP bind probe + store reservations)
       └──rewrite_config(project_id,offset)──┘
                          │
                          ▼ written back to cloned config.toml
  host_pre: supabase start --workdir ws   (binds 543xx+offset, project <repo>-<run8>)
                          │
  recipe Compose: NEXT_PUBLIC_SUPABASE_URL = host.docker.internal:(54321+offset)
                          │
  app container ──────────┴────────────▶ its own isolated Supabase
```

## Error handling

- `NoFreePortBlock` after `max_blocks` → session emits an `error` event; nothing reserved.
- `supabase start` non-zero → existing `host_pre` is best-effort; if the app can't reach
  Supabase, the existing health-gate fails and the session reports a health error (unchanged).
- Crash between reserve and start → `reconcile` on next startup releases the orphan and
  `supabase stop`s it.
- Teardown is idempotent: `release_supabase`/`supabase stop` are no-ops if already gone.

## Testing

- **Pure unit** (`test_supaports.py`): `rewrite_config` shifts only allowlisted keys, sets
  `project_id`, leaves 465 + comments intact; `base_ports`/`required_ports`; `find_free_offset`
  skips OS-busy and reserved blocks, starts at k=1, raises on exhaustion.
- **Allocator** (fake store + fake `is_free`): reserve picks the first free non-reserved
  block; concurrent reserves pick distinct blocks; release frees; reconcile drops stale.
- **Store** (`test_store.py`): reserve/list/release/get round-trip; migration adds the table.
- **Session** (fakes): start on a next-supabase probe reserves + rewrites the cloned config +
  bakes the offset URL; end stops Supabase + releases.
- **Opt-in smoke** (gated on the `supabase` CLI being present), mirroring existing real-Docker
  smoke gating: two offsets bind disjoint ports.

## Files touched

| File | Change |
|---|---|
| `src/forge/supaports.py` | **new** — pure rewrite/probe helpers + `SupabaseAllocator` |
| `src/forge/recipe.py` | `next_supabase_recipe(offset=0)`, `resolve(supabase_offset=0)`, baked URL |
| `src/forge/session.py` | allocate+rewrite in `_recipe_for`; stop+release in `end`; reconcile |
| `src/forge/store.py` | `supabase_ports` table + reserve/release/list/get + migration |
| `src/forge/config.py` | optional `FORGE_SUPABASE_PORT_STRIDE` / `FORGE_SUPABASE_MAX_BLOCKS` |
| `tests/…` | `test_supaports.py` (new) + additions to store/session tests + opt-in smoke |

DHIS2 and node-web recipes are **untouched** — they already isolate correctly.
