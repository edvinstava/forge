"""Host-side attachment inbox.

User-supplied images (Slack file_share downloads, web uploads) are staged in
runs/<run_id>/inbox/ — a host dir that exists independently of the workspace
(the workspace is created by the repo clone at provision time, so a first
message's attachments can't be written straight into it). sync() copies named
files into workspace/.forge/inbox/, which the compose recipe bind-mounts at
/work, so the agent can view them with its Read tool. .forge/inbox/ is in
hostops._FORGE_SCRATCH_PATTERNS, so synced images never enter a PR diff.
"""
import re
import shutil
import time
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024   # per file
MAX_FILES = 5                  # per message
CONTAINER_DIR = "/work/.forge/inbox"

_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_EXT_FOR_MIME = {"image/png": ".png", "image/jpeg": ".jpg",
                 "image/gif": ".gif", "image/webp": ".webp"}


def _sanitize(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "image").name)
    base = base.strip("._") or "image"
    return base[-80:]


def save(runs_dir, run_id: str, filename: str, data: bytes,
         mimetype: str | None = None) -> str:
    """Validate + write one image into runs/<run_id>/inbox/. Returns the
    stored name (callers thread it to turn()/plan_task()). Raises ValueError
    for non-image names/oversize payloads — ingress surfaces these as notes."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"file too large (max {MAX_BYTES // (1024 * 1024)} MB)")
    name = _sanitize(filename)
    if Path(name).suffix.lower() not in _ALLOWED_EXTS:
        ext = _EXT_FOR_MIME.get((mimetype or "").lower())
        if ext is None:
            raise ValueError("only image attachments are supported (png/jpg/gif/webp)")
        name += ext
    d = Path(runs_dir) / run_id / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{int(time.time())}-{name}"
    n = 1
    while p.exists():
        p = d / f"{int(time.time())}-{n}-{name}"
        n += 1
    p.write_bytes(data)
    return p.name


def sync(runs_dir, run_id: str, names) -> list:
    """Copy named inbox files into workspace/.forge/inbox/ (visible in the
    container at /work/.forge/inbox). Returns container paths for the files
    that exist; missing/suspicious names are skipped (best-effort — the prompt
    must only reference files the agent can actually Read)."""
    src = Path(runs_dir) / run_id / "inbox"
    dst = Path(runs_dir) / run_id / "workspace" / ".forge" / "inbox"
    out = []
    for name in names or []:
        if "/" in name or "\\" in name or name.startswith("."):
            continue
        s = src / name
        if not s.is_file():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, dst / name)
        out.append(f"{CONTAINER_DIR}/{name}")
    return out
