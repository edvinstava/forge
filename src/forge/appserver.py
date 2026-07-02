import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AppSpec:
    start_argv: list
    port: int
    health_path: str
    ok: bool


def _repo_yml_start(repo_yml):
    start = re.search(r"playwright:\s*\n(?:\s+.*\n)*?\s+start:\s*(.+)", repo_yml)
    if not start:
        return None
    port = re.search(r"playwright:\s*\n(?:\s+.*\n)*?\s+port:\s*(\d+)", repo_yml)
    return start.group(1).strip(), (int(port.group(1)) if port else None)


def detect_appserver(repo_yml, package_json, default_port=3000) -> AppSpec:
    """Decide how to start the repo's web/dev server and on which port.

    Precedence: .forge/repo.yml `playwright.start` (+ optional `port`) →
    package.json `dev` else `start` script → ok=False if none. The app is
    forced onto a single known port via the PORT env var so the orchestrator
    can publish that port deterministically.
    """
    if repo_yml:
        hit = _repo_yml_start(repo_yml)
        if hit:
            cmd, port = hit
            port = port or default_port
            return AppSpec(["sh", "-lc", f"PORT={port} {cmd}"], port, "/", True)
    if package_json:
        try:
            scripts = json.loads(package_json).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for name in ("dev", "start"):
            if name in scripts:
                return AppSpec(
                    ["sh", "-lc", f"PORT={default_port} npm run {name}"],
                    default_port, "/", True)
    return AppSpec([], default_port, "/", False)
