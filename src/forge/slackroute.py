"""Conversational router: one host `claude -p` that, for a message the regex
classifier (`slackmsg.classify_intent`) couldn't pin down, decides whether the
teammate actually wants a build, a repo question, or genuine chat — and, for
chat, writes the reply in the same call. This is the multilingual brain that
keeps a Norwegian 'kan du lage en side…' from being mistaken for small talk and
answered with an empty promise.

Mirrors slackchat / slackopener's host-`claude` pattern: a neutral empty cwd,
the default model, and a hard timeout. On ANY failure it returns
`Route("chat", fallback)`, so behavior is never worse than the canned blurb.
`run` is injectable for tests."""
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from forge import commands
from forge.slackmsg import clean_summary, route_prompt
from forge.worker import parse_worker_result

_TIMEOUT_SECS = 30
_ACTIONS = {"build", "qa", "chat"}


@dataclass(frozen=True)
class Route:
    action: str               # "build" | "qa" | "chat"
    reply: str | None = None  # populated only for action == "chat"


def _extract_json(text: str):
    """Pull the first {...} object out of the model's reply, tolerating prose or
    code fences around it."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def parse_route(result_text: str, fallback: str) -> Route:
    obj = _extract_json(result_text)
    if not isinstance(obj, dict):
        return Route("chat", fallback)
    action = str(obj.get("action", "")).strip().lower()
    if action == "question":
        action = "qa"
    if action not in _ACTIONS:
        return Route("chat", fallback)
    if action == "chat":
        reply = obj.get("reply") or ""
        return Route("chat", clean_summary(reply) if reply.strip() else fallback)
    return Route(action, None)


def route_chat(cfg, transcript, latest, fallback, run=subprocess.run) -> Route:
    # Neutral empty dir: routing/answering is about the message, not a repo.
    d = Path(cfg.runs_dir) / "cache" / "chat"
    try:
        d.mkdir(parents=True, exist_ok=True)
        argv = commands.worker_cmd(route_prompt(transcript, latest), None)
        r = run(argv, cwd=str(d),
                env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": cfg.oauth_token},
                capture_output=True, text=True, timeout=_TIMEOUT_SECS)
    except Exception:
        return Route("chat", fallback)
    if getattr(r, "returncode", 1) != 0:
        return Route("chat", fallback)
    res = parse_worker_result(r.stdout)
    if res.auth_error or res.is_error:
        return Route("chat", fallback)
    return parse_route(res.result_text, fallback)
