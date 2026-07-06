import subprocess
from datetime import datetime, timedelta

from forge import compose
from forge.envreg import superseded_run_ids
from forge.proxy import PROXY_NAME


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
        # `down` cannot remove the project network while forge-proxy holds an
        # endpoint on it (the proxy joins every live run's network to route
        # run-<id>.forge.localhost). Detach and remove explicitly — otherwise
        # every reaped env leaks a subnet until Docker's finite address pools
        # run out ("all predefined address pools have been fully subnetted").
        net = f"{project}_default"
        subprocess.run(["docker", "network", "disconnect", "-f", net,
                        PROXY_NAME], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
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


# --- dead project-network sweep (subnet reclamation) ---

def network_run_id(name: str):
    """forge-<run_id>_default → run_id (None for any other network name)."""
    if name.startswith("forge-") and name.endswith("_default"):
        return name[len("forge-"):-len("_default")] or None
    return None


def dead_networks(networks, active_run_ids, container_run_ids) -> list:
    """Pure: project networks safe to remove — the run is not registered
    live/starting/asleep and no containers (running or stopped) remain.
    Warm-slept envs keep stopped containers, so their networks survive."""
    return [n for n in networks
            if (rid := network_run_id(n)) is not None
            and rid not in active_run_ids
            and rid not in container_run_ids]


def _docker_out(argv):
    """stdout on success, None on failure/absence — the sweep is best-effort."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def sweep_dead_networks(store, run=_docker_out) -> list:
    """Remove project networks orphaned by earlier teardowns — a `down` that
    raced the proxy re-attach, a hung-daemon timeout, or leaks predating the
    explicit removal in compose_down_project. Returns the networks removed."""
    nets = run(["docker", "network", "ls", "--format", "{{.Name}}"])
    projects = run(["docker", "ps", "-a", "--format",
                    '{{.Label "com.docker.compose.project"}}'])
    if nets is None or projects is None:
        return []
    active = {e["run_id"] for e in store.list_envs(
        states=("live", "starting", "asleep"))}
    containers = {p[len("forge-"):] for p in projects.split()
                  if p.startswith("forge-")}
    removed = []
    for net in dead_networks(nets.split(), active, containers):
        run(["docker", "network", "disconnect", "-f", net, PROXY_NAME])
        if run(["docker", "network", "rm", net]) is not None:
            removed.append(net)
    return removed
