"""Conversational first-reply opener: a one-shot agent-CLI run on the host that
reads the user's actual message and writes forge's opening line. Mirrors
slackqa / slackchat's host-CLI pattern, but needs no repo — it runs in a
neutral empty dir on the provider's fast model. On ANY failure it returns the
caller-supplied `fallback` template, so behavior is never worse than the
canned greeting. `run` is injectable for tests."""
import os
import subprocess
from pathlib import Path

from forge import providers
from forge.slackmsg import opener_prompt

# Fast + cheap: it's one sentence. The timeout is a ceiling, not a target — a
# warm fast-model call returns in ~3-6s; cold starts run longer, so we give
# comfortable headroom (the opener is hidden behind the instant ack and dwarfed
# by spin-up). Past the ceiling we fall back rather than stall the session.
_TIMEOUT_SECS = 15


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def generate_opener(cfg, slug: str, task: str, mode: str, fallback: str,
                    run=subprocess.run) -> str:
    # Neutral empty dir so the agent has no repo to scan — it only reacts to
    # the message text we hand it in the prompt.
    p = providers.from_config(cfg)
    d = Path(cfg.runs_dir) / "cache" / "opener"
    try:
        d.mkdir(parents=True, exist_ok=True)
        argv = p.worker_cmd(opener_prompt(task, slug, mode), p.fast_model)
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
    return _one_line(res.result_text) or fallback
