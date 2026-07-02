"""Sleep / wake / delete lifecycle of a session (mixin).

Warm-sleep stops the compose stack but keeps containers, volumes, and the
Supabase reservation so wake restarts in seconds; delete archives the branch
first and only then removes the workspace. Split from session.py for
readability only — at runtime these are ordinary SessionManager methods."""
import shutil
from pathlib import Path

from forge import lifecycle
from forge import nextdev
from forge.eventbus import published
from forge.events import TurnEvent


class LifecycleOps:
    def _warm_sleep(self, run_id, reason=None) -> None:
        """Warm-sleep body (no _active check): STOP the compose stack (keep
        containers + volumes + workspace) and pause Supabase keeping its
        reservation, so wake can START in seconds. Records a lockfile signature
        so wake knows the warm node_modules is valid. `reason` is persisted so
        the Slack lifecycle notice can label the sleep (or skip it when the
        initiating surface already announced it)."""
        self._env_for(run_id).stop()            # keep containers + named volumes
        self._pause_supabase(run_id)            # stop Supabase, KEEP the reservation
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        self.store.set_snapshot_lockhash(run_id, self._lockfile_hash(ws))
        self.store.mark_asleep(run_id, reason=reason)

    def sleep(self, run_id, reason="manual") -> bool:
        """Warm-sleep now. Refuses while a turn is in flight (use request_sleep
        to pause gracefully at the next phase boundary instead)."""
        if run_id in self._active:
            return False
        self._warm_sleep(run_id, reason)
        return True

    def request_sleep(self, run_id, reason="manual") -> str:
        """Idle → sleep now ('sleeping'); mid-turn → defer to the next phase
        boundary ('deferred', consumed by _pause_if_requested inside _execute)."""
        if run_id in self._active:
            self._sleep_requested[run_id] = reason
            return "deferred"
        return "sleeping" if self.sleep(run_id, reason) else "deferred"

    def _pause_if_requested(self, run_id):
        """At a phase boundary: if a deferred sleep is pending, warm-sleep and
        yield a `slept` event. RETURNS True when it paused (caller should stop)."""
        if run_id not in self._sleep_requested:
            return False
        reason = self._sleep_requested.pop(run_id)
        self._warm_sleep(run_id, reason)
        yield TurnEvent("slept", {"message": "💤 Paused — reply here to wake."})
        return True

    @published
    def wake(self, run_id, fresh=False):
        """Bring a slept session back. Warm-wakes (compose start) when the
        snapshot is still valid; otherwise (deps changed, --fresh, or warm start
        unhealthy) falls back to a full cold provision. Generator of TurnEvents."""
        if run_id in self._waking or run_id in self._active:
            yield TurnEvent("error", {"kind": "busy", "detail": "already waking or busy"})
            return
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        if not Path(ws).is_dir():
            self.store.mark_deleted(run_id, reason="gone")  # workspace gone — tombstone
            yield TurnEvent("error", {"kind": "gone",
                                      "detail": "workspace deleted; cannot wake"})
            return
        self._waking.add(run_id)
        try:
            warm = (not fresh) and self._warm_eligible(run_id, ws)
            yield TurnEvent("phase", {"name": "wake",
                                      "label": "Waking (warm)" if warm else "Waking"})
            yield from self._provision(run_id, ws, warm=warm)
            if warm and self.store.get_env(run_id).get("state") == "failed":
                # Warm start didn't come up healthy — clear it and cold-provision once.
                yield TurnEvent("phase", {"name": "wake", "label": "Cold retry"})
                self._env_for(run_id).down()
                yield from self._provision(run_id, ws, warm=False)
        finally:
            self._waking.discard(run_id)

    def _archive_code(self, run_id) -> bool:
        """Commit + push the session branch from the host workspace so the code
        survives deletion. Returns True only if the push succeeds. Never raises
        — a failure just means 'keep the workspace and retry next sweep'.

        Auth mirrors the worker: `gh auth setup-git` wires git's credential
        helper to gh, which reads GH_TOKEN from the subprocess env — no token in
        argv. The infra config.toml stays out of the commit (skip-worktree)."""
        run = self.store.get_run(run_id)
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        branch = run.get("branch")
        if not branch or not Path(ws, ".git").is_dir():
            return False
        name, email = self._commit_identity(run_id)
        token = {"GH_TOKEN": self.cfg.gh_token}
        try:   # archive the agent's work, not forge's runtime origin patch
            nextdev.unpatch_for_commit(self.host, ws)
        except Exception:
            pass
        self.host.run(["git", "-C", ws, "config", "user.name", name])
        self.host.run(["git", "-C", ws, "config", "user.email", email])
        self.host.run(["git", "-C", ws, "add", "-A"])
        # commit may be a no-op (nothing to commit) — push still publishes HEAD.
        msg = f"forge: archive {run_id} before cleanup"
        trailer = self._commit_trailer(run_id)
        if trailer:
            msg += f"\n\n{trailer}"
        self.host.run(["git", "-C", ws, "commit", "-m", msg])
        self.host.run(["gh", "auth", "setup-git"], env=token)
        push = self.host.run(["git", "-C", ws, "push", "-u", "origin", branch],
                             env=token)
        return push.exit_code == 0

    def delete_dormant(self, run_id) -> bool:
        """Archive (commit+push) then delete a dormant session's workspace.
        Tears down the warm (stopped) compose stack so it doesn't leak containers
        + volumes. Returns True if deleted; False if the archive failed."""
        if not self._archive_code(run_id):
            return False
        self._env_for(run_id).down()            # down -v the warm stopped stack
        self._release_supabase(run_id)          # stop Supabase + free the port block
        ws = Path(self.cfg.runs_dir) / run_id / "workspace"
        if ws.is_dir():
            shutil.rmtree(ws, ignore_errors=True)
        self.store.mark_deleted(run_id, reason="dormant")
        return True

    def _pause_supabase(self, run_id) -> None:
        """Warm-sleep: stop the per-run host Supabase but KEEP its port reservation +
        on-disk DB so wake reattaches the same instance. (Contrast _release_supabase,
        which also frees the block — used on end/dormant-delete/failure.)"""
        if self.store.get_supabase(run_id):
            ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
            self.host.run(["supabase", "stop", "--workdir", ws])

    def _release_supabase(self, run_id) -> None:
        # Stop the per-run host Supabase (if any) and free its port block. The
        # next-supabase recipe starts Supabase in host_pre, so every teardown
        # path — explicit end AND provisioning failure — must call this or the
        # host Supabase stack is orphaned (keeps running in Docker after the
        # session is gone / failed).
        if self.store.get_supabase(run_id):
            ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
            self.host.run(["supabase", "stop", "--workdir", ws])
            self.alloc.release(run_id)

    def end(self, run_id, reason="manual") -> None:
        self._release_supabase(run_id)
        lifecycle.reap_project(self.store, run_id)
        self.store.mark_deleted(run_id, reason=reason)

    def reconcile(self, ps_checker=None) -> None:
        import subprocess

        def _default(project):
            r = subprocess.run(["docker", "compose", "-p", project, "ps", "-q"],
                               capture_output=True, text=True)
            return bool(r.stdout.strip())

        check = ps_checker or _default
        for e in self.store.list_envs(states=("live", "starting", "running")):
            if not check(f"forge-{e['run_id']}"):
                # Container gone (e.g. after a forge restart) → sleep it so the
                # user can wake it, rather than killing it as failed.
                self.store.mark_asleep(e["run_id"], reason="restart")
        # Release (and stop) Supabase blocks whose run is no longer active.
        active = {e["run_id"] for e in
                  self.store.list_envs(states=("live", "starting", "running"))}
        for rid in self.alloc.reconcile(active):
            ws = str(Path(self.cfg.runs_dir) / rid / "workspace")
            self.host.run(["supabase", "stop", "--workdir", ws])

    def stop(self, run_id) -> None:
        # Cancel the env currently streaming for this run (recorded by _env_for),
        # NOT a fresh env — a fresh one has no live _proc, so cancel would no-op.
        env = self._envs.get(run_id)
        if env is not None:
            env.cancel()
        self._end_active(run_id)
