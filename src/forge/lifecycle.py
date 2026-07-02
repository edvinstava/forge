import subprocess
from datetime import datetime, timedelta

from forge import compose
from forge.envreg import superseded_run_ids


def reap_env(runner, store, run_id) -> None:
    runner.stop(f"forge-{run_id}")
    store.mark_reaped(run_id)


def reap_superseded(runner, store, keep_run_id) -> list:
    """Reap every live/starting env except `keep_run_id` (concurrency 1)."""
    ids = superseded_run_ids(store.list_envs(states=("live", "starting")), keep_run_id)
    for rid in ids:
        reap_env(runner, store, rid)
    return ids


# --- compose-project reaping (multi-service envs) ---

def compose_down_project(project: str) -> None:
    # `down` by project name finds containers via labels — no -f needed
    try:
        subprocess.run(["docker", "compose", "-p", project, "down", "-v",
                        "--remove-orphans"], capture_output=True)
    except FileNotFoundError:
        pass   # docker absent (e.g. CI without docker) — registry state still updated


def reap_project(store, run_id, downer=compose_down_project) -> None:
    downer(compose.project_name(run_id))
    store.mark_reaped(run_id)


def reap_superseded_projects(store, keep_run_id, downer=compose_down_project) -> list:
    """Concurrency 1: tear down every other live/starting compose env."""
    ids = superseded_run_ids(store.list_envs(states=("live", "starting")), keep_run_id)
    for rid in ids:
        reap_project(store, rid, downer)
    return ids


def _aged_run_ids(envs, now: datetime, ttl_secs: int, field: str) -> list:
    """run_ids whose `field` timestamp is older than `ttl_secs` (pure). SQLite
    stores timestamps as 'YYYY-MM-DD HH:MM:SS' (UTC); compare against a UTC
    `now`."""
    cutoff = now - timedelta(seconds=ttl_secs)
    out = []
    for e in envs:
        ts = e.get(field)
        if not ts:
            continue
        try:
            seen = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if seen < cutoff:
            out.append(e["run_id"])
    return out


def idle_run_ids(envs, now: datetime, ttl_secs: int) -> list:
    """live envs idle (no activity) longer than `ttl_secs` → ready to sleep."""
    return _aged_run_ids(envs, now, ttl_secs, "last_seen_at")


def dormant_run_ids(envs, now: datetime, ttl_secs: int) -> list:
    """asleep envs dormant longer than `ttl_secs` → ready to delete."""
    return _aged_run_ids(envs, now, ttl_secs, "asleep_at")


def reap_idle(store, now: datetime, ttl_secs: int,
              downer=compose_down_project) -> list:
    """Reap every live env idle longer than ttl_secs."""
    ids = idle_run_ids(store.list_envs(states=("live",)), now, ttl_secs)
    for rid in ids:
        reap_project(store, rid, downer)
    return ids
