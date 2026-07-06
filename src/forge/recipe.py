"""Resolve a cloned repo into a concrete environment recipe.

Precedence (first match wins):
  1. .forge/env.yml committed in the repo, declaring the app
     (dev_cmd + web_port)                            → synthesized from it
  2. CHAP markers (multi-repo stack)                 → dhis2-chap template
  3. supabase/config.toml + Next                     → next-supabase template
  4. package.json (JS app)                           → node-web template
  5. repo's own docker-compose.yml / compose.yml     → wrap it
  6. learned overlay declaring the app               → synthesized from it
  7. else                                            → none (worker only, no app)

Note: the repo's own compose is wrapped (5) only when the repo is not a
recognized JS app (4). A JS repo that also ships an incidental compose (e.g. a
bare db service) still runs via its dev script — wrapping such a compose would
start the db but never the app. A pure compose repo (no package.json) is the
case this rescues from the worker-only `none` fallback.

(6) is what lets Forge spin up repos with NO recognized marker at all — Python,
Go, Rust, Ruby, JVM, static sites, anything: the self-heal probe agent reads
the repo (README, manifests, code), emits an overlay describing how to run it
(image, setup_cmds, dev_cmd, web_port, extra services), and the resolver
synthesizes a compose from that description. Learned once per repo, repaired by
the same overlay-delta loop as everything else. An env.yml that validates but
does NOT declare the app acts as an overlay patch on whatever resolves below
it; an invalid env.yml is ignored (never fatal). A committed env.yml runs with
the same trust as package.json scripts or the repo's own compose — containment
is the container, not the recipe source.

A Recipe with `multi=True` is provisioned via ComposeEnv; `compose` is a plain
dict serialized as JSON (valid YAML) to runs/<id>/forge-compose.yml.
"""
import copy
import dataclasses
import json
import os
import re
from dataclasses import dataclass, field

import yaml

from forge import knowledge
from forge.supaports import SUPABASE_BASE_API_PORT

# The Supabase local anon key is a fixed, deterministic JWT (signed from the
# default local JWT secret) — identical on every machine, safe to hardcode.
SUPABASE_LOCAL_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
# Note: the embedded key above is the documented local-dev anon JWT. Forge also
# resolves the live key from `supabase status -o env` at provision time when
# available; the constant is the zero-config fallback.


@dataclass(frozen=True)
class Probe:
    package_json: str | None = None
    repo_yml: str | None = None
    env_yml: str | None = None
    has_supabase_config: bool = False
    has_d2_config: bool = False
    is_chap_core: bool = False
    is_chap_frontend: bool = False
    repo_compose_path: str | None = None
    repo_compose_text: str | None = None
    pkg_manager: str = "npm"


@dataclass(frozen=True)
class Recipe:
    name: str
    multi: bool
    compose: dict | None = None
    web_service: str | None = None
    web_port: int | None = None
    health_path: str = "/"
    host_pre: tuple = ()      # host commands before compose up (e.g. supabase start)
    host_post: tuple = ()     # host commands after teardown (e.g. supabase stop)
    seed: tuple = ()          # (service, argv) run via compose exec once healthy
    notes: str = ""
    confidence: str = "high"        # "high" | "low" — gates the self-heal probe
    pkg_manager_used: str = "npm"   # remembered so an overlay can rebuild the cmd
    script_used: str = "dev"
    test_cmds: tuple = ()           # canonical test commands (from package.json)
    synth_overlay: dict | None = None   # synthesized recipes: the overlay they
                                        # were built from, so a repair delta can
                                        # rebuild the compose (cf. script_used)

    def runtime_facts(self, app_url: str | None = None) -> dict:
        """Operational facts for the agent's prompt: the reachable endpoints and
        canonical commands of the *already running* environment, derived from the
        resolved recipe. Deliberately tiny — no secrets, no full env, no service
        topology; just what the agent would otherwise waste turns rediscovering
        (and get wrong, e.g. assuming localhost instead of the container-DNS URL).
        `app_url` overrides the derived internal URL when the caller has a better
        one (e.g. the proxy URL)."""
        app = app_url or (f"http://{self.web_service}:{self.web_port}"
                          if self.web_service else None)
        is_js = self.name in ("node-web", "next-supabase")
        if is_js:
            dev_cmd = f"{self.pkg_manager_used} run {self.script_used}"
        elif self.name == "synthesized":
            dev_cmd = (self.synth_overlay or {}).get("dev_cmd")
        else:
            dev_cmd = None
        return {
            "stack": self.name,
            "app": app,
            "endpoints": self._endpoints(app),
            "pkg_manager": self.pkg_manager_used if is_js else None,
            "dev_cmd": dev_cmd,
            "test_cmds": list(self.test_cmds) if is_js else [],
        }

    def _endpoints(self, app) -> list:
        """(label, url) pairs for the extra services the agent can reach, by
        recipe. Only reachable, non-secret endpoints; the app's own URL is
        excluded (it's the `app` fact) so it never appears twice."""
        out = []
        if self.name == "next-supabase" and self.compose:
            web = self.compose.get("services", {}).get(self.web_service, {})
            url = _env_get(web, "NEXT_PUBLIC_SUPABASE_URL")
            if url:
                out.append(("Supabase", url))
        elif self.name == "dhis2-chap":
            out += [("DHIS2", "http://dhis2-web:8080"), ("CHAP", "http://chap:8000")]
        return [ep for ep in out if ep[1] != app]   # never repeat the app URL


def _scripts(package_json):
    if not package_json:
        return {}
    try:
        return json.loads(package_json).get("scripts", {})
    except json.JSONDecodeError:
        return {}


def _dev_script(package_json):
    scripts = _scripts(package_json)
    for name in ("dev", "start"):
        if name in scripts:
            return name
    return None


def _test_cmds(package_json, pkg_manager) -> tuple:
    """Canonical test commands for the agent's runtime facts: the repo's own unit
    and e2e scripts, phrased for the detected package manager. Empty when the repo
    defines neither."""
    scripts = _scripts(package_json)
    run = _PKG_RUN.get(pkg_manager, _PKG_RUN["npm"])
    out = []
    if "test" in scripts:
        out.append(f"{run} test")
    for e2e in ("test:e2e", "e2e"):
        if e2e in scripts:
            out.append(f"{run} {e2e}")
            break
    return tuple(out)


_PKG_INSTALL = {"bun": "bun install", "pnpm": "pnpm install",
                "yarn": "yarn install", "npm": "npm install"}
_PKG_RUN = {"bun": "bun run", "pnpm": "pnpm run", "yarn": "yarn run", "npm": "npm run"}


def detect_pkg_manager(host, ws) -> str:
    """Pick the package manager from the committed lockfile (bun → pnpm → yarn →
    npm). Wrong-PM installs (e.g. npm in a bun repo) synthesize a lockfile that
    pollutes the PR, so this is load-bearing, not cosmetic."""
    if host.exists(ws, "bun.lockb") or host.exists(ws, "bun.lock"):
        return "bun"
    if host.exists(ws, "pnpm-lock.yaml"):
        return "pnpm"
    if host.exists(ws, "yarn.lock"):
        return "yarn"
    return "npm"


def web_command(pkg_manager: str, script: str, port: int, apt=()) -> str:
    """The web service's `sh -lc` command: optional apt deps (long-tail system
    libs; common ones are baked into the image), then install, then the dev
    server on PORT. corepack (pnpm/yarn) and bun are provided by the image."""
    install = _PKG_INSTALL.get(pkg_manager, _PKG_INSTALL["npm"])
    run = _PKG_RUN.get(pkg_manager, _PKG_RUN["npm"])
    pre = (f"apt-get update && apt-get install -y --no-install-recommends "
           f"{' '.join(apt)} && " if apt else "")
    return f"{pre}{install} && PORT={port} {run} {script}"


def _worker_service(workspace, worker_image, env_refs):
    # the worker image's ENTRYPOINT is `sleep infinity`; override it so our
    # `command` runs via a shell instead of being appended as sleep args.
    # Both agent-CLI tokens are always referenced (:- keeps compose quiet when
    # unset) so switching FORGE_PROVIDER never requires re-resolving recipes.
    return {
        "image": worker_image,
        "working_dir": "/work",
        "volumes": [f"{workspace}:/work"],
        "entrypoint": ["sh", "-lc"],
        "command": ["sleep infinity"],   # single-elem list: whole script → one -lc arg
        "environment": {"CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN:-}",
                        "OPENAI_API_KEY": "${OPENAI_API_KEY:-}",
                        "GH_TOKEN": "${GH_TOKEN:-}", **env_refs},
    }


# --- generators --------------------------------------------------------------

def node_web_recipe(workspace, worker_image, package_json, port=3000,
                    pkg_manager="npm") -> Recipe:
    detected = _dev_script(package_json)
    script = detected or "dev"
    compose = {
        "services": {
            "web": {
                "image": worker_image,
                "working_dir": "/work",
                "volumes": [f"{workspace}:/work"],
                "entrypoint": ["sh", "-lc"],
                "command": [web_command(pkg_manager, script, port)],
                "ports": [f"127.0.0.1::{port}"],
            },
            "forge": _worker_service(workspace, worker_image, {
                "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}",
                "GH_TOKEN": "${GH_TOKEN}",
            }),
        }
    }
    return Recipe("node-web", True, compose, "web", port, "/",
                  confidence="high" if detected else "low",
                  pkg_manager_used=pkg_manager, script_used=script,
                  test_cmds=_test_cmds(package_json, pkg_manager))


def next_supabase_recipe(workspace, worker_image, port=3000, offset=0,
                         pkg_manager="npm", package_json=None) -> Recipe:
    """Next dev server + worker in a compose project; Supabase brought up on the
    host via its CLI (the user's normal workflow) and reached at
    host.docker.internal:<api_port>. `offset` shifts the Supabase port block so
    several sessions of the same repo can run at once (the cloned config.toml is
    rewritten to match); api_port = 54321 + offset."""
    api_port = SUPABASE_BASE_API_PORT + offset
    compose = {
        "services": {
            "web": {
                "image": worker_image,
                "working_dir": "/work",
                "volumes": [f"{workspace}:/work"],
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "environment": {
                    "NEXT_PUBLIC_SUPABASE_URL": f"http://host.docker.internal:{api_port}",
                    "NEXT_PUBLIC_SUPABASE_ANON_KEY": "${FORGE_SUPABASE_ANON_KEY}",
                },
                "entrypoint": ["sh", "-lc"],
                "command": [web_command(pkg_manager, "dev", port)],
                "ports": [f"127.0.0.1::{port}"],
            },
            "forge": _worker_service(workspace, worker_image, {
                "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}",
                "GH_TOKEN": "${GH_TOKEN}",
            }),
        }
    }
    return Recipe(
        "next-supabase", True, compose, "web", port, "/",
        host_pre=(
            ["supabase", "start", "--workdir", workspace],
            ["supabase", "db", "reset", "--workdir", workspace],
        ),
        host_post=(["supabase", "stop", "--workdir", workspace],),
        notes="Supabase via local CLI on host:54321; anon key is the fixed local JWT.",
        pkg_manager_used=pkg_manager, script_used="dev",
        test_cmds=_test_cmds(package_json, pkg_manager),
    )


def dhis2_chap_recipe(workspace, worker_image, target, dhis2_image="dhis2/core:2.42",
                      target_repo="frontend", seed_dir=None) -> Recipe:
    """DHIS2 + chap-core (+worker/redis/db) + the modeling-app frontend, all on
    one project network. The repo under change is live-mounted at /work; the
    other pieces come from prebuilt GHCR images + a baked DHIS2 seed.

    The modeling-app reaches chap-core ONLY through a DHIS2 Route
    ({dhis2}/api/routes/chap/run/...), so a bootstrap step creates that Route
    and DHIS2's dhis.conf must allow http://chap:8000.
    """
    pg = "ghcr.io/baosystems/postgis:18-3.6"
    seed = seed_dir or f"{workspace}/.forge/dhis2-seed"   # populated by `forge bake`
    services = {
        "dhis2-db": {
            "image": pg,
            "environment": {"POSTGRES_DB": "dhis2", "POSTGRES_USER": "dhis",
                            "POSTGRES_PASSWORD": "dhis"},
            "volumes": [f"{seed}:/docker-entrypoint-initdb.d:ro"],
        },
        "dhis2-web": {
            "image": dhis2_image,
            "environment": {"DB_HOSTNAME": "dhis2-db", "DB_NAME": "dhis2",
                            "DB_USERNAME": "dhis", "DB_PASSWORD": "dhis",
                            "JAVA_TOOL_OPTIONS": "-Xmx3g"},
            "ports": ["127.0.0.1::8080"],
            "depends_on": ["dhis2-db"],
        },
        "chap-db": {"image": "postgres:17",
                    "environment": {"POSTGRES_USER": "chap", "POSTGRES_PASSWORD": "chap",
                                    "POSTGRES_DB": "chap"}},
        "chap-redis": {"image": "valkey/valkey:8"},
        "chap": {
            "image": "ghcr.io/dhis2-chap/chap-core:latest",
            "environment": {
                "CHAP_DATABASE_URL": "postgresql://chap:chap@chap-db:5432/chap",
                "REDIS_HOST": "chap-redis", "REDIS_PORT": "6379",
                "CELERY_BROKER": "redis://chap-redis:6379/0", "PORT": "8000"},
            "ports": ["127.0.0.1::8000"],
            "depends_on": ["chap-db", "chap-redis"],
        },
        "chap-worker": {
            "image": "ghcr.io/dhis2-chap/chap-worker:latest",
            "environment": {
                "CHAP_DATABASE_URL": "postgresql://chap:chap@chap-db:5432/chap",
                "CELERY_BROKER": "redis://chap-redis:6379/0"},
            "depends_on": ["chap-db", "chap-redis"],
        },
        "forge": _worker_service(workspace, worker_image, {
            "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}",
            "GH_TOKEN": "${GH_TOKEN}"}),
    }

    if target_repo == "frontend":
        # live-mount the modeling-app; serve its dev server, proxied to DHIS2
        services["frontend"] = {
            "image": worker_image,
            "working_dir": "/work",
            "volumes": [f"{workspace}:/work"],
            "entrypoint": ["sh", "-lc"],
            "command": ["corepack enable && pnpm install && "
                        "pnpm --filter=@dhis2-chap/modeling-app... start "
                        "--proxy http://dhis2-web:8080 --port 3000"],
            "ports": ["127.0.0.1::3000"],
            "depends_on": ["dhis2-web", "chap"],
        }
        web_service, web_port, health = "frontend", 3000, "/"
    else:
        # fixing chap-core: live-mount chap-core; the modeling-app is the
        # installed DHIS2 app, so the clickable surface is DHIS2 itself
        services["chap"]["volumes"] = [f"{workspace}:/work"]
        services["chap"]["working_dir"] = "/work"
        web_service, web_port, health = "dhis2-web", 8080, "/api/system/info.json"

    return Recipe(
        "dhis2-chap", True, {"services": services}, web_service, web_port, health,
        seed=(
            # bootstrap: generate analytics + create the chap Route → http://chap:8000
            ("dhis2-web", ["bash", "-lc",
                           "curl -fs -u admin:district -X POST "
                           "http://localhost:8080/api/routes -H 'Content-Type: application/json' "
                           "-d '{\"code\":\"chap\",\"name\":\"Chap\",\"url\":\"http://chap:8000/**\"}' "
                           "|| true"]),
        ),
        notes=("Needs `forge bake dhis2-chap` (DHIS2 Sierra-Leone seed) + "
               "route.remote_servers_allowed=http://chap:8000 in dhis.conf. "
               f"Fixing target: {target_repo}."),
    )


# --- repo's own compose -------------------------------------------------------

_WEB_SERVICE_NAMES = ("web", "app", "frontend", "front", "www", "ui", "client",
                      "site", "server", "api", "next", "nextjs")


def _is_rel_path(p) -> bool:
    return isinstance(p, str) and (p.startswith("./") or p.startswith("../")
                                   or p in (".", ".."))


def _abs_in_ws(workspace: str, rel: str) -> str:
    """Anchor a repo-relative path at the cloned workspace. The merged compose
    is written to runs/<id>/, NOT the repo, so a relative build context / bind
    source / env_file would otherwise resolve against the wrong directory."""
    return os.path.normpath(os.path.join(workspace, rel))


def _absolutize_service(svc: dict, workspace: str) -> None:
    """Rewrite a service's repo-relative paths to absolute, in place."""
    b = svc.get("build")
    if _is_rel_path(b):
        svc["build"] = _abs_in_ws(workspace, b)
    elif isinstance(b, dict) and _is_rel_path(b.get("context")):
        b["context"] = _abs_in_ws(workspace, b["context"])
    vols = svc.get("volumes")
    if isinstance(vols, list):
        out = []
        for v in vols:
            if isinstance(v, str) and ":" in v:
                src, _, rest = v.partition(":")
                if _is_rel_path(src):
                    src = _abs_in_ws(workspace, src)
                out.append(f"{src}:{rest}")
            elif isinstance(v, dict) and _is_rel_path(v.get("source")):
                out.append({**v, "source": _abs_in_ws(workspace, v["source"])})
            else:
                out.append(v)
        svc["volumes"] = out
    ef = svc.get("env_file")
    if _is_rel_path(ef):
        svc["env_file"] = _abs_in_ws(workspace, ef)
    elif isinstance(ef, list):
        svc["env_file"] = [_abs_in_ws(workspace, e) if _is_rel_path(e) else e
                           for e in ef]


def _container_port(svc: dict):
    """The port the service listens on inside the container (what the health
    curl and the proxy target), from `ports` (last field) or `expose`."""
    for p in (svc.get("ports") or []):
        if isinstance(p, dict):
            if p.get("target") is not None:
                return int(p["target"])
        else:
            try:
                return int(str(p).split("/")[0].split(":")[-1])
            except ValueError:
                continue
    for e in (svc.get("expose") or []):
        try:
            return int(str(e).split("/")[0])
        except ValueError:
            continue
    return None


def _pick_web_service(services: dict):
    """Best-effort: the published service whose name looks most web-like; else
    the sole service. Returns (name, container_port) or (None, None)."""
    cands = [n for n, s in services.items()
             if n != "forge" and isinstance(s, dict)
             and (s.get("ports") or s.get("expose"))]
    if not cands:
        others = [n for n in services if n != "forge"]
        cands = others if len(others) == 1 else []
    if not cands:
        return None, None

    def rank(n):
        nl = n.lower()
        if nl in _WEB_SERVICE_NAMES:
            return (0, _WEB_SERVICE_NAMES.index(nl))
        for i, w in enumerate(_WEB_SERVICE_NAMES):
            if w in nl:
                return (1, i)
        return (2, 0)

    name = sorted(cands, key=rank)[0]
    return name, _container_port(services[name])


# --- repo compose is untrusted: sanitize before running it on the host -------
#
# A repo's own compose is executed with `docker compose up` on the HOST, so
# every field is untrusted input. These service directives can each breach the
# container boundary — grant extra Linux capabilities, share host namespaces,
# expose host devices, or (via a bind mount) hand over the docker socket or the
# host filesystem. Strip them from every repo service before running the stack.
# Forge's own injected worker/web services are trusted and added AFTER
# sanitizing. Compare knowledge._validate_services, which allowlists the far
# smaller key set a learned env.yml overlay may set.
_UNSAFE_COMPOSE_KEYS = (
    "privileged", "cap_add", "devices", "device_cgroup_rules",
    "pid", "ipc", "uts", "cgroup", "cgroupns_mode", "userns_mode",
    "network_mode", "security_opt", "cgroup_parent", "sysctls", "group_add",
)


def _within_workspace(workspace_real: str, path) -> bool:
    """True if a host path resolves inside the run workspace. Symlinks are
    resolved first so a bind source can't tunnel out through a link."""
    if not isinstance(path, str):
        return False
    ws = workspace_real.rstrip("/")
    real = os.path.realpath(path)
    return real == ws or real.startswith(ws + os.sep)


def _is_bind_source(src) -> bool:
    """A compose volume source is a HOST bind (vs a named volume) when it looks
    like a path: absolute, home-relative, or containing a separator."""
    return isinstance(src, str) and (src.startswith(("/", "~", ".")) or "/" in src)


def _loopback_port(spec):
    """Force a compose `ports` entry to publish on 127.0.0.1 only. A repo's
    short-form '5432:5432' would otherwise bind 0.0.0.0 (every host interface,
    incl. the LAN); forge's own recipes always bind loopback."""
    if isinstance(spec, dict):
        return {**spec, "host_ip": "127.0.0.1"}
    if not isinstance(spec, (str, int)):
        return spec
    body, sep, proto = str(spec).partition("/")
    parts = body.split(":")
    if len(parts) >= 3:                       # host_ip present → force loopback
        parts[0] = "127.0.0.1"
        rebuilt = ":".join(parts)
    elif len(parts) == 2:                     # host:container → pin host_ip
        rebuilt = f"127.0.0.1:{parts[0]}:{parts[1]}"
    else:                                     # container-only → ephemeral loopback
        rebuilt = f"127.0.0.1::{parts[0]}"
    return f"{rebuilt}/{proto}" if sep else rebuilt


def _sanitize_service(svc: dict, name: str, workspace_real: str, removed: list) -> None:
    """Strip boundary-breaching directives, drop bind mounts pointing outside
    the workspace, and pin every published port to loopback — in place."""
    for key in _UNSAFE_COMPOSE_KEYS:
        if key in svc:
            svc.pop(key, None)
            removed.append(f"{name}.{key}")
    vols = svc.get("volumes")
    if isinstance(vols, list):
        kept = []
        for v in vols:
            if isinstance(v, str) and ":" in v:
                src = v.partition(":")[0]
                if _is_bind_source(src) and not _within_workspace(workspace_real, src):
                    removed.append(f"{name} volume {v!r}")
                    continue
            elif isinstance(v, dict) and v.get("type", "volume") == "bind":
                if not _within_workspace(workspace_real, v.get("source", "")):
                    removed.append(f"{name} volume {v.get('source')!r}")
                    continue
            kept.append(v)
        if kept:
            svc["volumes"] = kept
        else:
            svc.pop("volumes", None)
    if isinstance(svc.get("ports"), list):
        svc["ports"] = [_loopback_port(p) for p in svc["ports"]]


def _sanitize_named_volumes(merged: dict, workspace_real: str, removed: list) -> None:
    """A named volume can bind a host path via driver_opts (type: none, o: bind,
    device: /) — a service-level volume check would miss it. Strip the bind
    driver_opts from any top-level volume whose device escapes the workspace,
    leaving a plain local volume."""
    vols = merged.get("volumes")
    if not isinstance(vols, dict):
        return
    for vname, spec in vols.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("driver_opts"), dict):
            continue
        device = spec["driver_opts"].get("device")
        if _is_bind_source(device) and not _within_workspace(workspace_real, device):
            spec.pop("driver_opts", None)
            spec.pop("driver", None)
            removed.append(f"volume {vname!r} host bind {device!r}")


def repo_compose_recipe(workspace, worker_image, compose_text) -> Recipe | None:
    """Wrap a repo's own docker-compose file: parse it, anchor its relative
    paths at the cloned workspace, inject the `forge` worker so the agent can
    edit/verify/PR, and pick a web service for the URL + health check. Returns
    None when it can't be wrapped safely (unparseable, no services, or the repo
    already defines a service literally named `forge`)."""
    try:
        data = yaml.safe_load(compose_text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        return None
    if "forge" in services:
        return None        # injecting our worker would clobber theirs
    merged = copy.deepcopy(data)
    services = merged["services"]
    workspace_real = os.path.realpath(workspace)
    removed: list = []
    for name, svc in services.items():
        if isinstance(svc, dict):
            _absolutize_service(svc, workspace)
            _sanitize_service(svc, name, workspace_real, removed)
    _sanitize_named_volumes(merged, workspace_real, removed)
    web_service, web_port = _pick_web_service(services)
    if web_port is None:
        # A service with no resolvable port can't be health-checked or fronted;
        # run the stack worker-only (no URL) rather than fail a bogus health poll.
        web_service = None
    worker = _worker_service(workspace, worker_image, {
        "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}",
        "GH_TOKEN": "${GH_TOKEN}"})
    if web_service:
        nets = services[web_service].get("networks")
        if nets:   # web sits on named networks → worker must join to reach it
            worker["networks"] = (list(nets.keys()) if isinstance(nets, dict)
                                  else list(nets))
    services["forge"] = worker
    notes = ("Wrapped the repo's own compose; forge worker injected. "
             + (f"Web: {web_service}:{web_port}." if web_service
                else "No web service detected — stack runs, no public URL."))
    if removed:
        notes += (" Sandboxing stripped unsafe directives: "
                  + ", ".join(removed) + ".")
    return Recipe("repo-compose", True, merged, web_service, web_port, "/",
                  notes=notes)


def none_recipe(workspace, worker_image) -> Recipe:
    """No web app detected: a worker-only compose so Forge can still edit,
    verify, and open a PR (no URL)."""
    compose = {
        "services": {
            "forge": _worker_service(workspace, worker_image, {
                "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}",
                "GH_TOKEN": "${GH_TOKEN}",
            }),
        }
    }
    return Recipe("none", True, compose, None, None, "/", confidence="low")


# --- synthesized: any stack, from a learned/committed overlay -----------------

# The overlay keys that describe the environment itself. qa_credentials,
# lessons, provenance etc. ride along in the repo overlay but must never be
# baked into a Recipe (synth_overlay is rebuilt into composes on repair).
_SYNTH_KEYS = ("image", "apt", "setup_cmds", "dev_cmd", "web_port",
               "health_path", "env", "services")


def declares_app(overlay: dict | None) -> bool:
    """An overlay describes a runnable app when it names the command and the
    port; everything else (image, setup, services) has a workable default."""
    return bool(overlay and overlay.get("dev_cmd") and overlay.get("web_port"))


def synth_command(overlay: dict, port: int) -> str:
    """The app service's `sh -lc` command: optional apt deps, then the overlay's
    setup steps (dependency install, build, migrate), then the dev server with
    PORT exported — the same shape as web_command, minus the JS assumptions."""
    apt = overlay.get("apt") or []
    pre = (f"apt-get update && apt-get install -y --no-install-recommends "
           f"{' '.join(apt)} && " if apt else "")
    steps = " && ".join(overlay.get("setup_cmds") or [])
    return f"{pre}{steps + ' && ' if steps else ''}PORT={port} {overlay['dev_cmd']}"


def synthesized_recipe(workspace, worker_image, overlay: dict) -> Recipe:
    """Build a runnable compose from an overlay that describes the app — the
    universal fallback for repos with no recognized marker. The app runs in
    `image` (default: the worker image, which covers JS), with the repo mounted
    at /work; overlay `services` (db, cache — validated to {image, environment,
    command}) join the project network unpublished, reachable by name."""
    port = int(overlay["web_port"])
    web = {
        "image": overlay.get("image") or worker_image,
        "working_dir": "/work",
        "volumes": [f"{workspace}:/work"],
        "entrypoint": ["sh", "-lc"],
        "command": [synth_command(overlay, port)],
        "ports": [f"127.0.0.1::{port}"],
    }
    if overlay.get("env"):
        web["environment"] = dict(overlay["env"])
    if overlay.get("apt"):
        web["user"] = "root"                          # apt-get needs root
    services = {"web": web}
    for name, svc in (overlay.get("services") or {}).items():
        services[name] = copy.deepcopy(svc)
    services["forge"] = _worker_service(workspace, worker_image, {
        "CLAUDE_CODE_OAUTH_TOKEN": "${CLAUDE_CODE_OAUTH_TOKEN}",
        "GH_TOKEN": "${GH_TOKEN}"})
    return Recipe("synthesized", True, {"services": services}, "web", port,
                  overlay.get("health_path", "/"),
                  notes="Synthesized from learned/committed overlay.",
                  synth_overlay={k: copy.deepcopy(overlay[k])
                                 for k in _SYNTH_KEYS if k in overlay})


def parse_env_yml(text: str | None) -> dict | None:
    """A committed .forge/env.yml as a validated overlay, or None. Absent,
    unparseable, or schema-invalid files all resolve to None — a broken env.yml
    must degrade to marker resolution, never take provisioning down."""
    if not text:
        return None
    try:
        data = yaml.safe_load(text)
        return knowledge.validate(data) if isinstance(data, dict) else None
    except (yaml.YAMLError, ValueError):
        return None


def _worker_mount(compose: dict) -> tuple:
    """(workspace, worker_image) recovered from the injected forge service, so
    a synthesized recipe can be rebuilt from a repair delta without threading
    the originals through the repair loop."""
    svc = compose["services"]["forge"]
    ws = next(v.rpartition(":")[0] for v in svc["volumes"]
              if isinstance(v, str) and v.endswith(":/work"))
    return ws, svc["image"]


def _reapply_synth(recipe: Recipe, delta: dict) -> Recipe:
    """Rebuild a synthesized recipe with the delta merged onto the overlay it
    was built from — but carry the existing forge service over verbatim, so
    session-applied mutations (e.g. the codex auth mount) survive repair."""
    keep = {k: v for k, v in delta.items() if k in _SYNTH_KEYS}
    merged = knowledge.merge_overlay(recipe.synth_overlay or {}, keep)
    ws, img = _worker_mount(recipe.compose)
    rebuilt = synthesized_recipe(ws, img, merged)
    compose = copy.deepcopy(rebuilt.compose)
    compose["services"]["forge"] = copy.deepcopy(recipe.compose["services"]["forge"])
    return dataclasses.replace(rebuilt, compose=compose)


def dhis2_seed_url(version: str = "2.42") -> str:
    return (f"https://databases.dhis2.org/sierra-leone/{version}/"
            "dhis2-db-sierra-leone.sql.gz")


def apply_overlay(recipe: Recipe, overlay: dict | None) -> Recipe:
    """Merge a learned/committed overlay onto a resolved recipe: rebuild the web
    command for pkg_manager/apt, honor a dev_cmd escape hatch, and override
    web_port/health_path/env. Returns a new (frozen) Recipe, mutating only a deep
    copy of the compose dict. Synthesized recipes are rebuilt from their source
    overlay instead — their command is not a pkg_manager expansion."""
    if not overlay or recipe.compose is None:
        return recipe
    if recipe.name == "synthesized":
        return _reapply_synth(recipe, overlay)
    if not recipe.web_service:
        return recipe
    compose = copy.deepcopy(recipe.compose)
    web = compose["services"][recipe.web_service]
    port = overlay.get("web_port") or recipe.web_port
    apt = overlay.get("apt") or []
    if overlay.get("dev_cmd"):
        web["command"] = [overlay["dev_cmd"]]
    elif overlay.get("pkg_manager") or apt:
        pm = overlay.get("pkg_manager") or recipe.pkg_manager_used
        web["command"] = [web_command(pm, recipe.script_used, port, apt=apt)]
    if apt:
        web["user"] = "root"                      # apt-get needs root
    if overlay.get("env"):
        web.setdefault("environment", {}).update(overlay["env"])
    return dataclasses.replace(
        recipe, compose=compose, web_port=port,
        health_path=overlay.get("health_path", recipe.health_path),
        confidence="high")


def _env_get(web: dict, key: str):
    """Read an env var from a Compose service's `environment`, which may be a
    dict ({KEY: val}) OR a list (["KEY=val", ...]). Returns the value or None."""
    env = web.get("environment")
    if isinstance(env, dict):
        return env.get(key)
    if isinstance(env, list):
        prefix = f"{key}="
        for item in env:
            if isinstance(item, str) and item.startswith(prefix):
                return item[len(prefix):]
    return None


def _env_set(web: dict, key: str, value: str) -> None:
    """Set an env var on a Compose service's `environment`, preserving the
    author's form: list stays a list, dict (or absent) becomes/stays a dict."""
    env = web.get("environment")
    if isinstance(env, list):
        prefix = f"{key}="
        web["environment"] = [e for e in env
                              if not (isinstance(e, str) and e.startswith(prefix))]
        web["environment"].append(f"{key}={value}")
    else:
        d = dict(env or {})
        d[key] = value
        web["environment"] = d


def apply_resource_limits(recipe: Recipe, mem_limit: str = "8g",
                          node_max_old_space_mb: int = 4096) -> Recipe:
    """Bound the web/dev service so a leaky dev server (e.g. `next dev` under
    sustained HMR/recompile churn, or a failed-compile loop) can't grow until it
    eats the whole host. This is the JS analogue of the DHIS2 recipe's `-Xmx3g`:

      * NODE_OPTIONS=--max-old-space-size makes a runaway V8 heap OOM the process
        *cleanly* (a JS-level exit) long before RSS balloons to tens of GB;
      * mem_limit is a hard cgroup backstop for non-heap growth (native buffers,
        the Turbopack build workers);
      * restart: unless-stopped auto-recovers after either kill;
      * the command first clears a stale `.next/dev/lock`, because `next dev`
        refuses to boot when a hard-killed predecessor left its lock behind —
        which is exactly what would otherwise turn an OOM kill into a crash loop.

    No-op for worker-only recipes (no web service). Idempotent and safe to
    re-apply after apply_overlay (the self-heal repair path rebuilds the command,
    dropping the prepend — re-applying restores it)."""
    if recipe.compose is None or not recipe.web_service:
        return recipe
    compose = copy.deepcopy(recipe.compose)
    web = compose["services"][recipe.web_service]
    # setdefault, not assignment: on the wrapped repo-compose path the web
    # service is the author's own, so an explicit restart/mem_limit is a
    # deliberate choice we must not clobber. Forge-generated recipes set
    # neither, so they still get the cap.
    web.setdefault("restart", "unless-stopped")
    if mem_limit:
        web.setdefault("mem_limit", mem_limit)
    if node_max_old_space_mb:
        if "max-old-space-size" not in (_env_get(web, "NODE_OPTIONS") or ""):
            flag = f"--max-old-space-size={node_max_old_space_mb}"
            existing = _env_get(web, "NODE_OPTIONS") or ""
            _env_set(web, "NODE_OPTIONS",
                     f"{existing} {flag}".strip() if existing else flag)
    cmd = web.get("command")
    if isinstance(cmd, list) and cmd and "rm -f .next/dev/lock" not in cmd[0]:
        web["command"] = [f"rm -f .next/dev/lock 2>/dev/null; {cmd[0]}"]
    return dataclasses.replace(recipe, compose=compose)


def resolve(probe: Probe, workspace: str, worker_image: str,
            chap_target: str = "frontend", seed_dir: str | None = None,
            supabase_offset: int = 0, overlay: dict | None = None) -> Recipe:
    # A committed .forge/env.yml is the repo author's explicit recipe: when it
    # declares the app it wins outright (precedence #1); otherwise it merges
    # over the learned overlay as a patch (committed beats learned, per key).
    committed = parse_env_yml(probe.env_yml)
    if committed or overlay:
        overlay = knowledge.merge_overlay(overlay or {}, committed or {})
    if committed and declares_app(committed):
        return synthesized_recipe(workspace, worker_image, overlay)
    if probe.is_chap_core or probe.is_chap_frontend:
        target = "frontend" if probe.is_chap_frontend else "chap-core"
        recipe = dhis2_chap_recipe(workspace, worker_image, target,
                                   target_repo=target, seed_dir=seed_dir)
    elif probe.has_supabase_config and "next" in (probe.package_json or ""):
        recipe = next_supabase_recipe(workspace, worker_image, offset=supabase_offset,
                                      pkg_manager=probe.pkg_manager,
                                      package_json=probe.package_json)
    elif probe.package_json:
        # A package.json means "this is a JS app, try to run it". With a known
        # dev/start script it's high-confidence; without one it's low-confidence
        # (node_web_recipe marks it) so the self-heal probe can learn a dev_cmd.
        recipe = node_web_recipe(workspace, worker_image, probe.package_json,
                                 pkg_manager=probe.pkg_manager)
    elif probe.repo_compose_text:
        # The repo ships its own compose and is not a recognized JS app: wrap it
        # (kept BELOW node-web so a JS repo with an *incidental* compose — e.g.
        # just a db — still runs via its dev script rather than being hijacked).
        recipe = (repo_compose_recipe(workspace, worker_image,
                                      probe.repo_compose_text)
                  or _no_marker(workspace, worker_image, overlay))
    else:
        # No marker matched: if the (learned) overlay describes the app — the
        # probe agent read the repo and worked out how to run it — synthesize;
        # else worker-only. This is the "spin up anything" path.
        recipe = _no_marker(workspace, worker_image, overlay)
    if recipe.name == "synthesized":
        return recipe                     # overlay already baked in
    return apply_overlay(recipe, overlay)


def _no_marker(workspace, worker_image, overlay) -> Recipe:
    if declares_app(overlay):
        return synthesized_recipe(workspace, worker_image, overlay)
    return none_recipe(workspace, worker_image)
