"""Pure helpers for collecting the visual artifacts an agent captured during a
turn: parse `.forge/artifacts/manifest.json` into validated, upload-ready
descriptors. No Slack I/O here — slackbot does the upload. Validation is the
security boundary: a manifest path is attacker-influenced (agent output), so we
only ever accept a bare filename that resolves inside the artifacts dir."""
import json
from dataclasses import dataclass
from pathlib import Path

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".webm"}
_VIDEO_EXT = {".mp4", ".webm"}
_VALID_KINDS = {"before", "after", "video"}
_MAX_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACTS = 6
_DEFAULT_CAPTION = {"before": "Before", "after": "After", "video": "Walkthrough"}


@dataclass(frozen=True)
class Artifact:
    path: Path
    kind: str       # before | after | video
    caption: str


def parse_manifest(manifest_text: str, artifacts_dir) -> list:
    artifacts_dir = Path(artifacts_dir)
    arts = _from_manifest(manifest_text, artifacts_dir)
    if not arts:                       # missing/garbage/useless manifest
        arts = _from_glob(artifacts_dir)
    return arts[:_MAX_ARTIFACTS]


def _resolve(name, artifacts_dir: Path):
    """A valid artifact is a bare filename, in the artifacts dir, with an
    allowed extension, that exists and is within the size cap."""
    if not isinstance(name, str) or not name:
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    p = artifacts_dir / name
    if p.suffix.lower() not in _ALLOWED_EXT:
        return None
    if not p.is_file():
        return None
    if p.stat().st_size > _MAX_BYTES:
        return None
    return p


def _infer_kind(name: str) -> str:
    n = name.lower()
    if n.startswith("before"):
        return "before"
    if Path(n).suffix in _VIDEO_EXT or n.startswith("flow") or n.startswith("video"):
        return "video"
    return "after"


def _from_manifest(manifest_text: str, artifacts_dir: Path) -> list:
    try:
        data = json.loads(manifest_text)
    except (json.JSONDecodeError, TypeError):
        return []
    entries = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        p = _resolve(e.get("path"), artifacts_dir)
        if p is None:
            continue
        kind = e.get("kind") if e.get("kind") in _VALID_KINDS else _infer_kind(p.name)
        caption = e.get("caption") or _DEFAULT_CAPTION[kind]
        out.append(Artifact(p, kind, str(caption)))
    return out


def _from_glob(artifacts_dir: Path) -> list:
    if not artifacts_dir.is_dir():
        return []
    out = []
    for p in sorted(artifacts_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _ALLOWED_EXT:
            continue
        if p.stat().st_size > _MAX_BYTES:
            continue
        kind = _infer_kind(p.name)
        out.append(Artifact(p, kind, _DEFAULT_CAPTION[kind]))
    return out
