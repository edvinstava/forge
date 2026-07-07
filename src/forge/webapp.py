import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from forge import inbox, repos

logger = logging.getLogger("forge.webapp")


def _on_startup(app, fn) -> None:
    """Run `fn` at server startup without FastAPI's deprecated on_event: keep a
    hook list on app.state and install a single router lifespan that drains it.
    Safe to call repeatedly (attach_background + attach_tunnel_lifecycle both
    register here, in any order)."""
    hooks = getattr(app.state, "forge_startup_hooks", None)
    if hooks is None:
        hooks = []
        app.state.forge_startup_hooks = hooks

        @asynccontextmanager
        async def _lifespan(_app):
            for h in hooks:
                h()
            yield

        app.router.lifespan_context = _lifespan
    hooks.append(fn)


def _sse(event_iter):
    def gen():
        for ev in event_iter:
            yield f"event: {ev.kind}\ndata: {json.dumps(ev.data)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def bus_events(bus, run_id, since=0, tail=True, heartbeat_secs=15.0):
    """SSE frames for a run's bus feed: replay buffered events with seq > since,
    then tail live ones. `since=-1` skips history (attach to *now*); the web
    client instead catches up on session select via `since=0&tail=0` (fetch
    the buffered backlog and return) so a refresh mid-turn can replay the
    in-flight turn, then tails from the last seen seq — a reconnect passes
    that seq so the gap replays. Heartbeat comments keep idle streams alive
    through proxies. Frames carry {...data, seq, origin} so the client can
    dedup and badge the driver."""
    def frame(e):
        return (f"event: {e['kind']}\n"
                f"data: {json.dumps({**e['data'], 'seq': e['seq'], 'origin': e['origin']})}\n\n")

    sub = bus.subscribe(run_id)      # subscribe FIRST so replay→tail loses nothing
    try:
        last = bus.last_seq(run_id) if since < 0 else since
        for e in bus.replay(run_id, since=last):
            last = e["seq"]
            yield frame(e)
        while tail and not sub.closed:
            e = sub.get(timeout=heartbeat_secs)
            if e is None:
                yield ": ping\n\n"
                continue
            if e["seq"] <= last:     # already sent during replay
                continue
            last = e["seq"]
            yield frame(e)
    finally:
        sub.close()


def create_app(config, store, manager) -> FastAPI:
    app = FastAPI(title="forge")

    @app.get("/api/repos")
    def get_repos(q: str = ""):
        return repos.list_repos(config.workspace_dir, q)

    @app.get("/api/config")
    def get_config():
        # The frontend derives each run's DNS-free local preview URL
        # (http://run-<id>.<domain>:<port>) from the proxy settings; the model
        # picker adapts to whichever agent provider the daemon runs.
        return {"proxy_domain": config.proxy_domain,
                "proxy_port": config.proxy_port,
                "provider": manager.provider.name,
                "model_choices": manager.provider.model_choices}

    @app.get("/api/sessions")
    def list_sessions():
        return store.list_sessions()

    @app.get("/api/sessions/{run_id}")
    def session_detail(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "no such session")
        env = store.get_env(run_id)
        cp = store.open_checkpoint(run_id)
        plan = json.loads(run["plan_json"]) if run.get("plan_json") else None
        return {
            "run_id": run_id,
            "repo": run.get("repo"),
            "title": run.get("title"),
            "branch": run.get("branch"),
            "state": run.get("state"),
            "pr_url": run.get("pr_url"),
            "repo_source": run.get("repo_source"),
            "web_url": env.get("web_url"),
            "env_state": env.get("state"),
            "messages": store.list_messages(run_id),
            "checkpoint": cp,
            "plan": plan,
        }

    @app.get("/api/sessions/{run_id}/events")
    def session_events(run_id: str, since: int = -1, tail: int = 1):
        """Live attach: follow this run's TurnEvents regardless of which surface
        drives the turn (web POST, Slack thread, batch queue)."""
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        return StreamingResponse(
            bus_events(manager.bus, run_id, since=since, tail=bool(tail)),
            media_type="text/event-stream")

    @app.get("/api/sessions/{run_id}/diff")
    def session_diff(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        return {"diff": manager.diff(run_id)}

    @app.get("/api/sessions/{run_id}/verify")
    def session_verify(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        msgs = [m for m in store.list_messages(run_id) if m["role"] == "assistant"]
        meta = (msgs[-1].get("meta") or {}) if msgs else {}
        return {
            "verify_ok": meta.get("verify_ok"),
            "diff_files": meta.get("diff_files"),
            "verify_failed": meta.get("verify_failed") or [],
            "verify_output": meta.get("verify_output") or "",
            "model": meta.get("model"),
        }

    # Live agent-browser view (workspace #live=<id>): the QA screencaster drops
    # JPEG frames into the run's workspace (forge/browserview.py); these two
    # routes let the UI poll them. Frames deliberately bypass the SSE bus —
    # base64 frames would bloat the replay buffer and the Slack tap.
    @app.get("/api/sessions/{run_id}/browser")
    def browser_status(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        from forge import browserview
        return browserview.status(config.runs_dir, run_id)

    @app.get("/api/sessions/{run_id}/browser/frame")
    def browser_frame(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        from forge import browserview
        p = browserview.frame_path(config.runs_dir, run_id)
        if not p.is_file():
            raise HTTPException(404, "no live frame")
        return FileResponse(p, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{run_id}/browser/stream")
    async def browser_stream(run_id: str):
        """MJPEG push: one long-lived <img> request instead of poll-and-swap —
        frames reach the workspace a file-poll tick after the screencaster
        writes them. /frame stays as the poll fallback."""
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        from forge import browserview
        boundary = browserview.STREAM_BOUNDARY.decode()
        return StreamingResponse(
            browserview.stream_frames(config.runs_dir, run_id),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
            headers={"Cache-Control": "no-store"})

    # Live files view (workspace #live=<id>): the workspace tree + per-file
    # content/diff, read host-side from the bind-mounted workspace
    # (forge/workfiles.py) so it works even while the agent holds the
    # container busy mid-turn. Same loopback-only exposure as /diff.
    @app.get("/api/sessions/{run_id}/files")
    def session_files(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        from forge import workfiles
        return workfiles.list_files(config.runs_dir, run_id)

    @app.get("/api/sessions/{run_id}/file")
    def session_file(run_id: str, path: str = ""):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        from forge import workfiles
        detail = workfiles.file_detail(config.runs_dir, run_id, path)
        if detail is None:
            raise HTTPException(404, "no such file")
        return detail

    @app.post("/api/sessions")
    async def start_session(req: Request):
        body = await req.json()
        ok, msg = manager.can_start()
        if not ok:
            return JSONResponse({"error": msg}, status_code=409)
        run_id = uuid.uuid4().hex
        def events():
            yield SimpleNamespace(kind="session", data={"run_id": run_id})
            yield from manager.start(run_id, body["repo"], body.get("source", "github"),
                                     origin="web")
        return _sse(events())

    @app.post("/api/sessions/{run_id}/messages")
    async def post_message(run_id: str, req: Request):
        body = await req.json()
        return _sse(manager.turn(run_id, body["prompt"], body.get("model", "auto"),
                                 attachments=body.get("attachments"), origin="web"))

    # SSE contract: these routes emit `plan` and `checkpoint` event kinds via
    # _sse — the frontend must handle both kinds (part of the SSE event contract).
    @app.post("/api/sessions/{run_id}/task")
    async def start_task(run_id: str, req: Request):
        body = await req.json()
        return _sse(manager.plan_task(run_id, body["task"], body.get("model", "auto"),
                                      attachments=body.get("attachments"), origin="web"))

    @app.post("/api/sessions/{run_id}/attachments")
    async def upload_attachment(run_id: str, req: Request, name: str = "image.png"):
        """Raw-body image upload (Content-Type: image/*) — deliberately not
        multipart so we don't grow a python-multipart dependency. The stored name
        comes back to the client, which references it in the next task/message."""
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        ctype = (req.headers.get("content-type") or "").split(";")[0].strip()
        if not ctype.startswith("image/"):
            raise HTTPException(415, "only image attachments are supported")
        data = await req.body()
        if len(data) > inbox.MAX_BYTES:
            raise HTTPException(413, f"max {inbox.MAX_BYTES // (1024 * 1024)} MB")
        try:
            stored = manager.save_attachment(run_id, name, data, mimetype=ctype)
        except ValueError as e:
            raise HTTPException(415, str(e))
        return {"name": stored}

    @app.post("/api/sessions/{run_id}/checkpoints/{cid}")
    async def respond_checkpoint(run_id: str, cid: int, req: Request):
        body = await req.json()
        return _sse(manager.respond_checkpoint(
            run_id, cid, body["action"], body.get("body"), body.get("model", "auto"),
            origin="web"))

    @app.post("/api/review")
    async def start_review(req: Request):
        body = await req.json()
        ok, msg = manager.can_start()
        if not ok:
            return JSONResponse({"error": msg}, status_code=409)
        run_id = uuid.uuid4().hex
        def events():
            yield SimpleNamespace(kind="session", data={"run_id": run_id})
            yield from manager.review(run_id, body["pr"], body.get("model", "auto"),
                                      origin="web")
        return _sse(events())

    @app.post("/api/batch")
    async def start_batch(req: Request):
        # The non-blocking path: enqueue N tasks (no can_start / no 409).
        # Over-capacity items wait in 'queued' and the scheduler drains them.
        body = await req.json()
        items = body.get("items") or []
        if not items:
            return JSONResponse({"error": "no items"}, status_code=400)
        if len(items) > config.batch_max_items:
            return JSONResponse(
                {"error": f"batch too large (max {config.batch_max_items})"},
                status_code=400)
        batch_id, run_ids = manager.enqueue_batch(items)
        return {"batch_id": batch_id, "run_ids": run_ids}

    @app.delete("/api/batch/{batch_id}")
    def cancel_batch(batch_id: str):
        return {"canceled": store.cancel_batch(batch_id)}

    @app.post("/api/sessions/{run_id}/pr")
    def open_pr(run_id: str):
        res = manager.open_pr(run_id)
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res

    @app.post("/api/sessions/{run_id}/stop")
    def stop(run_id: str):
        manager.stop(run_id)
        return {"stopped": True}

    @app.post("/api/sessions/{run_id}/sleep")
    def sleep(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        return {"asleep": manager.sleep(run_id, reason="web")}

    @app.post("/api/sessions/{run_id}/wake")
    def wake(run_id: str):
        if not store.get_run(run_id):
            raise HTTPException(404, "no such session")
        return _sse(manager.wake(run_id, origin="web"))

    @app.delete("/api/sessions/{run_id}")
    def end(run_id: str):
        # A queued run holds no env — just mark it canceled. Anything else goes
        # through the normal teardown. (get_run returns {} for an unknown id,
        # so the manager owns the not-found case, same as pr/stop.)
        if store.get_run(run_id).get("state") == "queued":
            return {"canceled": store.cancel_queued(run_id)}
        manager.end(run_id, reason="web")
        return {"ended": True}

    dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="spa")
    return app


def make_app() -> FastAPI:
    """App factory for `uvicorn --factory --reload` (used by `forge web --reload`).

    The reloader spawns a worker subprocess that re-imports the app on every code
    change, so the app must be rebuildable from the environment alone. Runs dir
    comes from FORGE_RUNS_DIR (set by the CLI); tokens from the usual env vars.
    """
    import os
    from forge.config import Config
    from forge.store import Store
    from forge.session import SessionManager
    from forge.hostops import LocalHost
    from forge.cli import _populate_identity

    cfg = Config.from_env(Path(os.environ.get("FORGE_RUNS_DIR", "runs")))
    _populate_identity(cfg)
    store = Store(cfg.runs_dir / "forge.db")
    manager = SessionManager(cfg, store, LocalHost())
    app = create_app(cfg, store, manager)
    attach_background(app, cfg, store, manager)
    return app


def make_server(app, bus, host, port, timeout_graceful_shutdown=5):
    """uvicorn Server whose shutdown isn't held hostage by SSE streams.

    The web UI always has event-stream connections open (EventSource
    reconnects, heartbeats keep them alive), and uvicorn's graceful shutdown
    waits for open connections *indefinitely* by default — so a bare
    uvicorn.run() never gets past "Waiting for connections to close" on
    Ctrl+C. Two-part fix: the exit signal closes every bus subscription
    (tailing feeds end within milliseconds), and anything still streaming —
    an in-flight turn iterating the engine — is force-closed after
    `timeout_graceful_shutdown` seconds."""
    import uvicorn

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            bus.close_all()
            super().handle_exit(sig, frame)

    return Server(uvicorn.Config(
        app, host=host, port=port, log_level="info",
        timeout_graceful_shutdown=timeout_graceful_shutdown))


# Loopback Host names the local browser uses when talking to `forge web`
# directly. Only these get the full (unauthenticated) API.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def public_request_allowed(host: str, method: str, path: str,
                           public_host: str) -> bool:
    """Gate for the daemon when a public (tunnel) hostname fronts it.

    The whole ``/api/*`` surface — sessions, stop, batch — is unauthenticated
    by design (single-user, local), so it must never be reachable from the
    public ingress. This is an *allowlist*: only requests whose Host is a
    recognized loopback name get the full API; everything else — the public
    hostname, an unknown Host, or an absent Host header — is restricted to
    the one signed public route, the GitHub webhook. Fail closed, not open,
    so a client-supplied or rebound Host can't smuggle in the local API."""
    if not public_host:
        return True  # no tunnel configured; app is loopback-only anyway
    # Strip port and any trailing dot ("host." is the FQDN root form of
    # "host" — leaving it unnormalized is a classic Host-ACL bypass).
    h = (host or "").split(":", 1)[0].rstrip(".").lower()
    # *.localhost is loopback by definition (RFC 6761; browsers hardcode it):
    # the workspace serves itself from http://forge.localhost:<port> so its
    # app iframe (run-<id>.forge.localhost) is same-site and login cookies
    # survive — the API must accept that Host too.
    if h in _LOCAL_HOSTS or h.endswith(".localhost"):
        return True
    return method.upper() == "POST" and path == "/api/github/webhook"


def attach_public_gate(app, public_host: str) -> None:
    @app.middleware("http")
    async def _gate(request, call_next):
        if not public_request_allowed(request.headers.get("host", ""),
                                      request.method, request.url.path,
                                      public_host):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)


def attach_github_webhook(app, cfg, manager, ghapp_client, secret,
                          delivery_log=None) -> None:
    """POST /api/github/webhook: the @<slug> review comment-command trigger.
    Fast-ack (GitHub times out at 10s; provisioning takes minutes): all real
    work — reaction ack, review run, failure comment — happens on a daemon
    thread. Events still reach the bus via @published, so the run is watchable
    live in the web UI like any other."""
    import threading
    from forge import ghwebhook

    log = delivery_log or ghwebhook.DeliveryLog()

    def _run_review(cmd, run_id):
        ghwebhook.ack_comment(ghapp_client, cmd.owner, cmd.repo, cmd.comment_id)
        failed = None
        try:
            for ev in manager.review(run_id, f"{cmd.slug}#{cmd.number}",
                                     "auto", origin="github"):
                if ev.kind == "error":
                    failed = ev.data.get("detail") or ev.data.get("kind") or "error"
        except Exception as e:  # noqa: BLE001 - thread boundary: report, don't die
            logger.exception("github-triggered review crashed (run %s)", run_id)
            failed = str(e)[:200]
        if failed:
            ghwebhook.post_comment(
                ghapp_client, cmd.owner, cmd.repo, cmd.number,
                f"⚠️ forge review failed: {failed}")

    @app.post("/api/github/webhook")
    async def github_webhook(req: Request):
        body = await req.body()
        if not secret:
            return JSONResponse({"error": "webhook secret not configured"},
                                status_code=503)
        if not ghwebhook.verify_signature(
                secret, body, req.headers.get("X-Hub-Signature-256", "")):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        event = req.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return {"ok": True}
        guid = req.headers.get("X-GitHub-Delivery", "")
        if guid and log.seen(guid):
            return {"duplicate": True}
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        cmd = ghwebhook.parse_command(event, payload, cfg.gh_app_slug)
        if cmd is None:
            return {"ignored": True}
        ok, msg = manager.can_start()
        if not ok:
            threading.Thread(
                target=ghwebhook.post_comment,
                args=(ghapp_client, cmd.owner, cmd.repo, cmd.number,
                      f"⏳ forge is at capacity ({msg}) — try again shortly."),
                daemon=True).start()
            return {"accepted": False, "reason": "capacity"}
        run_id = uuid.uuid4().hex
        threading.Thread(target=_run_review, args=(cmd, run_id),
                         daemon=True).start()
        return {"accepted": True, "run_id": run_id}

    # create_app mounts the SPA StaticFiles at "/" and Starlette matches
    # routes in registration order, so a route appended after that mount is
    # unreachable (the mount answers POSTs with 405). Move the webhook route
    # ahead of the first mount.
    from starlette.routing import Mount
    route = app.router.routes.pop()
    idx = next((i for i, r in enumerate(app.router.routes)
                if isinstance(r, Mount)), len(app.router.routes))
    app.router.routes.insert(idx, route)


def tunnel_reconcile(live_ids, tunnel_ids):
    """Tunnels to stop = those whose run is no longer live."""
    return set(tunnel_ids) - set(live_ids)


def tunnel_sweep(store, tunnel, seen_state, on_transition):
    """One reconcile pass: stop orphan tunnels, fire a notice the first time a
    run enters asleep/deleted. `seen_state` carries last-seen state across
    sweeps so each transition notifies once."""
    # Treat starting/running envs as live too (same filter as reconcile): a
    # tunnel opened mid-provision (state 'starting', before the app is healthy)
    # is not an orphan — reaping it would kill the public URL during spin-up.
    live = {e["run_id"]
            for e in store.list_envs(states=("live", "starting", "running"))}
    for rid in tunnel_reconcile(live, tunnel.running_ids()):
        tunnel.stop(rid)
    # A woken run must notify again on its NEXT sleep: drop its stale entry,
    # otherwise seen_state still says "asleep" and the re-sleep never fires.
    for rid in live:
        seen_state.pop(rid, None)
    for e in store.list_envs(states=("asleep", "deleted")):
        if seen_state.get(e["run_id"]) != e["state"]:
            seen_state[e["run_id"]] = e["state"]
            if on_transition:
                try:
                    on_transition(e["run_id"], e["state"])
                except Exception:
                    pass


def _supabase_offsets(store) -> dict:
    return {r["run_id"]: r["offset"] for r in store.list_supabase()}


def refresh_proxy(store, cfg) -> None:
    """Attach Caddy to every live run's network and rewrite+reload its Caddyfile
    with per-run app + Supabase split routes. Safe to call repeatedly."""
    from forge import proxy
    live = store.list_envs(states=("live",))
    proxy.connect_networks([e["run_id"] for e in live])
    caddyfile = cfg.runs_dir / "Caddyfile"
    caddyfile.write_text(proxy.caddy_config(
        proxy.routes_for(live, supabase_offsets=_supabase_offsets(store),
                         domain=cfg.proxy_domain),
        cfg.proxy_port))
    proxy.reload_proxy()


def seed_seen_state(store) -> dict:
    """Baseline of runs already in a terminal-ish state (asleep/deleted) at
    process start. `tunnel_sweep` notifies only on a state *change*, so without
    this seed a daemon restart would re-fire the whole backlog of historical
    transitions — spamming "slept"/"removed" into every old Slack thread. A run
    can only enter asleep/deleted while the daemon is running (the reaper lives
    in-process), so its notice already went out; seeding the baseline means only
    genuinely new transitions after startup notify."""
    return {e["run_id"]: e["state"]
            for e in store.list_envs(states=("asleep", "deleted"))}


def attach_tunnel_lifecycle(app, cfg, store, manager, tunnel, on_transition):
    import threading
    import time as _time

    def _start_for_live():
        # Daemon-restart recovery: re-front Caddy for live envs. A restarted quick
        # tunnel gets a NEW url; the live container keeps its baked one, so
        # client-side Supabase may be stale until the session next re-provisions.
        for e in store.list_envs(states=("live",)):
            tunnel.start(e["run_id"], f"http://localhost:{cfg.proxy_port}",
                         host_header=f"run-{e['run_id']}.{cfg.proxy_domain}")

    seen_state: dict = seed_seen_state(store)

    def _t_startup():
        _start_for_live()

        def loop():
            while True:
                tunnel_sweep(store, tunnel, seen_state, on_transition)
                _time.sleep(30)

        threading.Thread(target=loop, daemon=True).start()

    _on_startup(app, _t_startup)


def drain_once(config, store, manager, dispatch) -> list:
    """One scheduler tick: claim up to admit_count() queued runs (oldest first)
    and hand each to `dispatch`. Dispatches exactly the rows claimed, so a row is
    never marked 'running' without a worker."""
    n = manager.admit_count()
    if n <= 0:
        return []
    ids = []
    for run in store.claim_queued(limit=n):
        dispatch(run["run_id"])
        ids.append(run["run_id"])
    return ids


def attach_background(app, config, store, manager):
    import threading
    import time
    from forge import lifecycle, proxy
    from datetime import datetime

    def _startup():
        # Restart recovery: re-queue batched workers the daemon left mid-flight,
        # BEFORE reconcile tears down their partial envs, so re-dispatch is clean.
        for rid in store.reclaim_orphans():
            store.add_event(rid, "queue", {"event": "reclaimed_on_restart"})
        manager.reconcile()
        caddyfile = config.runs_dir / "Caddyfile"
        caddyfile.write_text(proxy.caddy_config(
            proxy.routes_for(store.list_envs(states=("live",)),
                             supabase_offsets=_supabase_offsets(store),
                             domain=config.proxy_domain),
            config.proxy_port))
        proxy.ensure_proxy(str(caddyfile), config.proxy_port)

        def reap_loop():
            # Failures here were silently swallowed, which made "did the idle
            # reaper run at all?" impossible to answer — log every outcome.
            deferred_warned = set()
            while True:
                now = datetime.utcnow()
                # Idle live sessions go to sleep (resources freed, resumable).
                for rid in lifecycle.idle_run_ids(
                        store.list_envs(states=("live",)), now, config.env_ttl_secs):
                    try:
                        if manager.sleep(rid, reason="idle"):
                            logger.info("idle TTL: slept %s", rid)
                    except Exception:
                        logger.exception("idle sleep failed for %s", rid)
                # Sessions asleep past the dormant window are deleted (after a
                # commit+push archive guard inside delete_dormant).
                for rid in lifecycle.dormant_run_ids(
                        store.list_envs(states=("asleep",)), now,
                        config.dormant_ttl_secs):
                    try:
                        if manager.delete_dormant(rid):
                            logger.info("dormant TTL: deleted %s", rid)
                            deferred_warned.discard(rid)
                        elif rid not in deferred_warned:
                            deferred_warned.add(rid)
                            logger.warning("dormant delete deferred for %s "
                                           "(archive push failed)", rid)
                    except Exception:
                        logger.exception("dormant delete failed for %s", rid)
                refresh_proxy(store, config)
                # Recover any live web app stuck 5xx by a corrupted Next cache
                # (clear .next + restart the dev server). Best-effort; a bad
                # pass must never kill the reaper.
                try:
                    for rid in manager.heal_corrupted_web():
                        logger.info("web self-heal: recovered %s", rid)
                except Exception:
                    logger.exception("web self-heal pass failed")
                try:
                    removed = lifecycle.sweep_dead_networks(store)
                    if removed:
                        logger.info("network sweep: reclaimed %d subnet(s): %s",
                                    len(removed), ", ".join(removed))
                except Exception:
                    logger.exception("network sweep failed")
                time.sleep(30)

        def schedule_loop():
            # Drain the fire-and-forget batch queue: dispatch admitted runs to
            # autonomous worker threads. One new provision per stagger interval
            # avoids a Caddy-reload / SQLite thundering herd.
            def _dispatch(run_id):
                sink = manager._pop_sink(run_id)   # Slack thread renderer, if any
                threading.Thread(target=manager.run_autonomous,
                                 args=(run_id,), kwargs={"on_event": sink},
                                 daemon=True).start()
                time.sleep(config.queue_stagger_secs)
            while True:
                try:
                    drain_once(config, store, manager, _dispatch)
                except Exception:
                    pass          # a bad tick must never kill the scheduler
                time.sleep(config.queue_tick_secs)

        threading.Thread(target=reap_loop, daemon=True).start()
        threading.Thread(target=schedule_loop, daemon=True).start()

    _on_startup(app, _startup)
