"""Forge-side per-repo environment knowledge: small declarative overlays the
resolver merges onto a deterministic recipe and the self-heal agent learns.
Stored as YAML under <root>/<owner>/<repo>.yml — never inside the repo, so it
can never reach a PR."""
import os
from pathlib import Path

import yaml

OVERLAY_KEYS = {"schema", "repo", "pkg_manager", "apt", "dev_cmd",
                "web_port", "health_path", "env", "qa_credentials",
                "provenance", "lessons"}
_PKG_MANAGERS = {"bun", "pnpm", "yarn", "npm"}
LESSONS_CAP = 50   # max durable lessons kept per repo (most recent win)


def validate(overlay: dict) -> dict:
    """Return the overlay unchanged or raise ValueError. Keeps the surface small
    (YAGNI): unknown keys and bad package managers are rejected outright."""
    if not isinstance(overlay, dict):
        raise ValueError("overlay must be a mapping")
    extra = set(overlay) - OVERLAY_KEYS
    if extra:
        raise ValueError(f"unknown overlay keys: {sorted(extra)}")
    pm = overlay.get("pkg_manager")
    if pm is not None and pm not in _PKG_MANAGERS:
        raise ValueError(f"pkg_manager must be one of {sorted(_PKG_MANAGERS)}")
    apt = overlay.get("apt")
    if apt is not None and not (isinstance(apt, list)
                                and all(isinstance(x, str) for x in apt)):
        raise ValueError("apt must be a list of strings")
    lessons = overlay.get("lessons")
    if lessons is not None:
        if not isinstance(lessons, list) or not all(
                isinstance(l, dict) and l.get("text") for l in lessons):
            raise ValueError("lessons must be a list of dicts each with a non-empty text")
    creds = overlay.get("qa_credentials")
    if creds is not None:
        if not (isinstance(creds, list) and all(
                isinstance(c, dict) and isinstance(c.get("username"), str)
                and isinstance(c.get("password"), str) for c in creds)):
            raise ValueError(
                "qa_credentials must be a list of {username, password[, role]} dicts")
    return overlay


def _trim_lessons(lessons: list) -> list:
    """Cap at LESSONS_CAP keeping the most recent — but user-taught lessons
    (kind == 'user') are never evicted by auto-learned ones: a teammate's
    explicit instruction outranks anything the retrospective inferred."""
    if len(lessons) <= LESSONS_CAP:
        return lessons
    is_user = [isinstance(l, dict) and l.get("kind") == "user" for l in lessons]
    budget = LESSONS_CAP - sum(is_user)
    auto_idx = [i for i, u in enumerate(is_user) if not u]
    drop = set(auto_idx[: max(0, len(auto_idx) - max(budget, 0))])
    return [l for i, l in enumerate(lessons) if i not in drop]


def merge_overlay(base: dict, delta: dict) -> dict:
    """delta wins per key; apt lists union (order-stable, de-duped)."""
    out = dict(base or {})
    for k, v in (delta or {}).items():
        if k == "apt":
            seen = list(out.get("apt", []))
            for x in v or []:
                if x not in seen:
                    seen.append(x)
            out["apt"] = seen
        elif k == "lessons":
            seen = list(out.get("lessons", []))
            texts = {l.get("text") for l in seen if isinstance(l, dict)}
            for l in v or []:
                if isinstance(l, dict) and l.get("text") and l["text"] not in texts:
                    seen.append(l)
                    texts.add(l["text"])
            out["lessons"] = _trim_lessons(seen)
        elif k == "qa_credentials":
            def _key(c):
                # Same role -> replace; role-less entries keyed by username.
                return ("role", c["role"]) if c.get("role") else ("user", c.get("username") or "")
            by = {_key(c): c for c in out.get("qa_credentials", [])
                  if isinstance(c, dict)}
            for c in v or []:
                if isinstance(c, dict):
                    by[_key(c)] = c
            out["qa_credentials"] = list(by.values())
        else:
            out[k] = v
    return out


class KnowledgeStore:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, slug: str) -> Path:
        owner, _, repo = slug.partition("/")
        return self.root / owner / f"{repo or owner}.yml"

    def load(self, slug: str):
        p = self._path(slug)
        if not p.is_file():
            return None
        return validate(yaml.safe_load(p.read_text()) or {})

    def save(self, slug: str, overlay: dict) -> None:
        overlay = validate(overlay)
        p = self._path(slug)
        # Overlays can hold saved QA logins — keep them owner-only on disk.
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
        p.write_text(yaml.safe_dump(overlay, sort_keys=True))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    def merge_save(self, slug: str, delta: dict) -> dict:
        merged = merge_overlay(self.load(slug) or {}, validate(delta))
        self.save(slug, merged)
        return merged
