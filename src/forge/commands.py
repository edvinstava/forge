import re


def clone_cmd(repo: str) -> list:
    return ["gh", "repo", "clone", repo, "."]


def branch_cmd(branch: str) -> list:
    return ["git", "checkout", "-b", branch]


def setup_git_cmd() -> list:
    # Wire git's credential helper to gh (which reads GH_TOKEN from env) so
    # `git push` authenticates. The token stays in env, never in argv.
    return ["gh", "auth", "setup-git"]


def worker_cmd(prompt: str, model: str | None, session_id: str | None = None) -> list:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]   # carry context across fix iterations
    return cmd


def worker_stream_cmd(prompt: str, model: str | None,
                      session_id: str | None = None) -> list:
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def has_changes_cmd() -> list:
    return ["git", "status", "--porcelain"]


def restore_lockfile_churn_cmd() -> list:
    """Undo lockfile-only churn before committing: a newer package manager in
    the container may rewrite lockfile metadata (e.g. bun bumping
    lockfileVersion) with zero dependency change, and `git add -A` would ship
    that noise in the PR. If no package.json changed anywhere in the tree, a
    changed root-level lockfile is restored to HEAD. The intent-to-add pass
    makes brand-new (untracked) package.json files count as manifest changes —
    a freshly scaffolded package must keep its lockfile update. Deliberately
    conservative: one manifest edit anywhere keeps every lockfile change."""
    script = (
        'git add -A -N; changed=$(git diff --name-only HEAD); '
        'if ! printf "%s\\n" "$changed" | grep -Eq "(^|/)package\\.json$"; then '
        'for f in bun.lock bun.lockb package-lock.json pnpm-lock.yaml yarn.lock; do '
        'printf "%s\\n" "$changed" | grep -qx "$f" && git checkout HEAD -- "$f"; '
        'done; fi; true')
    return ["bash", "-lc", script]


def commit_cmds(message: str, name: str, email: str) -> list:
    return [
        ["git", "config", "user.name", name],
        ["git", "config", "user.email", email],
        ["git", "add", "-A"],
        ["git", "commit", "-m", message],
    ]


def push_cmd(branch: str) -> list:
    return ["git", "push", "-u", "origin", branch]


def pr_create_cmd(title: str, body_file: str, draft: bool) -> list:
    cmd = ["gh", "pr", "create", "--title", title, "--body-file", body_file]
    if draft:
        cmd.append("--draft")
    return cmd


def pr_checkout_cmd(number: int) -> list:
    return ["gh", "pr", "checkout", str(number)]


def pr_diff_cmd(slug: str, number: int) -> list:
    return ["gh", "pr", "diff", str(number), "-R", slug, "--patch"]


def pr_review_api_cmd(owner: str, repo: str, number: int, payload_file: str) -> list:
    return ["gh", "api", "--method", "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            "--input", payload_file]


def parse_host_port(stdout: str) -> int | None:
    """Parse `docker port <cid> <cport>` output (e.g. '127.0.0.1:5051') →
    the host port, taking the first line."""
    text = (stdout or "").strip()
    if not text:
        return None
    m = re.search(r":(\d+)\s*$", text.splitlines()[0])
    return int(m.group(1)) if m else None
