"""Caddy reverse proxy giving each live env a stable, readable URL
(`run-<id>.forge.localhost`). `*.localhost` resolves to 127.0.0.1 in browsers
with no /etc/hosts edits. The raw `http://localhost:<port>` always works too;
the proxy is an enhancement layered by the `forge serve` daemon."""
import subprocess
from collections import namedtuple

from forge.supaports import SUPABASE_BASE_API_PORT

PROXY_NAME = "forge-proxy"
DEFAULT_DOMAIN = "forge.localhost"
DEFAULT_PORT = 8088

# Supabase API path prefixes routed to the host Supabase (Kong gateway). Caddy's
# @supabase matcher wins over the bare app fallthrough regardless of order.
SUPABASE_PATHS = "/rest/* /auth/* /storage/* /realtime/* /functions/* /graphql/*"

Route = namedtuple("Route", "host web supabase")


def container_name(run_id, service):
    # compose v2 default container name: <project>-<service>-<index>
    return f"forge-{run_id}-{service}-1"


def local_url(run_id, domain=DEFAULT_DOMAIN, port=DEFAULT_PORT):
    """The proxy URL for a run on the forge host. `*.localhost` resolves to
    127.0.0.1 in every browser with no /etc/hosts or external DNS, so this opens
    even when the public tunnel hostname can't be resolved on the host's network
    (e.g. a router that NXDOMAINs *.trycloudflare.com via rebind protection)."""
    return f"http://run-{run_id}.{domain}:{port}"


def project_network(run_id):
    return f"forge-{run_id}_default"


def routes_for(envs, supabase_offsets=None, domain=DEFAULT_DOMAIN):
    """live envs → [Route(host, web_upstream, supabase_upstream_or_None)]. Caddy
    sits on each run's compose network and reaches the web container by name on
    its in-container port — avoiding the host.docker.internal / loopback-publish
    trap. When a run's Supabase offset is known, the same site also fronts the
    host Supabase so the browser hits one origin for both the app and its API."""
    supabase_offsets = supabase_offsets or {}
    out = []
    for e in envs:
        if e.get("web_port") and e.get("web_service"):
            host = f"run-{e['run_id']}.{domain}"
            web = f"http://{container_name(e['run_id'], e['web_service'])}:{e['web_port']}"
            offset = supabase_offsets.get(e["run_id"])
            supabase = (f"http://host.docker.internal:{SUPABASE_BASE_API_PORT + offset}"
                        if offset is not None else None)
            out.append(Route(host, web, supabase))
    return out


def connect_networks(run_ids, name=PROXY_NAME) -> None:
    """Attach the proxy to each run's compose network so it can resolve the
    web container by name (idempotent — already-connected is fine)."""
    for rid in run_ids:
        subprocess.run(["docker", "network", "connect",
                        project_network(rid), name], capture_output=True)


def caddy_config(routes, listen_port=DEFAULT_PORT) -> str:
    """Render a Caddyfile. Each Route maps a hostname to its web upstream; when
    the Route carries a Supabase upstream, Supabase API paths are split off to
    it (same-origin, so no CORS) and everything else falls through to the app."""
    blocks = []
    for r in routes:
        lines = [f"http://{r.host}:{listen_port} {{"]
        if r.supabase:
            lines.append(f"\t@supabase path {SUPABASE_PATHS}")
            lines.append(f"\treverse_proxy @supabase {r.supabase}")
        lines.append(f"\treverse_proxy {r.web}")
        lines.append("}")
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else "# no live envs\n"


# --- imperative (real Docker) ---

def _running(name) -> bool:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True)
    return name in out.stdout.split()


def ensure_proxy(caddyfile_path: str, listen_port=DEFAULT_PORT,
                 image="caddy:2", name=PROXY_NAME) -> None:
    """Start (or leave running) the forge-proxy Caddy container, serving the
    given Caddyfile and reachable from the host on `listen_port`. `name` defaults
    to the singleton PROXY_NAME; pass a unique name (and port) to run a throwaway
    proxy that won't collide with — or tear down — a live daemon's proxy."""
    if _running(name):
        return
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name,
         "--add-host", "host.docker.internal:host-gateway",
         "-p", f"127.0.0.1:{listen_port}:{listen_port}",
         "-v", f"{caddyfile_path}:/etc/caddy/Caddyfile:ro",
         image],
        capture_output=True)


def reload_proxy(name=PROXY_NAME) -> None:
    subprocess.run(
        ["docker", "exec", name, "caddy", "reload",
         "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
        capture_output=True)
