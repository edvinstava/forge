import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from forge import commands as cmd
from forge import envreg, lifecycle
from forge.budget import BudgetTracker
from forge.health import health_poll_argv
from forge.plan import parse_plan
from forge.probing import build_probe
from forge.prompts import build_fix_prompt, build_plan_prompt, build_task_prompt
from forge.recipe import (SUPABASE_LOCAL_ANON_KEY,
                          SUPABASE_LOCAL_SERVICE_ROLE_KEY, Probe,
                          apply_resource_limits, resolve)
from forge.rundir import RunDir
from forge.runspec import make_runspec
from forge.verify import parse_verify
from forge.worker import parse_worker_result
from forge.hostops import exclude_forge_scratch


@dataclass(frozen=True)
class RunOutcome:
    state: str
    pr_url: str | None
    draft: bool
    reason: str | None
    web_url: str | None = None


def default_env_factory(run_id, files):
    from forge.composeenv import ComposeEnv
    return ComposeEnv(run_id, files)


class ComposeOrchestrator:
    """Provision a per-run Docker Compose stack for the repo, run the worker as
    a service inside it, verify, open a PR, and keep the env warm so its web URL
    is live. `host` and `env_factory` are injected for testability."""

    def __init__(self, config, store, host, env_factory=default_env_factory,
                 clock=time.monotonic):
        self.cfg = config
        self.store = store
        self.host = host
        self.env_factory = env_factory
        self.clock = clock

    def _probe(self, ws) -> Probe:
        return build_probe(self.host, ws)

    def _gh_env(self, repo_slug) -> dict:
        """Per-exec GitHub token for forge's own git/gh execs — repo-scoped App
        token when configured, else the PAT. The container env never holds it."""
        from forge import ghapp
        return {"GH_TOKEN": ghapp.worker_token(self.cfg, repo_slug)}

    def run(self, repo, task, run_id, policy=None, approve=None) -> RunOutcome:
        from forge import flow
        policy = policy or flow.CheckpointPolicy.for_cli(auto=True)
        rs = make_runspec(repo, task, run_id)
        self.store.create_run(run_id, repo, task, rs.branch)
        rd = RunDir.for_run(self.cfg.runs_dir, run_id)
        rd.write("meta.json", json.dumps(
            {"run_id": run_id, "repo": repo, "task": task, "branch": rs.branch}, indent=2))
        rd.timeline(f"Run created — {repo} · {task}")

        reaped = lifecycle.reap_superseded_projects(self.store, run_id)  # concurrency 1
        if reaped:
            rd.timeline(f"Reaped superseded env(s): {', '.join(reaped)}")

        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        self.store.set_state(run_id, "provisioning")
        cl = self.host.clone(repo, rs.branch, ws, self.cfg.gh_token)
        if cl.exit_code != 0:
            self.store.set_state(run_id, "failed")
            rd.timeline(f"Clone FAILED: {(cl.stderr or cl.stdout).strip()[:200]}")
            return RunOutcome("failed", None, False, "clone_failed", None)
        exclude_forge_scratch(self.host, ws)
        rd.timeline(f"Cloned · branch {rs.branch}")

        probe = self._probe(ws)
        seed_dir = str(Path(self.cfg.runs_dir) / "cache" / "dhis2-seed")
        recipe = resolve(probe, ws, self.cfg.image_tag, seed_dir=seed_dir)
        # Cap the dev server's memory, same as SessionManager._cap on the
        # web/Slack path — a leaky `next dev` must not be able to starve the host
        # just because the run came in via the `forge run` CLI.
        recipe = apply_resource_limits(
            recipe, mem_limit=self.cfg.web_mem_limit,
            node_max_old_space_mb=self.cfg.web_node_max_old_space_mb)
        rd.timeline(f"Recipe: {recipe.name}")
        self.store.add_event(run_id, "recipe", {"name": recipe.name,
                                                "web": recipe.web_service})

        files = []
        if recipe.compose is not None:
            cf = Path(self.cfg.runs_dir) / run_id / "forge-compose.yml"
            self.host.write_file(str(cf), json.dumps(recipe.compose, indent=2))
            files.append(str(cf))

        verify_plan = parse_verify(probe.package_json, probe.repo_yml,
                                   self.host.exists(ws, ".forge/verify.sh"),
                                   probe.pkg_manager)
        self.store.add_event(run_id, "verify_plan",
                             {"real": verify_plan.has_real_verification,
                              "cmds": [c.name for c in verify_plan.commands]})

        # GH_TOKEN stays EMPTY in the container env (the worker runs untrusted
        # repo code); forge's own git/gh execs get a token per exec (_gh_env).
        secrets = {"CLAUDE_CODE_OAUTH_TOKEN": self.cfg.oauth_token,
                   "GH_TOKEN": "",
                   "FORGE_SUPABASE_ANON_KEY": SUPABASE_LOCAL_ANON_KEY,
                   "FORGE_SUPABASE_SERVICE_ROLE_KEY":
                       SUPABASE_LOCAL_SERVICE_ROLE_KEY}

        for hc in recipe.host_pre:   # e.g. supabase start (best-effort)
            r = self.host.run(hc)
            rd.timeline(f"host_pre {' '.join(hc[:2])} → exit {r.exit_code}")

        env = self.env_factory(run_id, files)
        self.store.create_env(run_id, f"forge-{run_id}", None, recipe.web_port,
                              "starting", web_service=recipe.web_service)
        try:
            env.up(secrets)
            self.store.set_state(run_id, "running")
            env.exec(cmd.setup_git_cmd(), service="forge",  # authenticated push
                     env=self._gh_env(repo))

            for svc, argv in recipe.seed:   # e.g. DHIS2 Route bootstrap
                env.exec(argv, service=svc)

            app_url = (f"http://{recipe.web_service}:{recipe.web_port}"
                       if recipe.web_service else None)

            sid = None
            if policy.gates(flow.PLAN_APPROVAL) and approve is not None:
                pres = env.exec(cmd.worker_cmd(build_plan_prompt(task, app_url), None, None),
                                service="forge")
                psid = parse_worker_result(pres.stdout).session_id or None
                pj = Path(self.cfg.runs_dir) / run_id / "workspace" / ".forge" / "plan.json"
                plan = parse_plan(pj.read_text()) if pj.is_file() else None
                rd.timeline("Planned" if plan else "Plan: none produced")
                if not approve(plan.to_dict() if plan else {"task": task}):
                    lifecycle.reap_project(self.store, run_id)
                    return RunOutcome("stopped_plan", None, False, "plan_rejected", None)
                sid = psid          # carry the planner's session into the build loop

            bt = BudgetTracker(self.cfg.budget, self.clock)
            bt.start()
            prompt = build_task_prompt(task, app_url)
            stop_reason = None
            while True:
                res = env.exec(cmd.worker_cmd(prompt, None, sid), service="forge")
                wr = parse_worker_result(res.stdout)
                sid = wr.session_id or sid
                with rd.path("agent.log").open("a") as f:
                    f.write(res.stdout + "\n")
                rd.timeline(f"Worker turn (cost≈${wr.total_cost_usd}) — {wr.session_id}")
                if wr.auth_error:
                    stop_reason = "usage"
                    break
                self.store.set_state(run_id, "verifying")
                if not verify_plan.has_real_verification:
                    break
                if verify_plan.format_fix:   # deterministic style fix first
                    env.exec(verify_plan.format_fix.argv, service="forge")
                failures = self._run_verify(env, verify_plan)
                if not failures:
                    rd.timeline("Verify: PASSED")
                    break
                rd.timeline(f"Verify: FAILED ({', '.join(n for n, _ in failures)})")
                bt.tick()
                stop_reason = bt.stop_reason()
                if stop_reason:
                    break
                prompt = build_fix_prompt(failures)

            web_url = self._register(env, run_id, rd, recipe)
            return self._finalize(env, run_id, rs, rd, verify_plan, stop_reason, web_url)
        except Exception:
            lifecycle.reap_project(self.store, run_id)
            for hc in recipe.host_post:
                self.host.run(hc)
            raise

    def _run_verify(self, env, plan):
        failures = []
        for c in plan.commands:
            res = env.exec(c.argv, service="forge")
            if res.exit_code != 0:
                failures.append((c.name, (res.stdout + res.stderr)[-2000:]))
        return failures

    def _register(self, env, run_id, rd, recipe):
        if not recipe.web_service:
            return None
        h = env.exec(
            health_poll_argv(recipe.web_port, recipe.health_path,
                             self.cfg.health_timeout_secs, host=recipe.web_service),
            service="forge")
        if h.exit_code != 0:
            self.store.set_env_state(run_id, "failed")
            rd.timeline("App did not become healthy — no URL")
            return None
        hp = env.port(recipe.web_service, recipe.web_port)
        url = envreg.web_url(hp) if hp else None
        self.store.set_env_state(run_id, "live", url)
        rd.timeline(f"App live → {url}")
        return url

    def _finalize(self, env, run_id, rs, rd, plan, stop_reason, web_url):
        self.store.set_state(run_id, "finalizing")
        if env.exec(cmd.has_changes_cmd(), service="forge").stdout.strip() == "":
            self.store.set_state(run_id, "failed")
            rd.timeline("No changes produced — nothing to PR")
            return RunOutcome("failed", None, False, "no_changes", web_url)

        name = self.cfg.git_author_name or "Forge User"
        email = self.cfg.git_author_email or "forge@local"
        for cc in cmd.commit_cmds(f"forge: {rs.task}", name, email):
            env.exec(cc, service="forge")

        gh = self._gh_env(rs.repo)     # one token for the push + PR-create pair
        push = env.exec(cmd.push_cmd(rs.branch), service="forge", env=gh)
        push_ok = push.exit_code == 0
        if not push_ok:
            rd.timeline(f"Push FAILED: {(push.stderr or push.stdout).strip()[:200]}")

        draft = (not plan.has_real_verification) or (stop_reason is not None)
        reason = "no_verification" if not plan.has_real_verification else stop_reason

        body = f"# {rs.task}\n\nrun: {run_id}\nbranch: {rs.branch}\n"
        if web_url:
            body += f"\nLive app (while warm): {web_url}\n"
        rd.write("report.md", body)
        env.exec(["bash", "-lc", f"printf '%s' {shlex.quote(body)} > /work/report.md"],
                 service="forge")

        pr_url = None
        if push_ok:
            pr = env.exec(cmd.pr_create_cmd(f"forge: {rs.task}", "/work/report.md", draft),
                          service="forge", env=gh)
            lines = pr.stdout.strip().splitlines()
            pr_url = lines[-1] if (pr.exit_code == 0 and lines) else None
            if pr_url is None:
                rd.timeline(f"PR create FAILED: {(pr.stderr or pr.stdout).strip()[:200]}")

        state = ("stopped_budget" if stop_reason in ("iterations", "wall_clock", "usage")
                 else "done")
        if pr_url is None:
            state = "failed"
            reason = "push_failed" if not push_ok else "pr_failed"

        self.store.set_state(run_id, state, pr_url=pr_url)
        rd.timeline(f"{'PR opened' if pr_url else 'PR NOT opened'}"
                    f"{' (draft)' if (draft and pr_url) else ''} → {pr_url}")
        rd.write("result.json", json.dumps(
            {"state": state, "pr_url": pr_url, "draft": draft, "reason": reason,
             "web_url": web_url}, indent=2))
        return RunOutcome(state, pr_url, draft, reason, web_url)
