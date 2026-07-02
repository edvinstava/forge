"""Make a Next.js dev server reachable through forge's Caddy proxy + cloudflared
tunnel.

A forge-served app is hit under a host the app never expects (the per-run
`*.forge.localhost` proxy host and the `*.trycloudflare.com` tunnel), which trips
TWO independent Next.js cross-origin guards. They allowlist differently, so we
inject both:

1. Dev-asset CORS (`allowedDevOrigins`). Next 15.2+/16 block cross-origin
   requests to dev-only assets (HMR websocket, `/_next/*`) unless the origin is
   allowlisted (default: localhost). Without it the browser never live-reloads
   and a transiently broken route stays broken until a manual refresh. Next
   matches this against the origin *hostname* (port stripped), so a bare wildcard
   like `*.forge.localhost` covers the local proxy host on any port.

2. Server Action CSRF (`serverActions.allowedOrigins`). Next only allows POST
   Server Actions whose `Origin` matches the `Host`, else it aborts with
   "Invalid Server Actions request." — which makes every form-driven action
   (e.g. login) fail through the proxy/tunnel. This match uses
   `new URL(origin).host`, which KEEPS the port, so `*.forge.localhost` matches
   the tunnel but NOT the local URL `run-<id>.forge.localhost:8088`; we must
   allowlist a port-bearing pattern too. In Next 16 this lives under
   `experimental.serverActions.allowedOrigins`.

Rather than parse arbitrary JS we append statements that mutate the already
exported config object (objects are by-reference, so a post-export mutation is
visible to Next when it loads the module)."""
import json
import re

# Dev-asset origins. Matched against the hostname (port stripped), so bare
# wildcards work for the local URL on any port. `web` is the compose service name
# (some same-network requests carry it as the Host).
DEV_ORIGINS = ["*.forge.localhost", "*.trycloudflare.com", "web"]

# Next config lookup order (mirrors Next's own resolution).
_CONFIG_NAMES = ["next.config.js", "next.config.mjs", "next.config.cjs",
                 "next.config.ts", "next.config.mts"]

# Bumped when the injected block's shape changes so already-patched configs from
# an older forge (which lack the Server Action fix) get re-patched on next wake.
_MARKER = "forge: allow HMR/dev + Server Action requests via the Caddy proxy + tunnel"


def server_action_origins(domain="forge.localhost", port=8088):
    """Origins for `serverActions.allowedOrigins`. Next matches these against the
    origin *host* (WITH port), so the local proxy URL needs a port-bearing
    wildcard; the public tunnel host carries no port (default 443)."""
    return [f"*.{domain}", f"*.{domain}:{port}", "*.trycloudflare.com"]


def _origins_literal(origins) -> str:
    return "[" + ", ".join(json.dumps(o) for o in origins) + "]"


def _merge_array_stmt(target: str, origins) -> str:
    # Union with any existing array so a config that sets its own list isn't
    # clobbered.
    return (f"{target} = Array.from(new Set(["
            f"...({target} || []), ...{_origins_literal(origins)}]));\n")


def _ensure_obj_stmt(path: str) -> str:
    # Guarantee `path` is a plain object before assigning nested keys, replacing a
    # non-object such as a legacy `serverActions: true`.
    return f"{path} = ({path} && typeof {path} === 'object') ? {path} : {{}};\n"


def _patch_block(target: str, dev_origins, sa_origins) -> str:
    sa = f"{target}.experimental.serverActions"
    return (
        f"\n\n// {_MARKER}\n"
        + _merge_array_stmt(f"{target}.allowedDevOrigins", dev_origins)
        + _ensure_obj_stmt(f"{target}.experimental")
        + _ensure_obj_stmt(sa)
        + _merge_array_stmt(f"{sa}.allowedOrigins", sa_origins)
    )


def inject(text: str, dev_origins=DEV_ORIGINS, sa_origins=None):
    """Return `text` with forge's dev/Server-Action origins merged in, or None
    when no change is needed (already patched) or the export shape isn't one we
    can safely patch."""
    if sa_origins is None:
        sa_origins = server_action_origins()
    if _MARKER in text:
        return None
    body = text.rstrip("\n")
    if re.search(r"module\.exports\b", text):           # CommonJS
        target = "module.exports"
    else:
        # Only a bare `export default <ident>` (optionally semicolon-terminated)
        # is safe to mutate; reject calls/objects like `export default fn(...)`.
        m = re.search(r"export\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$",
                      text, re.MULTILINE)
        if not m:                                       # inline/wrapped default — skip
            return None
        target = m.group(1)
    return body + _patch_block(target, dev_origins, sa_origins)


def _fresh_config(dev_origins=DEV_ORIGINS, sa_origins=None) -> str:
    if sa_origins is None:
        sa_origins = server_action_origins()
    return ("/** @type {import('next').NextConfig} */\n"
            f"// {_MARKER}\n"
            "module.exports = {\n"
            f"  allowedDevOrigins: {_origins_literal(dev_origins)},\n"
            "  experimental: {\n"
            f"    serverActions: {{ allowedOrigins: {_origins_literal(sa_origins)} }},\n"
            "  },\n"
            "};\n")


def _is_next_app(pkg_json: str | None) -> bool:
    if not pkg_json:
        return False
    try:
        pkg = json.loads(pkg_json)
    except (ValueError, TypeError):
        return False
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return "next" in deps


def unpatch(text: str) -> "str | None":
    """Remove forge's appended origin block: everything from the marker line to
    EOF. The block is always appended last, so this is robust even after a
    repo formatter rewraps the injected statements (they stay below the
    marker; only code an agent appends *after* the block would be lost, which
    inject's append-at-EOF placement makes vanishingly rare). Returns None
    when the text carries no marker."""
    idx = text.find(_MARKER)
    if idx == -1:
        return None
    line_start = text.rfind("\n", 0, idx)
    head = (text[:line_start + 1] if line_start != -1 else "").rstrip("\n")
    return head + "\n" if head else ""


def unpatch_for_commit(host, ws: str) -> list:
    """Prepare tracked Next configs for a commit: clear skip-worktree and strip
    forge's origin block, so an agent's REAL config edits ship in the PR while
    forge's runtime-only block never does. Returns the stripped filenames; the
    caller re-applies the patch (ensure_dev_origins) after committing. Fresh
    forge-created configs (untracked, .git/info/exclude'd) are left alone —
    stripping would gut them and git ignores them anyway."""
    run = getattr(host, "run", None)
    if run is None:
        return []
    stripped_names = []
    for name in _CONFIG_NAMES:
        if not host.exists(ws, name):
            continue
        stripped = unpatch(host.read(ws, name) or "")
        if stripped is None:
            continue
        tracked = run(["git", "-C", ws, "ls-files", "--error-unmatch", name])
        if getattr(tracked, "exit_code", 1) != 0:
            continue
        run(["git", "-C", ws, "update-index", "--no-skip-worktree", name])
        host.write_file(f"{ws}/{name}", stripped)
        stripped_names.append(name)
    return stripped_names


def _hide_from_git(host, ws: str, name: str, tracked: bool) -> None:
    """Keep forge's runtime-only origin patch out of the worker's diff and the
    PR: skip-worktree a tracked config (mirrors the Supabase config.toml
    treatment), .git/info/exclude an untracked one forge created. Best-effort —
    a host without run() (some test fakes) just skips."""
    run = getattr(host, "run", None)
    if tracked:
        if run is not None:
            run(["git", "-C", ws, "update-index", "--skip-worktree", name])
        return
    exclude_rel = ".git/info/exclude"
    existing = host.read(ws, exclude_rel) or ""
    line = f"/{name}"
    if line in existing.splitlines():
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    host.write_file(f"{ws}/{exclude_rel}", existing + line + "\n")


def ensure_dev_origins(host, ws: str, dev_origins=DEV_ORIGINS, *,
                       proxy_domain="forge.localhost", proxy_port=8088) -> bool:
    """Patch (or create) the workspace's Next config so dev assets AND Server
    Actions through the proxy/tunnel are allowed, and hide the patch from git
    (it is forge infrastructure, not the user's change — it must never land in
    a PR). Returns True if a file was written. Best-effort and idempotent: an
    already-patched app (forge marker present), a non-Next app, or an
    unpatchable config shape is left untouched."""
    sa_origins = server_action_origins(proxy_domain, proxy_port)
    for name in _CONFIG_NAMES:
        if host.exists(ws, name):
            text = host.read(ws, name) or ""
            patched = inject(text, dev_origins, sa_origins)
            if patched is None:
                # Already patched (e.g. a wake): re-assert the hide, so a
                # workspace patched by an older forge stops leaking into PRs.
                if _MARKER in text:
                    _hide_from_git(host, ws, name, tracked=True)
                return False
            host.write_file(f"{ws}/{name}", patched)
            _hide_from_git(host, ws, name, tracked=True)
            return True
    # No config file, but a Next app still honours one — create a minimal config.
    if host.exists(ws, "package.json") and _is_next_app(host.read(ws, "package.json")):
        host.write_file(f"{ws}/next.config.js",
                        _fresh_config(dev_origins, sa_origins))
        _hide_from_git(host, ws, "next.config.js", tracked=False)
        return True
    return False
