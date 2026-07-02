import subprocess
from pathlib import Path


def _origin(path: Path) -> str:
    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def list_repos(workspace_dir, q: str = "") -> list:
    base = Path(workspace_dir)
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            if q.lower() in child.name.lower():
                out.append({"name": child.name, "path": str(child),
                            "remote": _origin(child)})
    return out
