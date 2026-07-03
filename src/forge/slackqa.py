"""Clone-only repo Q&A fast path: shallow-clone a repo to a TTL'd cache dir and
run a one-shot agent-CLI call against it in a disposable worker container
(`docker run --rm`, clone mounted read-only) — no compose project, no session.
`run`/`clock` are injectable for tests."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from forge import providers
from forge.slackmsg import clean_summary

_MARKER = ".forge-qa-ts"
# A GitHub slug is owner/repo with a conservative char set. Validating before it
# reaches the `gh` argv blocks argument-injection (a slug starting with `-` could
# otherwise smuggle flags) and path traversal in qa_dir.
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Let the agent decide length — short questions get short answers, ones that
# need explaining (e.g. "what tests are failing?") get the detail the user wants.
_QA_PROMPT = ("Answer this question about the repository in this directory. "
             "Answer directly; keep it as short as the question allows, but give "
             "the detail it needs. Do not modify any files.\n\nQuestion: {q}")


def qa_dir(cfg, slug: str) -> Path:
    return Path(cfg.runs_dir) / "cache" / "qa" / slug.replace("/", "-")


def needs_clone(d: Path, ttl_secs: int, clock) -> bool:
    marker = Path(d) / _MARKER
    if not marker.is_file():
        return True
    try:
        return (clock() - float(marker.read_text())) > ttl_secs
    except (ValueError, OSError):
        return True


def container_argv(cfg, provider, d: Path, agent_argv: list, env_keys) -> list:
    """One-shot `docker run` for the agent CLI against an untrusted clone: the
    repo rides read-only at /work (Q&A must never modify it), secrets ride as
    name-only `-e KEY` flags with values in the client process env — inside the
    container only, never argv. --entrypoint overrides the worker image's
    `sleep infinity`. Codex plan auth mounts ~/.codex like the compose worker
    (session._mount_provider_auth) — read-write, the CLI refreshes auth.json."""
    cmd = ["docker", "run", "--rm", "-v", f"{d}:/work:ro", "-w", "/work",
           "--entrypoint", agent_argv[0]]
    for k in env_keys:
        cmd += ["-e", k]
    if provider.name == "codex" and cfg.codex_auth != "api":
        home = providers.codex_home()
        if home.is_dir():
            cmd += ["-v", f"{home}:/home/forge/.codex"]
    return cmd + [cfg.image_tag] + agent_argv[1:]


def answer_question(cfg, slug: str, question: str, run=subprocess.run,
                    clock=time.time) -> str:
    if not _SLUG.match(slug or "") or ".." in slug:
        return f"⚠️ `{slug}` doesn't look like a repo I can fetch (expected `owner/repo`)."
    d = qa_dir(cfg, slug)
    if needs_clone(d, cfg.repo_cache_ttl_secs, clock):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.parent.mkdir(parents=True, exist_ok=True)
        cl = run(["gh", "repo", "clone", slug, str(d), "--", "--depth", "1"],
                 env={**os.environ, "GH_TOKEN": cfg.gh_token},
                 capture_output=True, text=True)
        if getattr(cl, "returncode", 1) != 0:
            return f"⚠️ couldn't fetch `{slug}`: {(cl.stderr or cl.stdout or '')[:200]}"
        d.mkdir(exist_ok=True)
        (d / _MARKER).write_text(str(clock()))
    p = providers.from_config(cfg)
    secrets = p.secrets(cfg)
    r = run(container_argv(cfg, p, d, p.worker_cmd(_QA_PROMPT.format(q=question),
                                                   None), secrets),
            env={**os.environ, **secrets}, capture_output=True, text=True)
    res = p.parse_result(r.stdout)
    if res.auth_error or res.is_error:
        return f"⚠️ couldn't answer that one: {(res.result_text or 'error')[:200]}"
    return clean_summary(res.result_text)
