import json
import shlex
import time
from dataclasses import dataclass

from forge import commands as cmd
from forge import envreg, lifecycle
from forge.appserver import detect_appserver
from forge.budget import BudgetTracker
from forge.config import Config
from forge.container import ContainerRunner
from forge.health import health_poll_argv
from forge.prompts import build_fix_prompt, build_task_prompt
from forge.rundir import RunDir
from forge.runspec import make_runspec
from forge.store import Store
from forge.verify import parse_verify
from forge.worker import parse_worker_result


@dataclass(frozen=True)
class RunOutcome:
    state: str
    pr_url: str | None
    draft: bool
    reason: str | None
    web_url: str | None = None


class Orchestrator:
    def __init__(self, config: Config, store: Store, runner: ContainerRunner,
                 clock=time.monotonic):
        self.cfg = config
        self.store = store
        self.runner = runner
        self.clock = clock

    def _cat(self, cid, path):
        r = self.runner.exec(cid, ["cat", path])
        return r.stdout if r.exit_code == 0 else None

    def _run_verify(self, cid, plan):
        failures = []
        for c in plan.commands:
            res = self.runner.exec(cid, c.argv)
            if res.exit_code != 0:
                tail = (res.stdout + res.stderr)[-2000:]
                failures.append((c.name, tail))
        return failures

    def run(self, repo: str, task: str, run_id: str) -> RunOutcome:
        rs = make_runspec(repo, task, run_id)
        self.store.create_run(run_id, repo, task, rs.branch)
        rd = RunDir.for_run(self.cfg.runs_dir, run_id)
        rd.write("meta.json", json.dumps({"run_id": run_id, "repo": repo, "task": task, "branch": rs.branch}, indent=2))
        rd.timeline(f"Run created — {repo} · {task}")

        # Concurrency 1: a new run reclaims resources from any prior live env.
        reaped = lifecycle.reap_superseded(self.runner, self.store, run_id)
        if reaped:
            rd.timeline(f"Reaped superseded env(s): {', '.join(reaped)}")

        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": self.cfg.oauth_token,
            "GH_TOKEN": self.cfg.gh_token,
        }
        cid = self.runner.start(run_id, env, publish_port=self.cfg.web_port)
        self.store.create_env(run_id, f"forge-{run_id}", None, self.cfg.web_port, "starting")
        try:
            self.store.set_state(run_id, "provisioning")
            self.runner.exec(cid, cmd.clone_cmd(repo))
            self.runner.exec(cid, cmd.branch_cmd(rs.branch))
            self.runner.exec(cid, cmd.setup_git_cmd())  # enable authenticated git push
            rd.timeline(f"Cloned · branch {rs.branch}")

            package_json = self._cat(cid, "package.json")
            repo_yml = self._cat(cid, ".forge/repo.yml")
            plan = parse_verify(
                package_json, repo_yml,
                self.runner.exec(cid, ["test", "-f", ".forge/verify.sh"]).exit_code == 0,
            )
            self.store.add_event(run_id, "verify_plan",
                                 {"real": plan.has_real_verification,
                                  "cmds": [c.name for c in plan.commands]})

            # Bring the live app up (if this repo has a web/dev server) so the
            # worker can reproduce against it and the fix is running at the URL.
            app = detect_appserver(repo_yml, package_json, self.cfg.web_port)
            app_url = None
            if app.ok:
                self.runner.exec(cid, ["sh", "-lc", "npm install"])  # best-effort deps
                self.runner.exec_detached(cid, app.start_argv)       # dev server (HMR-resilient)
                app_url = f"http://localhost:{self.cfg.web_port}"
                rd.timeline(f"App starting on :{self.cfg.web_port}")

            bt = BudgetTracker(self.cfg.budget, self.clock)
            bt.start()
            self.store.set_state(run_id, "running")
            prompt = build_task_prompt(task, app_url)
            sid = None
            stop_reason = None

            while True:
                res = self.runner.exec(cid, cmd.worker_cmd(prompt, None, sid))
                wr = parse_worker_result(res.stdout)
                sid = wr.session_id or sid   # resume the same session across fixes
                with rd.path("agent.log").open("a") as f:
                    f.write(res.stdout + "\n")
                rd.timeline(f"Worker turn (cost≈${wr.total_cost_usd}) — {wr.session_id}")
                if wr.auth_error:
                    stop_reason = "usage"
                    break

                self.store.set_state(run_id, "verifying")
                if not plan.has_real_verification:
                    break  # nothing to gate on → draft PR below

                if plan.format_fix:   # deterministic style fix before checking
                    self.runner.exec(cid, plan.format_fix.argv)
                failures = self._run_verify(cid, plan)
                if not failures:
                    rd.timeline("Verify: PASSED")
                    break
                rd.timeline(f"Verify: FAILED ({', '.join(n for n, _ in failures)})")

                bt.tick()
                stop_reason = bt.stop_reason()
                if stop_reason:
                    break
                prompt = build_fix_prompt(failures)

            web_url = self._register_app(cid, run_id, rd, app)
            return self._finalize(cid, run_id, rs, rd, plan, stop_reason, web_url)
        except Exception:
            # Never leak a container on a hard error; warm-keep is only for success.
            lifecycle.reap_env(self.runner, self.store, run_id)
            raise
        # NB: no finally-stop — on success the container stays warm so the URL
        # is live for inspection. Reaping happens on the next run or `forge down`.

    def _register_app(self, cid, run_id, rd, app) -> str | None:
        """Health-gate the live app and register its host URL. Returns the URL
        or None (the run still proceeds to a PR either way)."""
        if not app.ok:
            return None
        h = self.runner.exec(
            cid, health_poll_argv(self.cfg.web_port, app.health_path,
                                  self.cfg.health_timeout_secs))
        if h.exit_code != 0:
            self.store.set_env_state(run_id, "failed")
            rd.timeline("App did not become healthy — no URL")
            return None
        hp = self.runner.port(cid, self.cfg.web_port)
        url = envreg.web_url(hp) if hp else None
        self.store.set_env_state(run_id, "live", url)
        rd.timeline(f"App live → {url}")
        return url

    def _finalize(self, cid, run_id, rs, rd, plan, stop_reason, web_url) -> RunOutcome:
        self.store.set_state(run_id, "finalizing")
        if self.runner.exec(cid, cmd.has_changes_cmd()).stdout.strip() == "":
            self.store.set_state(run_id, "failed")
            rd.timeline("No changes produced — nothing to PR")
            return RunOutcome("failed", None, False, "no_changes", web_url)

        name = self.cfg.git_author_name or "Forge User"
        email = self.cfg.git_author_email or "forge@local"
        for cc in cmd.commit_cmds(f"forge: {rs.task}", name, email):
            self.runner.exec(cid, cc)

        push = self.runner.exec(cid, cmd.push_cmd(rs.branch))
        push_ok = push.exit_code == 0
        if not push_ok:
            rd.timeline(f"Push FAILED (exit {push.exit_code}): "
                        f"{(push.stderr or push.stdout).strip()[:300]}")

        draft = (not plan.has_real_verification) or (stop_reason is not None)
        reason = "no_verification" if not plan.has_real_verification else stop_reason

        body = f"# {rs.task}\n\nrun: {run_id}\nbranch: {rs.branch}\n"
        if web_url:
            body += f"\nLive app (while warm): {web_url}\n"
        rd.write("report.md", body)
        # Write report.md into the container so gh pr create --body-file can find it
        self.runner.exec(
            cid,
            ["bash", "-lc", f"printf '%s' {shlex.quote(body)} > /work/report.md"],
        )

        pr_url = None
        if push_ok:
            pr = self.runner.exec(
                cid, cmd.pr_create_cmd(f"forge: {rs.task}", "/work/report.md", draft))
            lines = pr.stdout.strip().splitlines()
            pr_url = lines[-1] if (pr.exit_code == 0 and lines) else None
            if pr_url is None:
                rd.timeline(f"PR create FAILED (exit {pr.exit_code}): "
                            f"{(pr.stderr or pr.stdout).strip()[:300]}")

        if stop_reason in ("iterations", "wall_clock", "usage"):
            state = "stopped_budget"
        else:
            state = "done"
        # No PR delivered ⇒ the run did not succeed, regardless of verify result.
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
