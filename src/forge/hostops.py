import os
import subprocess
from pathlib import Path

from forge.container import ExecResult

# Files forge writes during a run must NEVER appear in a PR diff. Written to
# <ws>/.git/info/exclude (local-only — never tracked) so that `git add -A`
# silently ignores them regardless of call-site ordering.
#
# Default-deny: everything under .forge/ is scratch unless re-included below.
# Enumerating scratch files per-feature leaked twice (.forge/pr.json, then
# .forge/live/ into a real PR) — new writers must not need to know this list
# exists. The negations must stay AFTER ".forge/*": gitignore is
# last-match-wins, and a negation can only re-include a path whose parent
# directory isn't itself excluded (".forge/*" excludes children, not .forge/).
_FORGE_SCRATCH_PATTERNS = [
    "/report.md",
    ".forge/*",
    "!.forge/repo.yml",
    "!.forge/env.yml",
    "!.forge/verify.sh",
]


def hardened_git(ws: str, *args: str) -> list:
    """git argv for HOST-side runs against a workspace the agent has already
    modified. Treat that repo's .git/config as hostile input: core.fsmonitor,
    core.hooksPath and credential.helper can each make git execute arbitrary
    commands — on the HOST, outside the container sandbox. Command-line -c
    wins over repo config for the single-valued keys; the empty
    credential.helper RESETS the repo's helper list, then gh is re-added as
    the only helper so authenticated pushes still work (token via env)."""
    return ["git",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "credential.helper=!gh auth git-credential",
            "-C", ws, *args]


def exclude_forge_scratch(host, ws: str) -> None:
    """Append forge scratch-file patterns to <ws>/.git/info/exclude idempotently.

    Uses .git/info/exclude rather than a tracked .gitignore so the file never
    appears in the repo's own tree or the PR diff.  Repo-authored config files
    (.forge/repo.yml, .forge/env.yml, .forge/verify.sh) are re-included and
    will still be picked up by `git add -A`; gitignore never affects files the
    repo already tracks, so tracked .forge/ content keeps flowing either way.
    """
    exclude_rel = ".git/info/exclude"
    existing = host.read(ws, exclude_rel) or ""
    lines = existing.splitlines(keepends=True)

    # Collect patterns not already present (exact line match after stripping)
    present = {ln.rstrip("\r\n") for ln in lines}
    to_add = [p for p in _FORGE_SCRATCH_PATTERNS if p not in present]
    if not to_add:
        return

    # Ensure file ends with a newline before appending
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_content = existing + "\n".join(to_add) + "\n"
    host.write_file(str(Path(ws) / exclude_rel), new_content)


class LocalHost:
    """Host-side operations: clone the repo (so its files can be probed and
    bind-mounted into the compose project), read/write workspace files, and run
    host commands (e.g. the Supabase CLI)."""

    def clone(self, repo: str, branch: str, dest: str, gh_token: str) -> ExecResult:
        # Guard against argv flag-smuggling: a dash-leading repo/dest could be
        # parsed as a gh flag (mirrors clone_pr / clone_local's guard).
        if repo.startswith("-") or dest.startswith("-"):
            return ExecResult(2, "", "refusing suspicious clone argument")
        d = Path(dest)
        d.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GH_TOKEN": gh_token}
        r1 = subprocess.run(["gh", "repo", "clone", repo, str(d)],
                            capture_output=True, text=True, env=env)
        if r1.returncode != 0:
            return ExecResult(r1.returncode, r1.stdout, r1.stderr)
        r2 = subprocess.run(["git", "-C", str(d), "checkout", "-b", branch],
                            capture_output=True, text=True)
        return ExecResult(r2.returncode, r2.stdout, r2.stderr)

    def clone_pr(self, repo: str, dest: str, number: int, gh_token: str) -> ExecResult:
        # Guard against argv flag-smuggling: a dash-leading repo/dest could be
        # parsed as a gh flag (mirrors clone_local's guard).
        if repo.startswith("-") or dest.startswith("-"):
            return ExecResult(2, "", "refusing suspicious clone argument")
        d = Path(dest)
        d.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GH_TOKEN": gh_token}
        r1 = subprocess.run(["gh", "repo", "clone", repo, str(d)],
                            capture_output=True, text=True, env=env)
        if r1.returncode != 0:
            return ExecResult(r1.returncode, r1.stdout, r1.stderr)
        r2 = subprocess.run(["gh", "pr", "checkout", str(number)],
                            capture_output=True, text=True, env=env, cwd=str(d))
        return ExecResult(r2.returncode, r2.stdout, r2.stderr)

    def read(self, dest: str, relpath: str) -> str | None:
        p = Path(dest) / relpath
        return p.read_text() if p.is_file() else None

    def exists(self, dest: str, relpath: str) -> bool:
        return (Path(dest) / relpath).exists()

    def write_file(self, path: str, content: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def run(self, argv: list, env: dict | None = None) -> ExecResult:
        proc_env = {**os.environ, **(env or {})}
        out = subprocess.run(argv, capture_output=True, text=True, env=proc_env)
        return ExecResult(out.returncode, out.stdout, out.stderr)

    def origin_url(self, path: str) -> str | None:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True)
        url = r.stdout.strip()
        return url if (r.returncode == 0 and url) else None

    def clone_local(self, src: str, branch: str, dest: str) -> ExecResult:
        if src.startswith("-"):
            return ExecResult(2, "", "refusing suspicious source path")
        d = Path(dest)
        d.parent.mkdir(parents=True, exist_ok=True)
        r1 = subprocess.run(["git", "clone", "--", src, str(d)],
                            capture_output=True, text=True)
        if r1.returncode != 0:
            return ExecResult(r1.returncode, r1.stdout, r1.stderr)
        gh_origin = self.origin_url(src)
        if gh_origin:
            subprocess.run(["git", "-C", str(d), "remote", "set-url", "origin", gh_origin],
                           capture_output=True)
        r2 = subprocess.run(["git", "-C", str(d), "checkout", "-b", branch],
                            capture_output=True, text=True)
        return ExecResult(r2.returncode, r2.stdout, r2.stderr)
