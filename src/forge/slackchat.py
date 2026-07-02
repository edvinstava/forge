"""Conversational chat reply: a one-shot agent-CLI run on the host that answers
as forge itself (vs. about a repo). Mirrors slackopener / slackqa's host-CLI
pattern, but needs no repo — it runs in a neutral empty dir and is handed the
prior transcript + the latest message. On ANY failure it returns the caller's
`fallback` (the canned help blurb), so behavior is never worse than today.
Unlike the opener it uses the DEFAULT model (richer, for thinking-through-ideas)
and keeps multi-line replies. `run` is injectable for tests."""
import os
import subprocess
from pathlib import Path

from forge import providers
from forge.slackmsg import chat_prompt, clean_summary

# Conversational, so a bit more headroom than the one-line opener; past the
# ceiling we fall back rather than leave the teammate hanging.
_TIMEOUT_SECS = 30


def generate_reply(cfg, transcript, latest, fallback, run=subprocess.run) -> str:
    # Neutral empty dir: forge answers about itself, not a repo, so there's
    # nothing to scan — it reacts only to the transcript + message we hand it.
    p = providers.from_config(cfg)
    d = Path(cfg.runs_dir) / "cache" / "chat"
    try:
        d.mkdir(parents=True, exist_ok=True)
        argv = p.worker_cmd(chat_prompt(transcript, latest), None)
        r = run(argv, cwd=str(d),
                env={**os.environ, **providers.host_env(p, cfg)},
                capture_output=True, text=True, timeout=_TIMEOUT_SECS)
    except Exception:
        return fallback
    if getattr(r, "returncode", 1) != 0:
        return fallback
    res = p.parse_result(r.stdout)
    if res.auth_error or res.is_error:
        return fallback
    return clean_summary(res.result_text) if (res.result_text or "").strip() else fallback
