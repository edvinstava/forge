import argparse
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from forge import lifecycle, proxy
from forge.compose_orchestrator import ComposeOrchestrator
from forge.config import Config
from forge.hostops import LocalHost
from forge.runspec import make_runspec
from forge.session import SessionManager
from forge.store import Store


def resolve_identity(env_name, env_email, git_name, git_email, gh_name, gh_email):
    """First non-empty wins, independently for name and email."""
    name = next((x for x in (env_name, git_name, gh_name) if x), "")
    email = next((x for x in (env_email, git_email, gh_email) if x), "")
    return name, email


def _require_credentials(cfg: Config) -> str | None:
    """Error message if the active provider's agent credential or GH_TOKEN is
    missing; None when both are present. Provider-aware so a codex-only user
    (ChatGPT-plan login, no Claude token) is not wrongly blocked."""
    from forge import providers
    provider = providers.from_config(cfg)
    if not provider.credentials_ready(cfg):
        return (f"error: the {provider.name} provider needs "
                f"{provider.credential_hint}")
    if not cfg.gh_token:
        return "error: GH_TOKEN must be set (run `gh auth token`)"
    return None


def _capture(argv) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _populate_identity(cfg: Config) -> None:
    cfg.git_author_name, cfg.git_author_email = resolve_identity(
        cfg.git_author_name, cfg.git_author_email,
        _capture(["git", "config", "user.name"]),
        _capture(["git", "config", "user.email"]),
        _capture(["gh", "api", "user", "--jq", ".name // empty"]),
        _capture(["gh", "api", "user", "--jq", ".email // empty"]),
    )


def _cmd_review(args) -> int:
    from forge.session import SessionManager
    cfg = Config.from_env(Path(args.runs_dir))
    if err := _require_credentials(cfg):
        print(err, file=sys.stderr)
        return 1
    _populate_identity(cfg)
    store = Store(cfg.runs_dir / "forge.db")
    mgr = SessionManager(cfg, store, LocalHost())
    run_id = uuid.uuid4().hex
    result = None
    for ev in mgr.review(run_id, args.pr, args.model):
        if ev.kind == "phase":
            print(f"… {ev.data.get('label', '')}")
        elif ev.kind == "error":
            print(f"error: {ev.data.get('kind')}: {ev.data.get('detail', '')}",
                  file=sys.stderr)
        elif ev.kind == "review":
            result = ev.data
    if not result or not result.get("ok"):
        print(f"review failed: {(result or {}).get('reason', 'unknown')}",
              file=sys.stderr)
        return 1
    tag = " (under your account — set up the Forge GitHub App for forge[bot])" \
        if result.get("degraded") else ""
    print(f"review posted{tag}: {result['review_url']}  "
          f"({result['comments']} inline, {result['dropped']} folded)")
    return 0


def _cmd_attach(args) -> int:
    cfg = Config.from_env(Path(args.runs_dir))
    store = Store(cfg.runs_dir / "forge.db")
    cp = store.open_checkpoint(args.run_id)
    if not cp:
        print(store.get_run(args.run_id).get("lifecycle_state") or "no open checkpoint")
        return 0
    mgr = SessionManager(cfg, store, LocalHost())
    print(__import__("json").dumps(cp["payload"], indent=2))
    ans = input("approve? [y]es / type changes / [n]o: ").strip().lower()
    action, body = ("approve", None) if ans in ("", "y", "yes") else \
                   (("reject", None) if ans in ("n", "no") else ("edit", ans))
    for ev in mgr.respond_checkpoint(args.run_id, cp["id"], action, body):
        if ev.kind in ("phase", "done", "error"):
            print(f"… {ev.kind}: {ev.data.get('label') or ev.data.get('message') or ev.data.get('detail','')}")
    return 0


def _cmd_run(args) -> int:
    cfg = Config.from_env(Path(args.runs_dir))
    if err := _require_credentials(cfg):
        print(err, file=sys.stderr)
        return 1
    try:
        make_runspec(args.repo, args.task, "x" * 8)   # validate early
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    _populate_identity(cfg)
    run_id = uuid.uuid4().hex
    store = Store(cfg.runs_dir / "forge.db")
    orch = ComposeOrchestrator(cfg, store, LocalHost())

    from forge import flow
    auto = args.yes or not args.plan
    policy = flow.CheckpointPolicy.for_cli(auto=auto)

    def _approve(plan):
        print("\nPLAN (Phase 1 — confirm repo/task; the full agent-generated plan lands in a later release):\n"
              + __import__("json").dumps(plan, indent=2))
        ans = input("approve? [y]es / type changes / [n]o: ").strip().lower()
        return ans in ("", "y", "yes")

    try:
        out = orch.run(args.repo, args.task, run_id, policy=policy,
                       approve=None if auto else _approve)
    except Exception as e:  # noqa: BLE001 - CLI boundary: surface cleanly
        print(f"error: run failed: {e}", file=sys.stderr)
        return 1

    if out.pr_url:
        print(f"{out.state}: {'draft ' if out.draft else ''}PR {out.pr_url}")
    else:
        print(f"{out.state}: no PR ({out.reason})")
    if out.web_url:
        print(f"app: {out.web_url}")
        if args.open and sys.platform == "darwin":
            subprocess.run(["open", out.web_url], capture_output=True)
    return 0 if out.state in ("done", "stopped_budget") else 1


def _render_proxy(cfg: Config, store: Store) -> str:
    live = store.list_envs(states=("live",))
    proxy.connect_networks([e["run_id"] for e in live])
    text = proxy.caddy_config(proxy.routes_for(live, domain=cfg.proxy_domain),
                              cfg.proxy_port)
    (cfg.runs_dir / "Caddyfile").write_text(text)
    return text


def _cmd_serve(args) -> int:
    cfg = Config.from_env(Path(args.runs_dir))
    store = Store(cfg.runs_dir / "forge.db")
    caddyfile = cfg.runs_dir / "Caddyfile"
    caddyfile.write_text(proxy.caddy_config(
        proxy.routes_for(store.list_envs(states=("live",)), domain=cfg.proxy_domain),
        cfg.proxy_port))
    proxy.ensure_proxy(str(caddyfile), cfg.proxy_port)
    print(f"forge proxy live → http://run-<id>.{cfg.proxy_domain}:{cfg.proxy_port}  "
          f"(idle TTL {cfg.env_ttl_secs}s, poll {args.interval}s)")
    last = None
    try:
        while True:
            reaped = lifecycle.reap_idle(store, datetime.utcnow(), cfg.env_ttl_secs)
            if reaped:
                print(f"reaped idle env(s): {', '.join(reaped)}")
            text = _render_proxy(cfg, store)
            if text != last:
                proxy.reload_proxy()
                last = text
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nforge serve stopped (envs keep running; use `forge down`)")
        return 0


def _cmd_web(args) -> int:
    cfg = Config.from_env(Path(args.runs_dir))
    if err := _require_credentials(cfg):
        print(err, file=sys.stderr)
        return 1
    _populate_identity(cfg)
    if args.slack:
        if args.reload:
            print("error: --slack cannot be combined with --reload", file=sys.stderr)
            return 1
        missing = [n for n, v in (("SLACK_BOT_TOKEN", cfg.slack_bot_token),
                                  ("SLACK_APP_TOKEN", cfg.slack_app_token),
                                  ("SLACK_ALLOWED_USER", cfg.slack_allowed_user))
                   if not v]
        if missing:
            from forge.config import config_file_path
            print(f"error: --slack needs {', '.join(missing)} set "
                  f"(set in the environment or in {config_file_path()})",
                  file=sys.stderr)
            return 1
    if args.github:
        if args.reload:
            print("error: --github cannot be combined with --reload",
                  file=sys.stderr)
            return 1
        from forge import ghapp as ghappmod
        if not ghappmod.is_configured(cfg):
            print("error: --github needs FORGE_GH_APP_ID and FORGE_GH_APP_KEY "
                  "(and the key file present)", file=sys.stderr)
            return 1
        from urllib.parse import urlparse
        # A schemeless URL parses with hostname=None, which would attach the
        # public gate with an empty host — i.e. silently fail OPEN and expose
        # the whole unauthenticated API through the user's ingress.
        if cfg.public_url and not urlparse(cfg.public_url).hostname:
            print("error: FORGE_PUBLIC_URL must be a full URL including the "
                  f"scheme (e.g. https://forge.example.com), got {cfg.public_url!r}",
                  file=sys.stderr)
            return 1
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError:
        print("error: `forge web` needs the web extra — "
              "install with `pip install -e \".[web]\"` "
              "(add `slack` / `gh-app` for those features)", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}"
    # Slack deep-links sessions to the web app; default to our own address so
    # links work out of the box (FORGE_WEB_URL overrides for LAN/tunnel setups).
    cfg.forge_web_url = cfg.forge_web_url or url
    print(f"forge web → {url}" + ("  (reload)" if args.reload else ""))
    if args.open and sys.platform == "darwin":
        subprocess.run(["open", url], capture_output=True)
    if args.reload:
        # Hand uvicorn an import string + factory so its reloader subprocess can
        # rebuild the app on each .py change; the factory reads runs_dir from env.
        import os
        os.environ["FORGE_RUNS_DIR"] = str(cfg.runs_dir)
        # timeout_graceful_shutdown: open SSE streams otherwise stall the
        # reload worker's shutdown forever (see webapp.make_server).
        uvicorn.run("forge.webapp:make_app", factory=True, host=args.host,
                    port=args.port, log_level="info", reload=True,
                    reload_dirs=[str(Path(__file__).resolve().parent.parent)],
                    timeout_graceful_shutdown=5)
        return 0
    from forge.session import SessionManager
    from forge.webapp import (create_app, attach_background,
                              attach_tunnel_lifecycle, make_server,
                              refresh_proxy)
    store = Store(cfg.runs_dir / "forge.db")
    tm = None
    proxy_refresh = None
    if args.slack:
        from forge import tunnel as tunnelmod
        tm = tunnelmod.TunnelManager(probe=tunnelmod.http_probe)
        proxy_refresh = lambda: refresh_proxy(store, cfg)
    manager = SessionManager(cfg, store, LocalHost(), tunnel=tm, proxy_refresh=proxy_refresh)
    app = create_app(cfg, store, manager)
    attach_background(app, cfg, store, manager)
    if args.slack:
        import threading
        from forge import slackbot
        from forge.reporesolve import build_resolver
        resolver = build_resolver(cfg)
        bot, handler = slackbot.build_app(manager, store, cfg, resolver, tm)
        # Register tunnel/notice lifecycle (and the bot's transition callback)
        # in the main thread so the FastAPI startup hook is wired before uvicorn
        # starts; serve Socket Mode events on a daemon thread.
        attach_tunnel_lifecycle(app, cfg, store, manager, tm, bot.on_lifecycle)
        threading.Thread(target=handler.start, daemon=True).start()
        print("forge slack bot → Socket Mode connected (DM the app)")
    if args.github:
        from urllib.parse import urlparse
        from forge import ghapp as ghappmod, ghwebhook
        from forge.webapp import attach_github_webhook, attach_public_gate
        secret = cfg.gh_webhook_secret or ghwebhook.load_or_create_secret(
            Path.home() / ".forge" / "webhook.secret")
        public = cfg.public_url
        if not public:
            from forge import tunnel as tunnelmod
            # Dedicated instance: the per-run TunnelManager is swept by
            # tunnel_sweep, which reaps any tunnel whose id isn't a live env —
            # a shared instance would kill this tunnel within 30s.
            wh_tunnel = tunnelmod.TunnelManager(probe=tunnelmod.http_probe)
            public = wh_tunnel.start("github-webhook",
                                     f"http://127.0.0.1:{args.port}")
        if not public:
            print("error: --github needs a public URL; cloudflared quick "
                  "tunnel failed (install cloudflared or set FORGE_PUBLIC_URL)",
                  file=sys.stderr)
            return 1
        public = public.rstrip("/")
        hook_url = f"{public}/api/github/webhook"
        gh = ghappmod.GhApp(cfg)
        try:
            gh.update_webhook_config(hook_url, secret)
            print(f"forge github webhook → {hook_url}  (App config updated)")
        except Exception as e:  # noqa: BLE001 - startup boundary: degrade, don't die
            secret_hint = ("$FORGE_GH_WEBHOOK_SECRET" if cfg.gh_webhook_secret
                           else "~/.forge/webhook.secret")
            print(f"warning: could not update the App webhook config ({e}); "
                  f"set it manually in the App settings — URL {hook_url}, "
                  f"secret in {secret_hint}", file=sys.stderr)
        attach_public_gate(app, urlparse(public).hostname or "")
        attach_github_webhook(app, cfg, manager, gh, secret)
    make_server(app, manager.bus, args.host, args.port).run()
    return 0


def _cmd_bake(args) -> int:
    from forge.recipe import dhis2_seed_url
    if args.template != "dhis2-chap":
        print(f"error: unknown bake template {args.template!r} "
              "(supported: dhis2-chap)", file=sys.stderr)
        return 1
    import urllib.request
    dest = Path(args.runs_dir) / "cache" / "dhis2-seed"
    dest.mkdir(parents=True, exist_ok=True)
    url = dhis2_seed_url(args.version)
    print(f"downloading {url} …")
    data = urllib.request.urlopen(url, timeout=300).read()
    is_gz = data[:2] == b"\x1f\x8b"   # served uncompressed despite .gz name
    name = "dhis2.sql.gz" if is_gz else "dhis2.sql"
    (dest / name).write_bytes(data)
    print(f"baked {dest / name} ({len(data) // 1_000_000} MB)")
    return 0


def _cmd_status(args) -> int:
    store = Store(Path(args.runs_dir) / "forge.db")
    envs = store.list_envs()
    if not envs:
        print("no environments")
        return 0
    for e in envs:
        print(f"{e['run_id']}  {e['state']:<8}  {e.get('web_url') or '-'}")
    return 0


def _cmd_down(args) -> int:
    store = Store(Path(args.runs_dir) / "forge.db")
    lifecycle.reap_project(store, args.run_id)   # docker compose -p forge-<id> down -v
    print(f"reaped {args.run_id}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="forge")
    sub = p.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run")
    runp.add_argument("repo")
    runp.add_argument("task")
    runp.add_argument("--runs-dir", default="runs")
    runp.add_argument("--open", action=argparse.BooleanOptionalAction, default=True,
                      help="open the app URL in a browser when live (macOS)")
    runp.add_argument("--yes", action="store_true",
                      help="auto-approve the plan (fire-and-forget)")
    runp.add_argument("--plan", action=argparse.BooleanOptionalAction, default=True,
                      help="propose a plan and gate on approval (default on; --yes overrides)")
    runp.set_defaults(func=_cmd_run)

    reviewp = sub.add_parser("review")
    reviewp.add_argument("pr", help="PR reference: owner/repo#N or a PR URL")
    reviewp.add_argument("--model", default="auto")
    reviewp.add_argument("--runs-dir", default="runs")
    reviewp.set_defaults(func=_cmd_review)

    statusp = sub.add_parser("status")
    statusp.add_argument("--runs-dir", default="runs")
    statusp.set_defaults(func=_cmd_status)

    servep = sub.add_parser("serve")
    servep.add_argument("--runs-dir", default="runs")
    servep.add_argument("--interval", type=int, default=30)
    servep.add_argument("--once", action="store_true", help="run one cycle and exit")
    servep.set_defaults(func=_cmd_serve)

    webp = sub.add_parser("web")
    webp.add_argument("--runs-dir", default="runs")
    webp.add_argument("--host", default="127.0.0.1")
    webp.add_argument("--port", type=int, default=8099)
    webp.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)
    webp.add_argument("--reload", action="store_true",
                      help="auto-restart the backend on src/ code changes (dev)")
    webp.add_argument("--slack", action="store_true",
                      help="also connect the Slack Socket Mode bot (needs SLACK_* env)")
    webp.add_argument("--github", action="store_true",
                      help="accept @<app-slug> review PR comment-commands via a "
                           "GitHub App webhook (needs FORGE_GH_APP_ID/KEY)")
    webp.set_defaults(func=_cmd_web)

    bakep = sub.add_parser("bake")
    bakep.add_argument("template")
    bakep.add_argument("--version", default="2.42")
    bakep.add_argument("--runs-dir", default="runs")
    bakep.set_defaults(func=_cmd_bake)

    downp = sub.add_parser("down")
    downp.add_argument("run_id")
    downp.add_argument("--runs-dir", default="runs")
    downp.set_defaults(func=_cmd_down)

    attachp = sub.add_parser("attach")
    attachp.add_argument("run_id")
    attachp.add_argument("--runs-dir", default="runs")
    attachp.set_defaults(func=_cmd_attach)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
