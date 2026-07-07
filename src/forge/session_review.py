"""PR-review capability of the SessionManager (mixin).

Provisions a full env on an existing PR's branch, runs a review worker, and
posts the findings as forge[bot] (or branded under the user token when no
GitHub App is configured). Split from session.py for readability only — at
runtime these are ordinary SessionManager methods."""
import json
from pathlib import Path

from forge import commands as cmd
from forge import review as reviewlib
from forge.eventbus import published
from forge.events import TurnEvent
from forge.hostops import exclude_forge_scratch
from forge.prompts import build_review_prompt
from forge.prref import parse_pr_ref

_DEGRADE_HEADER = "🔨 **Forge Review**\n\n"


class ReviewOps:
    @published
    def review(self, run_id, pr_ref, model="auto"):
        """Provision a full env on an existing PR's branch, run a review worker,
        and post the review as forge[bot] (or under the user token, branded, if
        no App). Generator of TurnEvents; terminal event kind 'review'."""
        try:
            ref = parse_pr_ref(pr_ref)
        except ValueError as e:
            self.store.create_run(run_id, str(pr_ref), "", "")
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "prref", "detail": str(e)[:300]})
            return
        self.store.create_run(run_id, ref.slug,
                              f"review {ref.slug}#{ref.number}",
                              f"pr-{ref.number}")
        self.store.set_session_fields(
            run_id, repo_source=f"review:{ref.slug}#{ref.number}",
            title=f"Review {ref.slug}#{ref.number}")
        # Persist the request so the web transcript (store.list_messages) shows
        # the same conversation as Slack, not just a lone "Review posted" line.
        self.store.add_message(run_id, "user", f"Review {ref.slug}#{ref.number}")
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        self.store.set_state(run_id, "provisioning")
        yield TurnEvent("phase", {"name": "clone",
                                  "label": f"Checking out PR #{ref.number}"})
        cl = self.host.clone_pr(ref.slug, ws, ref.number, self.cfg.gh_token)
        if cl.exit_code != 0:
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "clone",
                                      "detail": (cl.stderr or cl.stdout)[:300]})
            return
        exclude_forge_scratch(self.host, ws)
        yield from self._provision(run_id, ws)
        if self.store.get_run(run_id).get("state") == "failed":
            return
        yield from self._review_pass(run_id, ref, model)

    def _review_pass(self, run_id, ref, model):
        from forge import browserview
        from forge.creds import redact_secrets
        env = self._env_for(run_id)
        # Pre-fetch the PR diff HOST-side and drop it into the workspace: the
        # review agent runs on untrusted PR code, so the container gets no
        # GitHub token — it reads .forge/pr.diff instead of running `gh`.
        ws = Path(self.cfg.runs_dir) / run_id / "workspace"
        diff = self.host.run(cmd.pr_diff_cmd(ref.slug, ref.number),
                             env={"GH_TOKEN": self.cfg.gh_token}).stdout
        self.host.write_file(str(ws / ".forge" / "pr.diff"), diff or "")
        # Fresh artifact set for this review; stored creds injected + redacted,
        # exactly like the QA turn (session._qa).
        self._reset_artifacts(run_id)
        creds = self._qa_credentials(run_id)
        secrets = [c.get("password") for c in (creds or []) if c.get("password")]
        red = lambda s: redact_secrets(s, secrets)
        app_url = self._app_url(run_id)
        full = build_review_prompt(ref.slug, ref.number, app_url, credentials=creds)
        chosen = self.provider.resolve_model(
            model, "review for correctness bugs and security")
        yield TurnEvent("model", {"choice": model, "resolved": chosen})
        yield TurnEvent("phase", {"name": "agent", "label": "Reviewing"})
        # Live agent-browser view: start the shared CDP Chromium + screencaster
        # only when there is an app to drive (same guard as turn()); stop is
        # always attempted. Both ends best-effort — a browser failure never
        # fails the review.
        if app_url:
            browserview.start(self.cfg.runs_dir, run_id, env)
        try:
            result = yield from self._stream_worker(
                run_id, env, full, chosen, redact=red)
        finally:
            browserview.stop(self.cfg.runs_dir, run_id)
        if result and result.auth_error:
            yield TurnEvent("error", {"kind": "auth",
                                      "detail": red(result.result_text)[:300]})
            return
        # Persist the agent's review narration/result so the web transcript
        # mirrors turn()'s assistant message (cross-surface parity).
        if result:
            self.store.add_message(
                run_id, "assistant", red(result.result_text) or "(review complete)",
                meta={"cost": result.total_cost_usd, "model": chosen})
        posted = self._post_review(run_id, ref)
        msg = (f"Review posted: {posted['review_url']}" if posted.get("ok")
               else f"Review post failed: {posted.get('reason')}")
        self.store.add_message(run_id, "system", msg)
        self.store.touch_env(run_id)
        yield TurnEvent("review", posted)

    def _read_review(self, run_id):
        p = (Path(self.cfg.runs_dir) / run_id / "workspace"
             / ".forge" / "review.json")
        text = p.read_text() if p.is_file() else ""
        return reviewlib.parse_review(text)

    def _post_review(self, run_id, ref) -> dict:
        rev = self._read_review(run_id)
        diff = self.host.run(cmd.pr_diff_cmd(ref.slug, ref.number),
                             env={"GH_TOKEN": self.cfg.gh_token}).stdout
        valid, dropped = reviewlib.partition(rev, reviewlib.diff_line_map(diff))
        token = self.cfg.gh_token
        header = _DEGRADE_HEADER
        degraded = True
        app = self._ghapp()
        if app is not None:
            try:
                token = app.installation_token(ref.owner, ref.repo)
                header, degraded = "", False
            except Exception:
                token, header, degraded = self.cfg.gh_token, _DEGRADE_HEADER, True
        payload = reviewlib.build_payload(rev, valid, dropped, header=header)
        pf = Path(self.cfg.runs_dir) / run_id / "review-payload.json"
        self.host.write_file(str(pf), json.dumps(payload))
        res = self.host.run(
            cmd.pr_review_api_cmd(ref.owner, ref.repo, ref.number, str(pf)),
            env={"GH_TOKEN": token})
        if res.exit_code != 0:
            return {"ok": False, "reason": "post_failed",
                    "detail": (res.stderr or res.stdout)[:300], "degraded": degraded}
        url = reviewlib.parse_review_url(res.stdout)
        self.store.set_state(run_id, self.store.get_run(run_id)["state"], pr_url=url)
        return {"ok": True, "review_url": url, "comments": len(valid),
                "dropped": len(dropped), "degraded": degraded}
