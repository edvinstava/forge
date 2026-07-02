import json
import logging
import shlex
import threading
import time
import uuid
from pathlib import Path

from forge import commands as cmd
from forge import inbox
from forge import lifecycle
from forge import prbody
from forge import providers
from forge.config import parse_mem_mb
from forge.composeenv import ComposeEnv
from forge.eventbus import EventBus, published
from forge.events import TurnEvent   # re-exported: the engine's event type
from forge.health import health_poll_argv
from forge.probing import build_probe
from forge.prompts import (build_task_prompt, build_self_review_prompt,
                           build_fix_prompt,
                           build_plan_prompt, build_replan_prompt,
                           build_retrospective_prompt)
from forge import flow
from forge.recipe import (SUPABASE_LOCAL_ANON_KEY, apply_overlay,
                          apply_resource_limits, none_recipe, resolve)
from forge.knowledge import KnowledgeStore
from forge import envprobe
from forge import nextdev
from forge import proxy
from forge.runspec import make_runspec, normalize_github_repo
from forge.session_lifecycle import LifecycleOps
from forge.session_review import ReviewOps
from forge import supaports
from forge.supaports import NoFreePortBlock, SupabaseAllocator
from forge.verify import parse_verify
from forge.hostops import exclude_forge_scratch

__all__ = ["SessionManager", "TurnEvent", "default_env_factory"]

log = logging.getLogger("forge.session")


def _drain(gen):
    """Run a generator to completion and return its StopIteration value.
    Lets a non-generator caller (open_pr) reuse a generator-based helper
    (_repair) without streaming its events."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def default_env_factory(run_id, files):
    return ComposeEnv(run_id, files)


class SessionManager(ReviewOps, LifecycleOps):
    def __init__(self, config, store, host, env_factory=None,
                 clock=time.monotonic, tunnel=None, proxy_refresh=None):
        self.cfg, self.store, self.host = config, store, host
        # Default factory bakes in the configured compose-up timeout; tests
        # inject their own (FakeEnv) factory and bypass it.
        self.env_factory = env_factory or self._default_env_factory
        self.clock = clock
        self.tunnel = tunnel
        self._proxy_refresh = proxy_refresh or (lambda: None)
        self.knowledge = KnowledgeStore(config.knowledge_dir)
        self.provider = providers.from_config(config)
        self._verify_plans: dict = {}
        # Single-flight guard: at most one turn per run at a time. The manager is
        # driven concurrently (FastAPI threadpool, Slack daemon thread,
        # scheduler), so claim/release must be atomic — see _try_begin.
        self._active: set = set()
        self._active_lock = threading.Lock()
        self._waking: set = set()
        self._envs: dict = {}               # run_id -> cached ComposeEnv (live _proc)
        self._sleep_requested: dict = {}  # run_id → reason; warm-sleep at next boundary
        # Per-run progress sinks the scheduler passes to run_autonomous when it
        # dispatches a queued run (Slack registers a thread renderer here).
        self._event_sinks: dict = {}
        # Cross-surface live spine: every @published flow mirrors its events
        # here so the web feed and the Slack mirror see turns they didn't drive.
        self.bus = EventBus()
        self.alloc = SupabaseAllocator(store, stride=config.supabase_port_stride,
                                       max_blocks=config.supabase_max_blocks)

    def _default_env_factory(self, run_id, files):
        return ComposeEnv(run_id, files,
                          up_timeout=self.cfg.compose_up_timeout_secs)

    # --- single-flight turn guard ---
    def _try_begin(self, run_id) -> bool:
        """Atomically claim the run's turn slot. Returns False if a turn is
        already in flight. The check-and-add must be atomic: two surfaces
        racing a plain `in`/`add` pair could both start a worker in the same
        workspace and corrupt the diff/commit."""
        with self._active_lock:
            if run_id in self._active:
                return False
            self._active.add(run_id)
            return True

    def _end_active(self, run_id) -> None:
        with self._active_lock:
            self._active.discard(run_id)

    # --- helpers ---
    def _secrets(self) -> dict:
        # Empty defaults keep compose interpolation quiet for whichever agent
        # token the active provider doesn't use — and keep the inactive
        # provider's credentials out of the container entirely. The provider
        # decides its own auth (e.g. codex suppresses the API key when a
        # ChatGPT-plan login is mounted, so usage bills the subscription,
        # never per-token API costs).
        return {"CLAUDE_CODE_OAUTH_TOKEN": "", "OPENAI_API_KEY": "",
                "GH_TOKEN": self.cfg.gh_token,
                "FORGE_SUPABASE_ANON_KEY": SUPABASE_LOCAL_ANON_KEY,
                **self.provider.secrets(self.cfg)}

    def _session_id(self, run_id):
        """The run's resumable agent session id — but only when it was minted
        by the ACTIVE provider. After a FORGE_PROVIDER switch, feeding a claude
        session id to `codex exec resume` (or vice versa) would fail every
        subsequent turn; starting a fresh agent session is the correct
        degradation. Legacy rows (no agent_provider recorded) are claude's."""
        run = self.store.get_run(run_id) or {}
        sid = run.get("claude_session_id")
        if not sid:
            return None
        minted_by = run.get("agent_provider") or "claude"
        return sid if minted_by == self.provider.name else None

    def _persist_session_id(self, run_id, sid):
        self.store.set_session_fields(run_id, claude_session_id=sid,
                                      agent_provider=self.provider.name)

    def _stream_worker(self, run_id, env, prompt, model, redact=None):
        """Run one streaming worker turn (resuming the run's agent session) and
        translate the provider's output into narration/tool TurnEvents
        (optionally redacted — QA streams may echo credentials). Persists the
        new agent session id. RETURNS the final WorkerResult, or None when the
        stream ended without one (worker died)."""
        red = redact or (lambda s: s)
        sid = self._session_id(run_id)
        parser = self.provider.stream_parser()
        result = None
        for line in env.exec_stream(self.provider.stream_cmd(prompt, model, sid),
                                    service="forge"):
            ev = parser.feed(line)
            if ev is None:
                continue
            if ev.kind == "narration":
                yield TurnEvent("narration", {"text": red(ev.text)})
            elif ev.kind == "tool":
                yield TurnEvent("tool", {"name": red(ev.text),
                                         "target": red(ev.target)})
            elif ev.kind == "result":
                result = ev.result
        if result is not None and result.session_id:
            self._persist_session_id(run_id, result.session_id)
        return result

    def _run_worker(self, run_id, env, prompt, model):
        """One blocking (non-streamed) worker turn resuming the run's agent
        session; persists the new session id. Returns the WorkerResult."""
        sid = self._session_id(run_id)
        res = env.exec(self.provider.worker_cmd(prompt, model, sid),
                       service="forge")
        wr = self.provider.parse_result(res.stdout)
        if wr.session_id:
            self._persist_session_id(run_id, wr.session_id)
        return wr

    def _ghapp(self):
        from forge import ghapp
        return ghapp.GhApp(self.cfg) if ghapp.is_configured(self.cfg) else None

    def _commit_identity(self, run_id):
        """Resolve (name, email) for commits per cfg.commit_identity.
        auto = the user authors and forge[bot] is credited via a
        Co-Authored-By trailer (_commit_trailer); forge = always bot (error
        if no App); user = always the user, no trailer."""
        mode = (self.cfg.commit_identity or "auto").lower()
        user = (self.cfg.git_author_name or "Forge User",
                self.cfg.git_author_email or "forge@local")
        if mode != "forge":
            return user
        app = self._ghapp()
        if app is None:
            raise RuntimeError(
                "commit_identity='forge' but GitHub App not configured "
                "(set FORGE_GH_APP_ID and FORGE_GH_APP_KEY)")
        return app.bot_identity()

    def _commit_trailer(self, run_id) -> str:
        """`Co-Authored-By: <bot>` line for auto-mode commits — the user
        authors, forge shows as a contributor. '' when a mode pins a single
        identity or the App is missing/unreachable (never blocks a commit)."""
        if (self.cfg.commit_identity or "auto").lower() != "auto":
            return ""
        app = self._ghapp()
        if app is None:
            return ""
        try:
            login, email = app.bot_identity()
        except Exception:
            return ""
        return f"Co-Authored-By: {login} <{email}>"

    def _lockfile_hash(self, ws) -> str:
        """A stable signature of the dependency lockfile, so wake can tell whether
        the agent changed deps (stale node_modules → must reinstall → cold wake)."""
        import hashlib
        for name in ("bun.lock", "bun.lockb", "pnpm-lock.yaml", "yarn.lock",
                     "package-lock.json"):
            txt = self.host.read(ws, name)
            if txt:
                return hashlib.sha256(txt.encode("utf-8", "ignore")).hexdigest()[:16]
        return "none"

    def _warm_eligible(self, run_id, ws) -> bool:
        row = self.store.get_env(run_id)
        if row.get("state") != "asleep":
            return False
        sig = row.get("snapshot_lockhash")
        return bool(sig) and sig == self._lockfile_hash(ws)

    def _compose_path(self, run_id) -> Path:
        return Path(self.cfg.runs_dir) / run_id / "forge-compose.yml"

    def _env_for(self, run_id):
        cf = self._compose_path(run_id)
        files = [str(cf)] if cf.is_file() else []
        env = self.env_factory(run_id, files)
        # Remember the latest env per run so stop() can cancel whatever
        # subprocess is currently streaming (the env used right after this call).
        # Fresh instance each call is intentional — provision health-retry rebuilds.
        self._envs[run_id] = env
        return env

    def _provisioned(self, run_id) -> bool:
        """True when the run has a live/starting compose env (a turn can exec)."""
        return self.store.get_env(run_id).get("state") in ("live", "starting")

    @staticmethod
    def _is_next_supabase(probe) -> bool:
        return probe.has_supabase_config and "next" in (probe.package_json or "")

    def _reserve_supabase(self, run_id, ws) -> int:
        """Reserve a free port block and rewrite the cloned config.toml so this
        run's Supabase gets a unique project_id + ports. Returns the offset."""
        rel = "supabase/config.toml"
        text = self.host.read(ws, rel) or ""
        base = supaports.read_project_id(text) or "forge"
        project = f"{base}-{run_id[:8]}"
        offset = self.alloc.reserve(run_id, text, project)
        self.host.write_file(str(Path(ws) / rel),
                             supaports.rewrite_config(text, project, offset))
        # The rewrite is infra, not the user's work: hide it from git so it never
        # lands in the worker's diff or PR (skip-worktree keeps `supabase stop`
        # working since the offset project_id stays on disk).
        self.host.run(["git", "-C", ws, "update-index", "--skip-worktree", rel])
        return offset

    def _repo_slug(self, run_id):
        run = self.store.get_run(run_id)
        return (run or {}).get("repo")

    def _lessons(self, run_id) -> list:
        """The repo's learned lesson texts, for the planner to apply. [] when the
        repo has no overlay / no lessons yet."""
        slug = self._repo_slug(run_id)
        if not slug:
            return []
        overlay = self.knowledge.load(slug) or {}
        return [l["text"] for l in overlay.get("lessons", [])
                if isinstance(l, dict) and l.get("text")]

    def _recipe_for(self, run_id, ws):
        probe = build_probe(self.host, ws)
        seed_dir = str(Path(self.cfg.runs_dir) / "cache" / "dhis2-seed")
        offset = self._reserve_supabase(run_id, ws) if self._is_next_supabase(probe) else 0
        slug = self._repo_slug(run_id)
        overlay = self.knowledge.load(slug) if slug else None
        recipe = resolve(probe, ws, self.cfg.image_tag, seed_dir=seed_dir,
                         supabase_offset=offset, overlay=overlay)
        return self._cap(recipe), probe

    def _cap(self, recipe):
        """Bound a leaky dev server so it can't consume the whole host (see
        recipe.apply_resource_limits). Applied wherever a recipe's compose is
        (re)generated — incl. the self-heal repair path, which rebuilds the
        web command and would otherwise drop the cap."""
        return apply_resource_limits(
            recipe, mem_limit=self.cfg.web_mem_limit,
            node_max_old_space_mb=self.cfg.web_node_max_old_space_mb)

    def _write_worker_only_compose(self, run_id, ws):
        """A worker-only compose (the `forge` service is recipe-independent) so
        the self-heal probe can exec an agent before the full stack is resolved."""
        compose = none_recipe(ws, self.cfg.image_tag).compose
        self._mount_provider_auth(compose)   # the probe agent needs auth too
        self.host.write_file(str(self._compose_path(run_id)),
                             json.dumps(compose, indent=2))

    @published
    def start(self, run_id, repo, source, task=""):
        # Create the run row first, then boot it. _boot is shared with the
        # autonomous batch path (run_autonomous), where the row already exists
        # (enqueued as 'queued', then claimed). `task` (when the caller already
        # knows it, e.g. Slack) seeds the branch name and PR title fallback.
        self.store.create_run(run_id, repo, task or "", "")
        self.store.set_session_fields(run_id, repo_source=f"{source}:{repo}")
        yield from self._boot(run_id, repo, source)

    def _boot(self, run_id, repo, source):
        """Provision an existing run row: normalize the repo, compute + persist the
        branch, clone, then _provision. Yields TurnEvents. Shared by start()
        (interactive) and run_autonomous() (batched)."""
        try:
            if source == "github":
                repo = normalize_github_repo(repo)   # accept pasted URLs, not just slugs
            # Branch names carry the task when known (forge/57d7cf1c/fix-offer-table),
            # falling back to the generic session slug for bare interactive starts.
            task = (self.store.get_run(run_id) or {}).get("task") or "session"
            rs = make_runspec(repo if source == "github" else "local/repo", task, run_id)
        except ValueError as e:
            # Bad repo input must surface as a clean error bubble, not a 500 that
            # crashes the SSE stream and leaves an unqueryable orphan session.
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "repo", "detail": str(e)[:300]})
            return
        self.store.set_run_target(run_id, repo=repo, branch=rs.branch)
        # Re-record repo_source with the NORMALIZED repo (start seeds it with the
        # raw input before normalization; the batch path already stores the slug).
        self.store.set_session_fields(run_id, repo_source=f"{source}:{repo}")
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        self.store.set_state(run_id, "provisioning")
        yield TurnEvent("phase", {"name": "clone", "label": "Cloning"})
        cl = (self.host.clone_local(repo, rs.branch, ws) if source == "local"
              else self.host.clone(repo, rs.branch, ws, self.cfg.gh_token))
        if cl.exit_code != 0:
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "clone",
                                      "detail": (cl.stderr or cl.stdout)[:300]})
            return
        exclude_forge_scratch(self.host, ws)
        yield from self._provision(run_id, ws)

    def _provision(self, run_id, ws, warm=False):
        """Provision (start) or re-provision (wake) the env against an on-disk
        workspace: recipe, Supabase reserve, compose up, seed, health/URL.
        Yields TurnEvents. Shared by start() and wake()."""
        self.store.set_state(run_id, "provisioning")
        # Idempotent, and deliberately on the wake path too: a workspace cloned
        # by an older forge gains scratch patterns added since (e.g.
        # .forge/pr.json) instead of committing them into its next PR.
        try:
            exclude_forge_scratch(self.host, ws)
        except Exception:
            pass
        try:
            recipe, probe = self._recipe_for(run_id, ws)
        except NoFreePortBlock as e:
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "ports", "detail": str(e)[:300]})
            return
        yield TurnEvent("phase", {"name": "recipe", "label": f"Recipe: {recipe.name}"})
        slug = self._repo_slug(run_id)
        # Confidence gate: if the resolver is unsure (and we have no learned
        # overlay yet), let an agent inspect the live instance and emit one, then
        # re-resolve. The forge worker service is recipe-independent, so we bring
        # it up alone to probe before committing to the full stack.
        if (not warm and self.cfg.self_heal and slug and recipe.confidence == "low"
                and self.knowledge.load(slug) is None):
            yield TurnEvent("phase", {"name": "probe", "label": "Probing stack"})
            self._write_worker_only_compose(run_id, ws)
            probe_env = self._env_for(run_id)
            try:
                probe_env.up(self._secrets())
                learned = envprobe.probe(probe_env, model=None,
                                         max_iterations=self.cfg.probe_max_iterations,
                                         provider=self.provider)
            except Exception:
                learned = None
            if learned:
                self.knowledge.save(slug, {**learned, "repo": slug,
                                           "provenance": {"learned_by": "agent"}})
                recipe, probe = self._recipe_for(run_id, ws)   # re-resolve with overlay
        # Start the public tunnel (fronts the shared Caddy) so the app has a
        # remote URL; the per-run host header lets Caddy pick the right route.
        # `origin` is the public URL surfaced to the user (see _register below).
        origin = None
        if self.tunnel is not None and recipe.web_service:
            origin = self.tunnel.start(
                run_id, f"http://localhost:{self.cfg.proxy_port}",
                host_header=f"run-{run_id}.{self.cfg.proxy_domain}")
        if (self.tunnel is not None and recipe.name == "next-supabase"
                and recipe.compose is not None):
            # The web app needs a Supabase URL reachable from BOTH the host
            # browser and the server-side client inside the web container. The
            # public tunnel host satisfies neither reliably here: a router that
            # NXDOMAINs *.trycloudflare.com blocks the browser, and the container
            # can't resolve it either ("fetch failed ENOTFOUND" -> login fails).
            # The DNS-free local proxy URL works for both: the host browser
            # resolves *.localhost -> 127.0.0.1 -> Caddy, and the container
            # reaches the same Caddy via the run host mapped to the host gateway.
            # Caddy path-splits /auth,/rest,... same-origin to the host Supabase.
            local = proxy.local_url(run_id, self.cfg.proxy_domain,
                                    self.cfg.proxy_port)
            web = recipe.compose["services"]["web"]
            web["environment"]["NEXT_PUBLIC_SUPABASE_URL"] = local
            web.setdefault("extra_hosts", []).append(
                f"run-{run_id}.{self.cfg.proxy_domain}:host-gateway")
        if recipe.compose is not None:
            self._mount_provider_auth(recipe.compose)
            self.host.write_file(str(self._compose_path(run_id)),
                                 json.dumps(recipe.compose, indent=2))
        if recipe.web_service:
            # Next dev blocks cross-origin HMR/dev assets AND Server Actions; the
            # app is served via the Caddy proxy + tunnel, so without this the
            # browser never live-reloads and form-driven actions (e.g. login)
            # fail with "Invalid Server Actions request". The Server Action guard
            # matches the origin host WITH its port, so pass the proxy port.
            # Best-effort: a non-Next app or odd config is a no-op.
            try:
                nextdev.ensure_dev_origins(self.host, ws,
                                           proxy_domain=self.cfg.proxy_domain,
                                           proxy_port=self.cfg.proxy_port)
            except Exception:
                pass
        self._verify_plans[run_id] = parse_verify(
            probe.package_json, probe.repo_yml,
            self.host.exists(ws, ".forge/verify.sh"), probe.pkg_manager)
        env = self._env_for(run_id)
        self.store.create_env(run_id, f"forge-{run_id}", None, recipe.web_port,
                              "starting", web_service=recipe.web_service,
                              runtime_facts=json.dumps(recipe.runtime_facts()))
        for hc in recipe.host_pre:
            r = self.host.run(hc)
            if r.exit_code != 0:
                # A failed host_pre (e.g. `supabase start`: port clash, missing
                # CLI, Docker down) otherwise surfaces minutes later as a cryptic
                # health timeout. Abort now, naming the command and the reason.
                self.store.set_state(run_id, "failed")
                self.store.set_env_state(run_id, "failed")
                self._release_supabase(run_id)
                tail = (r.stderr or r.stdout or "").strip().splitlines()
                why = tail[-1] if tail else f"exit {r.exit_code}"
                yield TurnEvent("error", {
                    "kind": "host_pre",
                    "detail": f"`{' '.join(hc)}` failed: {why}"[:300]})
                return
        yield TurnEvent("phase", {"name": "up",
                                  "label": "Resuming stack" if warm else "Starting stack"})
        try:
            if warm:
                env.start()
            else:
                env.up(self._secrets())
        except Exception as e:                      # compose up failed
            self.store.set_state(run_id, "failed")
            self.store.set_env_state(run_id, "failed")
            self._release_supabase(run_id)          # don't orphan host_pre's supabase
            yield TurnEvent("error", {"kind": "up", "detail": str(e)[:300]})
            return
        self.store.set_state(run_id, "running")
        env.exec(cmd.setup_git_cmd(), service="forge")
        for svc, argv in recipe.seed:
            env.exec(argv, service=svc)
        # The web container + its network now exist; let Caddy attach and pick up
        # this run's split route before we hand the URL back to the user.
        try:
            self._proxy_refresh()
        except Exception:
            pass
        web_url = self._register(env, run_id, recipe, public_url=origin)
        if web_url:
            yield TurnEvent("url", self._url_payload(run_id, web_url, origin))
        elif recipe.web_service and self.cfg.self_heal and slug:
            # Health failed: let an agent diagnose the live container, learn an
            # overlay delta, and retry ONCE. apply_overlay patches the existing
            # recipe (no Supabase re-reserve) and preserves any baked env.
            yield TurnEvent("phase", {"name": "repair", "label": "Repairing env"})
            logs = env.exec(["sh", "-lc", "tail -n 80 /proc/1/fd/1 2>/dev/null || true"],
                            service=recipe.web_service).stdout
            delta = envprobe.repair(env, "health", logs, model=None,
                                    max_iterations=self.cfg.probe_max_iterations,
                                    provider=self.provider)
            if delta:
                self.knowledge.merge_save(slug, {**delta,
                                                 "provenance": {"learned_by": "agent"}})
                env.down()
                recipe = self._cap(apply_overlay(recipe, delta))
                self.host.write_file(str(self._compose_path(run_id)),
                                     json.dumps(recipe.compose, indent=2))
                env = self._env_for(run_id)
                try:
                    env.up(self._secrets())
                except Exception as e:              # repaired compose still failed
                    self.store.set_state(run_id, "failed")
                    self.store.set_env_state(run_id, "failed")
                    self._release_supabase(run_id)
                    yield TurnEvent("error", {"kind": "up", "detail": str(e)[:300]})
                    return
                try:
                    self._proxy_refresh()
                except Exception:
                    pass
                web_url = self._register(env, run_id, recipe, public_url=origin)
            if web_url:
                yield TurnEvent("url", self._url_payload(run_id, web_url, origin))
            else:
                self.store.set_state(run_id, "failed")
                self._release_supabase(run_id)
                yield TurnEvent("error", {"kind": "health", "detail": "health check failed"})
        elif recipe.web_service:
            self.store.set_state(run_id, "failed")
            self._release_supabase(run_id)          # don't orphan host_pre's supabase
            yield TurnEvent("error", {"kind": "health", "detail": "health check failed"})
        else:
            yield TurnEvent("phase", {"name": "noweb", "label": "No web service"})

    def _mount_provider_auth(self, compose) -> None:
        """Codex plan-based auth lives in ~/.codex (auth.json, refreshed by the
        CLI) — mount it into the worker so a ChatGPT subscription works without
        an API key. Claude needs nothing here (token env suffices). Mutates the
        compose dict in place; a compose without a forge service is a no-op."""
        if self.provider.name != "codex" or self.cfg.codex_auth == "api":
            return
        home = providers.codex_home()
        svc = (compose.get("services") or {}).get("forge")
        if svc is None or not home.is_dir():
            return
        svc.setdefault("volumes", []).append(f"{home}:/home/forge/.codex")

    def _url_payload(self, run_id, web_url, origin):
        """url-event data. `local_url` is the DNS-free *.forge.localhost proxy
        link, included only when a tunnel fronts the shared Caddy for this run
        (origin set) so a browser on the host's own network can always open the
        app even if the public tunnel hostname won't resolve there."""
        local = (proxy.local_url(run_id, self.cfg.proxy_domain, self.cfg.proxy_port)
                 if origin else None)
        return {"web_url": web_url, "local_url": local}

    def _register(self, env, run_id, recipe, public_url=None):
        if not recipe.web_service:
            self.store.set_env_state(run_id, "live")    # worker-only: env is usable
            return None
        h = env.exec(health_poll_argv(recipe.web_port, recipe.health_path,
                                      self.cfg.health_timeout_secs,
                                      host=recipe.web_service), service="forge")
        if h.exit_code != 0:
            self.store.set_env_state(run_id, "failed")
            return None
        hp = env.port(recipe.web_service, recipe.web_port)
        from forge.envreg import web_url
        url = public_url or (web_url(hp) if hp else None)
        self.store.set_env_state(run_id, "live", url)
        return url

    # --- turn helpers ---

    def _app_url(self, run_id):
        env_row = self.store.get_env(run_id)
        svc, port = env_row.get("web_service"), env_row.get("web_port")
        return f"http://{svc}:{port}" if svc and port else None

    def _runtime_facts(self, run_id):
        """The resolved environment's operational facts (endpoints + canonical
        commands), persisted at provision, for embedding in agent prompts. None
        when unavailable (old env row / worker-only)."""
        raw = self.store.get_env(run_id).get("runtime_facts")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _get_verify_plan(self, run_id):
        """Return verify plan for run_id, recomputing if missing (e.g. after restart)."""
        plan = self._verify_plans.get(run_id)
        if plan is None:
            ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
            probe = build_probe(self.host, ws)
            plan = parse_verify(probe.package_json, probe.repo_yml,
                                self.host.exists(ws, ".forge/verify.sh"),
                                probe.pkg_manager)
            self._verify_plans[run_id] = plan
        return plan

    def _run_verify(self, env, plan):
        failures = []
        for c in plan.commands:
            res = env.exec(c.argv, service="forge")
            if res.exit_code != 0:
                failures.append((c.name, (res.stdout + res.stderr)[-2000:]))
        return failures

    def _refresh_url(self, env, run_id):
        row = self.store.get_env(run_id)
        if not row.get("web_service"):
            return None
        # A tunnel-fronted run's canonical URL is the public origin, not the host
        # port. Prefer it so a turn never downgrades the stored link to a
        # localhost:port that only resolves on the host (breaking the public link
        # for anyone viewing the session in the web app or via Slack).
        public = self.tunnel.url_for(run_id) if self.tunnel else None
        if public:
            self.store.set_env_state(run_id, "live", public)
            return public
        hp = env.port(row["web_service"], row["web_port"])
        from forge.envreg import web_url
        url = web_url(hp) if hp else row.get("web_url")
        if url:
            self.store.set_env_state(run_id, "live", url)
        return url

    def _diff_file_count(self, env) -> int:
        # Mirror diff(): `git add -A -N` (intent-to-add) so brand-new untracked
        # files are counted too, keeping this count consistent with the patch.
        r = env.exec(["bash", "-lc", "git add -A -N && git diff --name-only HEAD"],
                     service="forge")
        return len([x for x in r.stdout.splitlines() if x.strip()])

    def _review_issue_count(self, run_id) -> int:
        from forge import review
        p = (Path(self.cfg.runs_dir) / run_id / "workspace"
             / ".forge" / "review.json")
        text = p.read_text() if p.is_file() else ""
        return len(review.parse_review(text).comments)

    def _self_review_and_fix(self, run_id) -> int:
        """Worker reviews its own uncommitted diff and fixes quality issues.
        Returns issue count. Verification-driven fixing lives in
        _repair, which open_pr always runs (independent of this flag)."""
        if not self.cfg.self_review:
            return 0
        env = self._env_for(run_id)
        self._reset_artifacts(run_id)        # review.json lives beside artifacts
        chosen = self.provider.resolve_model("auto", "debug review fix")  # heavy
        self._run_worker(run_id, env, build_self_review_prompt(), chosen)
        n = self._review_issue_count(run_id)
        self.store.add_message(
            run_id, "system",
            f"Self-review: addressed {n} issue(s) before opening PR.")
        return n

    def _repair(self, run_id, env, plan, model="auto", extra_guidance=""):
        """Verify the working tree the way CI will and try to make it green:
        deterministic format-fix, then up to max_repair_iters worker fixes, each
        followed by re-verify. Yields a `verify` event (initial) and a `repair`
        event per fix iteration; RETURNS the names of checks STILL failing ([] =
        green or no real verification). Never commits/pushes; never touches
        self._active (callers own it)."""
        if plan.format_fix:
            env.exec(plan.format_fix.argv, service="forge")
        if not plan.has_real_verification:
            return []
        self.store.set_lifecycle_state(run_id, flow.VERIFYING)
        self.store.set_state(run_id, "verifying")
        failures = self._run_verify(env, plan)
        yield TurnEvent("verify", {"ok": not failures,
                                   "failed": [n for n, _ in failures],
                                   "output": "\n\n".join(o for _, o in failures)[:4000]})
        if not failures:
            return []
        start = self.clock()
        chosen = self.provider.resolve_model(model, "fix verification failures")
        it = 0
        while failures and it < self.cfg.budget.max_repair_iters:
            it += 1
            self.store.set_lifecycle_state(run_id, flow.REPAIRING)
            self.store.set_state(run_id, "repairing")
            yield TurnEvent("repair", {"iter": it, "failed": [n for n, _ in failures]})
            prompt = build_fix_prompt(failures)
            if extra_guidance:
                prompt += f"\n\nHuman guidance: {extra_guidance}\n"
                extra_guidance = ""        # only inject on the first iteration
            self._run_worker(run_id, env, prompt, chosen)
            if plan.format_fix:
                env.exec(plan.format_fix.argv, service="forge")
            failures = self._run_verify(env, plan)
            if self.clock() - start >= self.cfg.budget.max_wall_secs:
                break
        if not failures:
            yield TurnEvent("verify", {"ok": True, "failed": [], "output": ""})
        return [n for n, _ in failures]

    def _read_pr_meta(self, run_id) -> dict:
        """The agent-authored PR title/body (.forge/pr.json), validated.
        {"title": None, "body": None} when absent or malformed."""
        p = Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "pr.json"
        return prbody.parse_pr_meta(p.read_text() if p.is_file() else "")

    def _finish_pr(self, run_id, env, verify_failed, note=None):
        """Commit + push + open a PR. Draft iff checks are failing, the repo has
        no real verification, or an explicit `note` marks it unverified (e.g. a
        browser-QA login wall — CI is green but the UI was never checked). Caller
        owns _active and has already run repair.

        The title/body come from the agent's own .forge/pr.json (it knows what
        changed and why); prbody supplies task-derived fallbacks and carries
        issue keys (ABC-374 style) into the title so trackers auto-link.
        Returns {ok, pr_url, draft, verify_failed} or {ok: False, reason}."""
        run = self.store.get_run(run_id)
        name, email = self._commit_identity(run_id)
        plan_doc = self._read_plan(run_id)
        task = (run.get("task") or (plan_doc.goal if plan_doc else "")
                or run.get("title") or "")
        meta = self._read_pr_meta(run_id)
        refs = prbody.issue_refs(task)
        title = prbody.ensure_issue_ref(
            meta["title"] or prbody.fallback_title(task, run.get("repo") or ""),
            refs)
        # Container package managers may rewrite lockfile metadata with zero
        # dependency change — drop that churn before `git add -A` sweeps it in.
        env.exec(cmd.restore_lockfile_churn_cmd(), service="forge")
        # Forge's Next origin patch is runtime-only scaffolding: strip it (and
        # clear its skip-worktree) around the commit so an agent's REAL config
        # edits ship in the PR while forge's block never does.
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        try:
            unpatched = nextdev.unpatch_for_commit(self.host, ws)
        except Exception:
            unpatched = []
        trailer = self._commit_trailer(run_id)
        commit_msg = f"{title}\n\n{trailer}" if trailer else title
        try:
            for cc in cmd.commit_cmds(commit_msg, name, email):
                env.exec(cc, service="forge")
            if env.exec(cmd.push_cmd(run["branch"]), service="forge").exit_code != 0:
                return {"ok": False, "reason": "push_failed"}
            plan = self._get_verify_plan(run_id)
            draft = (not plan.has_real_verification) or bool(verify_failed) or bool(note)
            warning = note
            if not warning and verify_failed:
                warning = ("Opened as a draft — these checks still fail locally: "
                           + ", ".join(verify_failed) + ". Forge attempted automatic "
                           "fixes but could not get them green; please review before "
                           "merging.")
            body = prbody.compose_body(task=task, run_id=run_id, branch=run["branch"],
                                       meta_body=meta["body"], refs=refs,
                                       warning=warning)
            env.exec(["bash", "-lc", f"printf '%s' {shlex.quote(body)} > /work/report.md"],
                     service="forge")
            pr = env.exec(cmd.pr_create_cmd(title, "/work/report.md", draft),
                          service="forge")
        finally:
            if unpatched:   # keep the live dev server proxied whatever happened
                try:
                    nextdev.ensure_dev_origins(self.host, ws,
                                               proxy_domain=self.cfg.proxy_domain,
                                               proxy_port=self.cfg.proxy_port)
                except Exception:
                    pass
        lines = pr.stdout.strip().splitlines()
        pr_url = lines[-1] if (pr.exit_code == 0 and lines) else None
        if not pr_url:
            return {"ok": False, "reason": "pr_failed"}
        self.store.set_state(run_id, self.store.get_run(run_id)["state"], pr_url=pr_url)
        msg = f"PR opened{' (draft)' if draft else ''}: {pr_url}"
        if verify_failed:
            msg += f" — checks still failing: {', '.join(verify_failed)}"
        self.store.add_message(run_id, "system", msg)
        return {"ok": True, "pr_url": pr_url, "draft": draft, "verify_failed": verify_failed}

    @published
    def turn(self, run_id, prompt, model="auto", attachments=None):
        if not self._try_begin(run_id):
            yield TurnEvent("error", {"kind": "busy", "detail": "a turn is in flight"})
            return
        try:
            self.store.add_message(run_id, "user", prompt)
            self.store.set_state(run_id, "running")
            self._reset_artifacts(run_id)
            self._reset_pr_meta(run_id)
            env = self._env_for(run_id)
            full = build_task_prompt(prompt, self._app_url(run_id),
                                     env=self._runtime_facts(run_id),
                                     lessons=self._lessons(run_id),
                                     attachments=self._attachment_paths(run_id, attachments))
            # Resolve which model runs this turn (auto = heuristic from prompt) and
            # surface it so the UI can show e.g. "auto → opus" before output starts.
            chosen = self.provider.resolve_model(model, prompt)
            yield TurnEvent("model", {"choice": model, "resolved": chosen})
            yield TurnEvent("phase", {"name": "agent", "label": "Agent working"})
            result = yield from self._stream_worker(run_id, env, full, chosen)
            if result is None:
                yield TurnEvent("error", {"kind": "worker", "detail": "no result event"})
                return
            if result.auth_error:
                self.store.add_message(run_id, "system",
                                       "Agent auth/usage problem.")
                yield TurnEvent("error", {"kind": "auth",
                                          "detail": result.result_text[:300]})
                return
            # Verify is tri-state: None when the repo has no checks configured
            # (so the UI never shows a misleading "passing" for work that was
            # never tested), else a real pass/fail with captured output.
            plan = self._get_verify_plan(run_id)
            verify_ok = None
            verify_failed: list = []
            verify_output = ""
            if plan.has_real_verification:
                self.store.set_state(run_id, "verifying")
                failures = self._run_verify(env, plan)
                verify_ok = not failures
                verify_failed = [n for n, _ in failures]
                verify_output = "\n\n".join(o for _, o in failures)[:4000]
                yield TurnEvent("verify", {
                    "ok": verify_ok,
                    "failed": verify_failed,
                    "output": verify_output,
                })
            recipe_url = self._refresh_url(env, run_id)
            if recipe_url:
                yield TurnEvent("url", {"web_url": recipe_url})
            diff_files = self._diff_file_count(env)
            self.store.add_message(
                run_id, "assistant", result.result_text or "(done)",
                meta={"cost": result.total_cost_usd, "model": chosen,
                      "diff_files": diff_files, "verify_ok": verify_ok,
                      "verify_failed": verify_failed, "verify_output": verify_output},
            )
            self.store.set_state(run_id, "running")
            self.store.touch_env(run_id)
            yield TurnEvent("done", {"message": result.result_text,
                                     "diff_files": diff_files,
                                     "verify_ok": verify_ok})
        except Exception:
            log.exception("turn failed run=%s", run_id)
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "internal",
                                      "detail": "turn failed unexpectedly"})
        finally:
            self._end_active(run_id)

    @published
    def plan_task(self, run_id, task, model="auto", policy=None, autonomous=False,
                  auto_draft=None, attachments=None):
        # autonomous=True (fire-and-forget batch) never stalls on a human: it
        # skips the ambiguity gate and, on verify/QA bottom-out, opens a DRAFT PR
        # instead of a repair-escalation checkpoint. It is NOT derived from the
        # policy — the escalation tests use for_cli(auto=True) and still expect a
        # gate.
        #
        # auto_draft is the *execution* half of autonomy, decoupled so Slack can
        # keep the ambiguity gate (autonomous=False) yet still draft-a-PR on every
        # bottom-out instead of stalling. Defaults to `autonomous` for back-compat
        # (batch = both; web/CLI = neither). Persisted so respond_checkpoint keeps
        # drafting after a resume.
        if auto_draft is None:
            auto_draft = autonomous
        policy = policy or flow.CheckpointPolicy.for_web()
        if not self._provisioned(run_id):
            yield TurnEvent("error", {"kind": "not_provisioned",
                                      "detail": "session is not live; start or wake it first"})
            return
        if not self._try_begin(run_id):
            yield TurnEvent("error", {"kind": "busy", "detail": "a turn is in flight"})
            return
        try:
            self.store.add_message(run_id, "user", task)
            self.store.set_task(run_id, task)   # canonical task → PR title fallback
            self.store.set_attachments(run_id, json.dumps(list(attachments or [])))
            self.store.set_auto_draft(run_id, auto_draft)
            self.store.set_lifecycle_state(run_id, flow.PLANNING)
            self.store.set_state(run_id, "planning")
            env = self._env_for(run_id)
            chosen = self.provider.resolve_model(model, task)
            yield TurnEvent("model", {"choice": model, "resolved": chosen})
            yield TurnEvent("phase", {"name": "planning", "label": "Planning"})
            yield from self._stream_worker(
                run_id, env,
                build_plan_prompt(task, self._app_url(run_id),
                                  lessons=self._lessons(run_id),
                                  env=self._runtime_facts(run_id),
                                  attachments=self._attachment_paths(run_id, attachments)), chosen)
            plan = self._read_plan(run_id)
            if plan is None:
                self.store.set_lifecycle_state(run_id, flow.FAILED)
                yield TurnEvent("error", {"kind": "plan",
                                          "detail": "planner produced no valid .forge/plan.json"})
                return
            self.store.set_plan(run_id, json.dumps(plan.to_dict()))
            yield TurnEvent("plan", plan.to_dict())
            gate_plan = policy.gates(flow.PLAN_APPROVAL)
            if gate_plan or (plan.has_open_questions and not autonomous):
                ctype = (flow.PLAN_APPROVAL if gate_plan
                         else flow.AMBIGUITY)
                cid = self.store.create_checkpoint(run_id, ctype, {"plan": plan.to_dict()})
                self.store.set_lifecycle_state(run_id, flow.AWAITING_APPROVAL)
                self.store.set_state(run_id, "awaiting_approval")
                prompt = ("Approve this plan to proceed, or describe changes."
                          if ctype == flow.PLAN_APPROVAL
                          else "Please answer the open questions, then I'll proceed.")
                yield TurnEvent("checkpoint", {"id": cid, "type": ctype, "prompt": prompt})
                return
            yield from self._execute(run_id, model, auto_draft=auto_draft)
        except Exception:
            log.exception("plan_task failed run=%s", run_id)
            self.store.set_lifecycle_state(run_id, flow.FAILED)
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "internal",
                                      "detail": "planning failed unexpectedly"})
        finally:
            self._end_active(run_id)

    @published
    def respond_checkpoint(self, run_id, checkpoint_id, action, body=None, model="auto"):
        cp = self.store.open_checkpoint(run_id)
        if not cp or cp["id"] != int(checkpoint_id):
            yield TurnEvent("error", {"kind": "checkpoint",
                                      "detail": "no matching open checkpoint"})
            return
        if action in ("approve", "edit") and not self._provisioned(run_id):
            yield TurnEvent("error", {"kind": "not_provisioned",
                                      "detail": "session is not live; wake it before approving"})
            return
        if not self._try_begin(run_id):
            yield TurnEvent("error", {"kind": "busy", "detail": "a turn is in flight"})
            return
        try:
            self.store.answer_checkpoint(int(checkpoint_id), {"action": action, "body": body})
            # A NEEDS_INPUT reply is the QA login prompt — the body is a
            # password. Keep the raw value for parse_credentials / _execute
            # below, but never let it reach the transcript (served by the API)
            # or the mirrored checkpoint_answered event (posted into Slack).
            shown_body = "[credentials provided]" if (
                cp["ctype"] == flow.NEEDS_INPUT and body) else body
            self.store.add_message(run_id, "user", f"[{action}] {shown_body or ''}".strip())
            # Tell the OTHER surface the ask is settled (close its gate / post a
            # note) before the resumed turn starts streaming.
            yield TurnEvent("checkpoint_answered",
                            {"id": int(checkpoint_id), "action": action, "body": shown_body})
            # Autonomy set at plan time must survive the resume: a Slack build
            # that paused for creds/ambiguity keeps drafting-on-bottom-out.
            auto_draft = bool(self.store.get_run(run_id).get("auto_draft"))
            if cp["ctype"] == flow.REPAIR_ESCALATION:
                low = (body or action or "").strip().lower()
                if action == "reject" or low in ("abort", "stop", "no"):
                    self.store.set_lifecycle_state(run_id, flow.IDLE)
                    self.store.set_state(run_id, "idle")
                    yield TurnEvent("done", {"message": "Stopped without pushing.",
                                             "diff_files": 0, "verify_ok": False})
                    return
                # retry: re-run the agent + repair (the worker resumes its
                # session; the user's guidance folds into the next fix).
                yield from self._execute(run_id, model, extra_guidance=body,
                                         auto_draft=auto_draft)
                return
            if cp["ctype"] == flow.NEEDS_INPUT:
                from forge.creds import parse_credentials
                low = (body or action or "").strip().lower()
                if action == "reject" or low in ("abort", "stop", "no"):
                    self.store.set_lifecycle_state(run_id, flow.IDLE)
                    self.store.set_state(run_id, "idle")
                    yield TurnEvent("done", {"message": "Stopped without pushing.",
                                             "diff_files": 0, "verify_ok": False})
                    return
                creds = parse_credentials(body or "")
                if creds:
                    slug = (self.store.get_run(run_id) or {}).get("repo")
                    if slug:
                        self.knowledge.merge_save(slug, {"qa_credentials": creds})
                        yield TurnEvent("creds_saved", {"repo": slug})
                # Resume: re-run execute; _qa now loads the saved creds and logs
                # in. (auto_draft: if the wall still blocks, _execute won't
                # re-ask — it drafts a PR. See the QA-blocked branch.)
                yield from self._execute(run_id, model, auto_draft=auto_draft)
                return
            if action == "reject":
                self.store.set_lifecycle_state(run_id, flow.IDLE)
                self.store.set_state(run_id, "idle")
                yield TurnEvent("done", {"message": "Plan rejected; standing by.",
                                         "diff_files": 0, "verify_ok": None})
                return
            if action == "edit":
                env = self._env_for(run_id)
                self.store.set_lifecycle_state(run_id, flow.PLANNING)
                yield TurnEvent("phase", {"name": "planning", "label": "Replanning"})
                prior = self.store.get_run(run_id).get("plan_json") or "{}"
                yield from self._stream_worker(
                    run_id, env, build_replan_prompt(prior, body or ""),
                    self.provider.resolve_model(model, body or ""))
                plan = self._read_plan(run_id)
                if plan is None:
                    self.store.set_lifecycle_state(run_id, flow.FAILED)
                    yield TurnEvent("error", {"kind": "plan",
                                              "detail": "replan produced no valid plan"})
                    return
                self.store.set_plan(run_id, json.dumps(plan.to_dict()))
                yield TurnEvent("plan", plan.to_dict())
                cid = self.store.create_checkpoint(run_id, flow.PLAN_APPROVAL,
                                                   {"plan": plan.to_dict()})
                self.store.set_lifecycle_state(run_id, flow.AWAITING_APPROVAL)
                self.store.set_state(run_id, "awaiting_approval")
                yield TurnEvent("checkpoint", {"id": cid, "type": flow.PLAN_APPROVAL,
                                               "prompt": "Approve the revised plan or describe more changes."})
                return
            # action == "approve" (or any answer to an ambiguity checkpoint)
            yield from self._execute(run_id, model, auto_draft=auto_draft)
        except Exception:
            log.exception("respond_checkpoint failed run=%s", run_id)
            self.store.set_lifecycle_state(run_id, flow.FAILED)
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "internal",
                                      "detail": "resuming the session failed unexpectedly"})
        finally:
            self._end_active(run_id)

    def _execute(self, run_id, model="auto", extra_guidance="", auto_draft=False):
        """Run the (approved) task agent + verify. Does NOT manage self._active —
        the caller (plan_task / respond_checkpoint) owns the guard. Phase 1 stops
        after verify; Phase 2 adds the repair loop + policy-gated push.

        auto_draft=True (fire-and-forget batch) opens a DRAFT PR on verify/QA
        bottom-out instead of a repair-escalation checkpoint — there is no human
        to escalate to, so review happens on the PR."""
        self.store.set_lifecycle_state(run_id, flow.EXECUTING)
        self.store.set_state(run_id, "running")
        self._reset_artifacts(run_id)
        self._reset_pr_meta(run_id)
        env = self._env_for(run_id)
        run = self.store.get_run(run_id)
        plan = self._read_plan(run_id)
        task = run.get("task") or (plan.goal if plan else "")
        try:
            att_names = json.loads(run.get("attachments_json") or "[]")
        except ValueError:
            att_names = []
        chosen = self.provider.resolve_model(model, task)
        yield TurnEvent("phase", {"name": "agent", "label": "Agent working"})
        result = yield from self._stream_worker(
            run_id, env, build_task_prompt(task, self._app_url(run_id),
                                           env=self._runtime_facts(run_id),
                                           attachments=self._attachment_paths(run_id, att_names),
                                           lessons=self._lessons(run_id)), chosen)
        if result is None:
            self.store.set_lifecycle_state(run_id, flow.FAILED)
            yield TurnEvent("error", {"kind": "worker", "detail": "no result event"})
            return
        if (yield from self._pause_if_requested(run_id)):   # graceful sleep boundary
            return
        plan_v = self._get_verify_plan(run_id)
        remaining = yield from self._repair(run_id, env, plan_v, model, extra_guidance=extra_guidance)
        url = self._refresh_url(env, run_id)
        if url:
            yield TurnEvent("url", {"web_url": url})
        diff_files = self._diff_file_count(env)
        base_meta = {"cost": result.total_cost_usd, "model": chosen,
                     "diff_files": diff_files}
        if remaining:
            if auto_draft:
                # Fire-and-forget: never stall on a human. Open a DRAFT PR that
                # flags the still-failing checks; review happens on the PR.
                yield from self._finalize_execute(run_id, env, result, base_meta,
                                                  verify_failed=remaining)
                return
            # Bottom-out: never push red. Surface failing checks + escalate.
            self.store.add_message(run_id, "assistant", result.result_text or "(done)",
                                   meta={**base_meta, "verify_ok": False,
                                         "verify_failed": remaining})
            cid = self.store.create_checkpoint(run_id, flow.REPAIR_ESCALATION,
                                               {"failed": remaining})
            self.store.set_lifecycle_state(run_id, flow.AWAITING_INPUT)
            self.store.set_state(run_id, "awaiting_input")
            yield TurnEvent("checkpoint", {
                "id": cid, "type": flow.REPAIR_ESCALATION,
                "prompt": ("Couldn't get these checks green within the repair "
                           f"budget: {', '.join(remaining)}. Reply with guidance to "
                           "retry, or 'abort' to stop without pushing.")})
            return
        if (yield from self._pause_if_requested(run_id)):   # graceful sleep boundary
            return
        # Acceptance QA tier: drive the live app through the plan's acceptance
        # criteria. Gated → escalate on bottom-out (never push); advisory → report.
        qa_plan = self._read_plan(run_id)
        if qa_plan and qa_plan.acceptance and self._app_url(run_id):
            if self.cfg.qa_gating:
                qa_failed = yield from self._qa_gate(run_id, env, qa_plan, model)
                qa_res = self._read_qa(run_id)
                qa_blocked = qa_res.blocked if qa_res else None
                if qa_blocked:
                    # A wall we can't legitimately cross (missing creds / 2FA /
                    # paywall). Supervised (auto_draft off): always pause and ask.
                    # Autonomous (Slack): ask for a login AT MOST ONCE — only when
                    # none is saved and we haven't asked before. If creds are
                    # already saved (and still failed) or we've asked once, never
                    # loop: draft a PR flagging the unverified UI (CI passed).
                    have_creds = bool(self._qa_credentials(run_id))
                    asked_before = self.store.count_checkpoints(run_id, flow.NEEDS_INPUT) > 0
                    if auto_draft and (have_creds or asked_before):
                        note = ("Opened as a draft — browser QA could not sign in to "
                                "verify the UI (no working login was available). CI "
                                "checks passed; please verify the UI before merging.")
                        yield from self._finalize_execute(
                            run_id, env, result,
                            {**base_meta, "qa_blocked": qa_blocked},
                            verify_failed=[], note=note)
                        return
                    self.store.add_message(
                        run_id, "assistant", result.result_text or "(done)",
                        meta={**base_meta, "verify_ok": True, "blocked": qa_blocked})
                    cid = self.store.create_checkpoint(
                        run_id, flow.NEEDS_INPUT, {"blocked": qa_blocked})
                    self.store.set_lifecycle_state(run_id, flow.AWAITING_INPUT)
                    self.store.set_state(run_id, "awaiting_input")
                    yield TurnEvent("checkpoint", {
                        "id": cid, "type": flow.NEEDS_INPUT,
                        "summary": result.result_text,
                        "prompt": (qa_blocked.get("question")
                                   or "I need credentials to finish QA — reply with "
                                   "the login to use (e.g. `user@x :: password`).")})
                    return
                if qa_failed:
                    if auto_draft:
                        # Fire-and-forget: draft a PR flagging the failing
                        # acceptance checks rather than stalling on a human.
                        yield from self._finalize_execute(
                            run_id, env, result,
                            {**base_meta, "acceptance_failed": qa_failed},
                            verify_failed=qa_failed)
                        return
                    self.store.add_message(
                        run_id, "assistant", result.result_text or "(done)",
                        meta={**base_meta, "verify_ok": True, "acceptance_failed": qa_failed})
                    cid = self.store.create_checkpoint(
                        run_id, flow.REPAIR_ESCALATION, {"failed": qa_failed, "kind": "acceptance"})
                    self.store.set_lifecycle_state(run_id, flow.AWAITING_INPUT)
                    self.store.set_state(run_id, "awaiting_input")
                    yield TurnEvent("checkpoint", {
                        "id": cid, "type": flow.REPAIR_ESCALATION,
                        "prompt": ("Couldn't get the change to pass acceptance QA — these "
                                   f"checks still fail: {', '.join(qa_failed)}. Reply with "
                                   "guidance to retry, or 'abort' to stop without pushing.")})
                    return
            else:
                yield from self._qa(run_id, env, qa_plan, model)   # advisory: report-only
        # Green → complete the task with a PR.
        yield from self._finalize_execute(run_id, env, result, base_meta, verify_failed=[])

    def _finalize_execute(self, run_id, env, result, base_meta, verify_failed,
                          note=None):
        """Commit+push+PR (draft iff verify_failed or `note`), record the assistant
        message, run the retrospective, and yield the terminal `done` (or `error`).
        Shared by the green path and the autonomous draft-on-bottom-out path."""
        self.store.set_lifecycle_state(run_id, flow.PUSHING)
        self.store.set_state(run_id, "pushing")
        pr = self._finish_pr(run_id, env, verify_failed=verify_failed, note=note)
        verify_ok = not verify_failed
        if not pr.get("ok"):
            self.store.set_lifecycle_state(run_id, flow.FAILED)
            self.store.set_state(run_id, "failed")
            self.store.add_message(run_id, "assistant", result.result_text or "(done)",
                                   meta={**base_meta, "verify_ok": verify_ok})
            yield TurnEvent("error", {"kind": "complete",
                                      "detail": pr.get("reason", "pr_failed")})
            return
        self.store.add_message(run_id, "assistant", result.result_text or "(done)",
                               meta={**base_meta, "verify_ok": verify_ok,
                                     "verify_failed": verify_failed, "pr_url": pr["pr_url"]})
        self.store.set_lifecycle_state(run_id, flow.PR_OPEN)
        self.store.set_state(run_id, "pr_open")
        self.store.touch_env(run_id)
        learned = self._retrospective(run_id)
        if learned:
            yield TurnEvent("retrospective", {"added": learned})
        yield TurnEvent("done", {"message": result.result_text,
                                 "diff_files": base_meta.get("diff_files"),
                                 "verify_ok": verify_ok, "draft": pr.get("draft"),
                                 "pr_url": pr["pr_url"]})

    # --- Task-10 methods ---

    def diff(self, run_id) -> str:
        env = self._env_for(run_id)
        r = env.exec(["bash", "-lc", "git add -A -N && git diff HEAD"], service="forge")
        return r.stdout

    def artifacts(self, run_id) -> list:
        """Visual artifacts the agent captured this turn, read straight from the
        host workspace (mounted into the container at /work, so files the agent
        wrote to .forge/artifacts/ are here). Validation lives in slackmedia."""
        from forge import slackmedia
        d = Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "artifacts"
        if not d.is_dir():
            return []
        manifest = d / "manifest.json"
        text = manifest.read_text() if manifest.is_file() else ""
        return slackmedia.parse_manifest(text, d)

    def _reset_artifacts(self, run_id):
        """Clear a prior turn's captures so we never re-post stale screenshots
        on a later turn that captured nothing of its own."""
        import shutil
        d = Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "artifacts"
        shutil.rmtree(d, ignore_errors=True)

    def _reset_pr_meta(self, run_id):
        """Clear the previous task's PR description at the start of a NEW task
        turn (not before self-review/open_pr, which must still read it) so a
        turn that writes none can't open a PR under a stale title."""
        p = Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "pr.json"
        p.unlink(missing_ok=True)

    def save_attachment(self, run_id, filename, data, mimetype=None):
        """Stage one user-supplied image for this run (see forge.inbox)."""
        return inbox.save(self.cfg.runs_dir, run_id, filename, data, mimetype=mimetype)

    def _attachment_paths(self, run_id, names):
        """Sync named inbox files into the workspace; container paths for the
        prompt. Best-effort: on any failure the turn proceeds without images."""
        if not names:
            return []
        try:
            return inbox.sync(self.cfg.runs_dir, run_id, names)
        except Exception:
            return []

    def _read_plan(self, run_id):
        """Read the plan the planner wrote to the bind-mounted workspace
        (/work/.forge/plan.json → runs/<id>/workspace/.forge/plan.json)."""
        from forge.plan import parse_plan
        p = Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "plan.json"
        return parse_plan(p.read_text()) if p.is_file() else None

    def _lessons_path(self, run_id):
        return Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "lessons.json"

    def _read_lessons(self, run_id) -> list:
        from forge.retrospective import parse_lessons
        p = self._lessons_path(run_id)
        return parse_lessons(p.read_text()) if p.is_file() else []

    def _reset_lessons(self, run_id):
        self._lessons_path(run_id).unlink(missing_ok=True)

    def _retrospective(self, run_id) -> int:
        """Best-effort: after a PR, a worker turn reflects on the run and records
        durable per-repo lessons in the knowledge overlay. Returns the count saved
        (0 when learning is off, there's no repo slug, or anything fails). Runs under
        the caller's _active guard; never commits/pushes."""
        if not self.cfg.learn:
            return 0
        slug = self._repo_slug(run_id)
        if not slug:
            return 0
        try:
            env = self._env_for(run_id)
            self._reset_lessons(run_id)
            chosen = self.provider.resolve_model("auto", "retrospective lessons")
            self._run_worker(run_id, env,
                             build_retrospective_prompt(self._lessons(run_id)),
                             chosen)
            lessons = self._read_lessons(run_id)
            if not lessons:
                return 0
            for l in lessons:
                l["added_run"] = run_id
            self.knowledge.merge_save(slug, {"lessons": lessons,
                                             "provenance": {"learned_by": "retrospective"}})
            return len(lessons)
        except Exception:
            return 0

    def _qa_path(self, run_id):
        return Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "qa.json"

    def _read_qa(self, run_id):
        from forge.qa import parse_qa
        p = self._qa_path(run_id)
        return parse_qa(p.read_text()) if p.is_file() else None

    def _reset_qa(self, run_id):
        p = self._qa_path(run_id)
        p.unlink(missing_ok=True)

    def _qa_credentials(self, run_id):
        """Load this repo's stored browser-QA credentials (or None)."""
        slug = (self.store.get_run(run_id) or {}).get("repo")
        overlay = self.knowledge.load(slug) if slug else None
        return (overlay or {}).get("qa_credentials")

    def remember_lesson(self, slug, text) -> bool:
        """Store a user-taught, durable lesson for a repo (Slack 'remember: …').
        User lessons are injected into every future plan/execute prompt on this
        repo and survive the lessons cap (never evicted by auto-learned ones)."""
        text = " ".join((text or "").split())
        if not slug or not text:
            return False
        self.knowledge.merge_save(slug, {
            "lessons": [{"text": text, "kind": "user",
                         "evidence": "taught by the user"}],
            "provenance": {"learned_by": "user"}})
        return True

    def forget_credentials(self, slug) -> bool:
        """Remove this repo's stored browser-QA credentials. Returns True if any
        were present and cleared, False otherwise."""
        overlay = self.knowledge.load(slug) if slug else None
        if not overlay or "qa_credentials" not in overlay:
            return False
        overlay.pop("qa_credentials")
        self.knowledge.save(slug, overlay)
        return True

    def _qa_gate(self, run_id, env, plan, model="auto"):
        """Gated acceptance QA: run _qa, then up to max_repair_iters rounds of
        {QA-fix turn → re-verify CI (_repair) → re-_qa}. RETURNS remaining
        acceptance failures ([] = all pass). If a QA fix regresses CI past its
        repair budget, returns the CI failure names (bottom-out). No _active, no push."""
        from forge.prompts import build_qa_fix_prompt
        failed = yield from self._qa(run_id, env, plan, model)
        it = 0
        while failed and it < self.cfg.budget.max_repair_iters:
            qa = self._read_qa(run_id)
            if qa and qa.blocked:
                break                           # needs a human → stop, don't "fix"
            it += 1
            chosen = self.provider.resolve_model(model, "fix acceptance failures")
            self._run_worker(run_id, env,
                             build_qa_fix_prompt(failed, self._app_url(run_id)),
                             chosen)
            ci = yield from self._repair(run_id, env, self._get_verify_plan(run_id), model)
            if ci:
                return ci                       # a QA fix broke CI → bottom-out
            failed = yield from self._qa(run_id, env, plan, model)
        return failed

    def _qa(self, run_id, env, plan, model="auto"):
        """One browser-QA turn: drive the live app through plan.acceptance, read
        .forge/qa.json. Injects this repo's stored credentials (if any) into the
        prompt and redacts them out of narration/tool output. Yields
        narration/tool + a `qa` event; RETURNS failing criteria ([] = all pass OR
        no/invalid qa.json — inconclusive never gates). The `qa` event's
        `blocked` flag / on-disk qa.json signals when a human is needed.
        Never touches self._active; never commits."""
        from forge.prompts import build_qa_prompt
        from forge.creds import redact_secrets
        self._reset_qa(run_id)
        self.store.set_lifecycle_state(run_id, flow.VERIFYING)
        self.store.set_state(run_id, "qa")
        chosen = self.provider.resolve_model(model, "browser acceptance qa")
        creds = self._qa_credentials(run_id)
        secrets = [c.get("password") for c in (creds or []) if c.get("password")]
        yield TurnEvent("phase", {"name": "qa", "label": "Browser QA"})
        yield from self._stream_worker(
            run_id, env, build_qa_prompt(list(plan.acceptance),
                                         self._app_url(run_id), credentials=creds),
            chosen, redact=lambda s: redact_secrets(s, secrets))
        qa = self._read_qa(run_id)
        failed = qa.failures if qa else []
        yield TurnEvent("qa", {"checked": qa.checked if qa else 0,
                               "failed": failed,
                               "summary": qa.summary if qa else "",
                               "blocked": bool(qa and qa.blocked)})
        return failed

    def can_start(self):
        n = len(self.store.list_envs(states=("live", "starting")))
        if n >= self.cfg.max_live_sessions:
            return (False, f"max_live_sessions reached ({n}); end an idle session first")
        return (True, "")

    def admit_count(self) -> int:
        """How many new queued runs the scheduler may dispatch right now.
        max_live_sessions is the hard ceiling; a memory budget can cap lower.
        Counts occupied slots deduped by run_id (a live worker mid-execute has
        both a 'running' run row and a 'live' env — one slot, not two)."""
        cap = self.cfg.max_live_sessions
        per = parse_mem_mb(self.cfg.web_mem_limit)
        if self.cfg.mem_budget_mb and per:
            cap = min(cap, self.cfg.mem_budget_mb // per)
        occupied = {e["run_id"]
                    for e in self.store.list_envs(states=("starting", "live"))}
        occupied |= {r["run_id"] for r in self.store.list_runs(states=("running",))}
        return max(0, cap - len(occupied))

    # --- fire-and-forget batch queue ---

    def set_event_sink(self, run_id, fn) -> None:
        """Register a per-run progress sink (e.g. a Slack thread renderer) that
        the scheduler passes to run_autonomous when this run is dispatched."""
        self._event_sinks[run_id] = fn

    def _pop_sink(self, run_id):
        return self._event_sinks.pop(run_id, None)

    def enqueue_batch(self, items) -> tuple:
        """Create one queued run per item and return (batch_id, run_ids). No
        can_start / no 409 — over-capacity items simply wait in 'queued'. Shared
        by POST /api/batch and the Slack list path. Each item:
        {repo, task, model?, source?} (defaults model='auto', source='github')."""
        batch_id = uuid.uuid4().hex
        run_ids = []
        for it in items:
            run_id = uuid.uuid4().hex
            repo = it["repo"]
            source = it.get("source") or "github"
            self.store.create_run(run_id, repo, it.get("task") or "", "")
            self.store.set_queue_fields(run_id, model=it.get("model") or "auto",
                                        batch_id=batch_id)
            self.store.set_session_fields(run_id, repo_source=f"{source}:{repo}")
            run_ids.append(run_id)
        return batch_id, run_ids

    def _release_env(self, run_id) -> None:
        """Free a batched run's container + Supabase block so its admission slot
        reopens — WITHOUT touching runs.state (keeps the terminal done/failed for
        the fleet). reap_project marks the env 'reaped'. Best-effort."""
        try:
            self._release_supabase(run_id)
        except Exception:
            pass
        try:
            lifecycle.reap_project(self.store, run_id)
        except Exception:
            pass

    def run_autonomous(self, run_id, on_event=None) -> None:
        """Drive a claimed (queued→running) run to completion, fully autonomous:
        _boot (provision) → plan_task with autonomous=True (no gate; draft-PR on
        verify/QA bottom-out). Forwards each TurnEvent to on_event (best-effort —
        a sink error never aborts the run). Frees the env in a finally so the
        admission slot reopens regardless of outcome."""
        def emit(ev):
            if on_event is None:
                return
            try:
                on_event(ev)
            except Exception:
                pass
        row = self.store.get_run(run_id) or {}
        repo = row.get("repo") or ""
        task = row.get("task") or ""
        model = row.get("model") or "auto"
        source = (row.get("repo_source") or "github:").split(":", 1)[0] or "github"
        saw_done = False
        last_error = None
        try:
            for ev in self._boot(run_id, repo, source):
                # _boot is an internal (undecorated) flow — publish here so the
                # web feed / mirror see queued provisions too.
                try:
                    self.bus.publish(run_id, ev, origin="queue")
                except Exception:
                    pass
                emit(ev)
                if ev.kind == "error":
                    last_error = ev.data
            if last_error is None and self.store.get_run(run_id).get("state") != "failed":
                for ev in self.plan_task(
                        run_id, task, model,
                        policy=flow.CheckpointPolicy.for_cli(auto=True),
                        autonomous=True, origin="queue"):
                    emit(ev)
                    if ev.kind == "done":
                        saw_done = True
                    elif ev.kind == "error":
                        last_error = ev.data
            if saw_done:
                self.store.set_state(run_id, "done")       # normalize pr_open → done
            else:
                self.store.set_state(run_id, "failed")
                detail = (last_error or {}).get("detail") if last_error \
                    else "did not complete autonomously"
                self.store.set_queue_error(run_id, (detail or "failed")[:500])
        except Exception as e:
            self.store.set_state(run_id, "failed")
            self.store.set_queue_error(run_id, str(e)[:500])
            emit(TurnEvent("error", {"kind": "autonomous", "detail": str(e)[:300]}))
        finally:
            self._release_env(run_id)

    def open_pr(self, run_id) -> dict:
        # open_pr runs a worker (self-review) + commits in the run's container.
        # The Slack "Open PR" button calls this OUTSIDE the per-thread turn queue,
        # so guard against a concurrent turn() (which holds _active) the same way
        # turn()/wake() do — two workers in one workspace corrupt the diff/commit.
        if not self._try_begin(run_id):
            return {"ok": False, "reason": "busy"}
        try:
            env = self._env_for(run_id)
            if env.exec(cmd.has_changes_cmd(), service="forge").stdout.strip() == "":
                return {"ok": False, "reason": "no_changes"}
            self._self_review_and_fix(run_id)          # quality review + fix
            plan = self._get_verify_plan(run_id)
            verify_failed = _drain(self._repair(run_id, env, plan))
            pr = self._finish_pr(run_id, env, verify_failed)
            if pr.get("ok"):
                self._retrospective(run_id)        # best-effort; not streamed here
            return pr
        finally:
            self._end_active(run_id)

