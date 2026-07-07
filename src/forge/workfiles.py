"""Host-side, read-only view of a run's workspace for the live files pane.

The workspace is bind-mounted into the worker at /work but lives on the host
(<runs_dir>/<run_id>/workspace), so the web daemon can list and read it
directly — no container round-trip, and it keeps working while the agent holds
the container busy mid-turn. Everything here is strictly read-only: the worker
owns the git index (it runs `git add -N` during diffs), so host-side git is
limited to `status`, `ls-files` and `diff`, all through hardened_git — the
repo's .git/config is agent-writable and must be treated as hostile input.

Scratch files never leak: listing goes through git, which honors the
.forge/* patterns forge writes to .git/info/exclude at provision time, so
live-view frames, qa.json etc. stay invisible here just as they do in PRs.
"""
import subprocess
from pathlib import Path

from forge.hostops import hardened_git

# A source file bigger than this is truncated for display (the pane is a live
# viewer, not an editor); diffs get the same cap.
MAX_BYTES = 200_000
# Listing cap: a pathological repo (vendored deps committed) shouldn't produce
# a multi-MB JSON payload; the UI shows a "truncated" notice past this.
MAX_FILES = 20_000

_STATUS = {"A": "added", "D": "deleted", "R": "renamed", "C": "added",
           "?": "untracked"}


def workspace_dir(runs_dir, run_id) -> Path:
    return Path(runs_dir) / run_id / "workspace"


def _git(ws: Path, *args: str) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(hardened_git(str(ws), *args),
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _parse_status_z(out: str) -> dict:
    """`git status --porcelain=v1 -z` records → {path: status}. Rename/copy
    records carry a second NUL-separated field (the origin path) — consume it
    so it can't be misread as an independent clean record."""
    states = {}
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path = rec[:2], rec[3:]
        if "R" in xy or "C" in xy:
            i += 1                      # skip the rename/copy origin path
        code = xy.strip()[:1] or "M"
        states[path] = _STATUS.get(code, "modified")
    return states


def list_files(runs_dir, run_id) -> dict:
    """All workspace files the repo would see: tracked files plus untracked-
    but-not-ignored ones, each tagged with its git status. Best-effort — a
    missing workspace (asleep/archived run) is just an empty listing."""
    ws = workspace_dir(runs_dir, run_id)
    if not (ws / ".git").exists():
        return {"files": [], "truncated": False}
    tracked = _git(ws, "ls-files", "-z")
    status = _git(ws, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if tracked is None or tracked.returncode != 0:
        return {"files": [], "truncated": False}
    states = _parse_status_z(status.stdout) if status and status.returncode == 0 else {}
    paths = set(p for p in tracked.stdout.split("\0") if p)
    paths.update(states)
    ordered = sorted(paths)
    truncated = len(ordered) > MAX_FILES
    return {"files": [{"path": p, "status": states.get(p, "clean")}
                      for p in ordered[:MAX_FILES]],
            "truncated": truncated}


def safe_workspace_path(runs_dir, run_id, relpath: str) -> "Path | None":
    """Resolve a client-supplied relative path strictly inside the workspace,
    or None. Rejects absolute paths, traversal (after symlink resolution) and
    anything under .git — the live pane serves repo files, not git internals."""
    rel = (relpath or "").strip()
    if not rel or rel.startswith(("/", "\\")) or "\0" in rel:
        return None
    ws = workspace_dir(runs_dir, run_id).resolve()
    try:
        p = (ws / rel).resolve()
    except OSError:
        return None
    if p != ws and ws not in p.parents:
        return None
    if ".git" in p.relative_to(ws).parts:
        return None
    return p


def file_detail(runs_dir, run_id, relpath: str) -> "dict | None":
    """One file for the viewer: content (size-capped, binary-flagged) plus its
    uncommitted diff. Untracked files get a --no-index pseudo-diff against
    /dev/null so brand-new files render as all-additions like any other change.
    None = bad path (route turns it into a 404)."""
    p = safe_workspace_path(runs_dir, run_id, relpath)
    if p is None:
        return None
    ws = workspace_dir(runs_dir, run_id)
    rel = str(p.relative_to(ws.resolve()))
    detail = {"path": rel, "status": "clean", "size": 0, "truncated": False,
              "binary": False, "missing": False, "content": "", "diff": ""}
    st = _git(ws, "status", "--porcelain=v1", "-z", "--", rel)
    if st and st.returncode == 0 and st.stdout:
        detail["status"] = _parse_status_z(st.stdout).get(rel, "clean")
    if not p.is_file():
        detail["missing"] = True
        detail["status"] = detail["status"] if detail["status"] != "clean" else "deleted"
    else:
        try:
            raw = p.read_bytes()
        except OSError:
            raw = b""
        detail["size"] = len(raw)
        detail["truncated"] = len(raw) > MAX_BYTES
        chunk = raw[:MAX_BYTES]
        if b"\0" in chunk:
            detail["binary"] = True
        else:
            detail["content"] = chunk.decode("utf-8", errors="replace")
    if detail["status"] == "untracked":
        d = _git(ws, "diff", "--no-index", "--", "/dev/null", rel)
    else:
        d = _git(ws, "diff", "HEAD", "--", rel)
    if d is not None and not detail["binary"]:
        diff = d.stdout
        if len(diff) > MAX_BYTES:
            diff = diff[:MAX_BYTES] + "\n… diff truncated …\n"
        detail["diff"] = diff
    return detail
