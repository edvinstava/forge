# Warm-wake snapshots

Forge keeps a slept session **warm** so waking it is a `docker compose start`
(seconds) instead of a full re-clone / install / build / dev-server warmup
(minutes).

## How it works

**Sleep** (`SessionManager.sleep`):
- `docker compose stop` the run's stack — containers, named volumes
  (`node_modules`, `.next`, …), and the on-disk workspace all **survive**.
  (Contrast the old behaviour, `down -v`, which dropped the volumes.)
- Pause Supabase with `supabase stop` **keeping its port reservation and local
  DB** (`_pause_supabase`) so wake reattaches the same instance.
- Record a **lockfile-hash signature** (`snapshot_lockhash`) of the dependency
  lockfile, so wake can tell whether the warm `node_modules` is still valid.

**Wake** (`SessionManager.wake(run_id, fresh=False)`):
- **Warm path** (eligible): the env is `asleep` **and** the recorded lockfile
  hash still matches the workspace **and** `--fresh` was not requested →
  `_provision(warm=True)`, which skips the self-heal probe and uses
  `docker compose start` instead of `up`. `host_pre` still runs, restarting the
  paused Supabase on the same reservation.
- **Cold path** (otherwise): a full `_provision` (clone-equivalent: recipe
  resolve, `compose up`, seed, health). Triggered when the agent changed
  dependencies (lockfile hash differs), on `--fresh`, or when there is no warm
  snapshot.
- **Auto-fallback:** if a warm start comes up unhealthy (env ends `failed`),
  wake tears it down (`down -v`) and retries cold once — a stale or missing
  snapshot never dead-ends the user.

**Dormant GC** (`delete_dormant`, after `dormant_ttl_secs`, default 3 days):
archives the code, then `down -v`s the warm stopped stack, releases Supabase,
and removes the workspace — so warm snapshots don't accumulate forever.

## Trade-offs

- An **asleep** session keeps its named volumes on disk and its Supabase port
  reservation + DB volume until it is deleted. The dev server (the memory hog)
  is **stopped**, so it consumes no CPU/RAM while asleep — only disk.
- Warm-wake is only as valid as the lockfile signature: any dependency change
  the agent made during a turn forces a clean cold provision (correct, but
  slower) on the next wake.

## Risk note

Warm-wake depends on `supabase stop` **preserving** the local Postgres volume
(plain `supabase stop`, never `--no-backup`). This is the standard Supabase CLI
behaviour; if a future CLI version changes it, warm-wake would reconnect to an
empty DB — verify against the CLI version in use, or force `--fresh` waking.
