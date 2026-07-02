"""Self-heal agent: probe an unfamiliar live instance (or repair a failed one)
and emit a small declarative overlay. The agent runs as the existing `forge`
worker service (its own instance only) and writes the overlay to OVERLAY_PATH,
which Forge reads back and validates — robust against free-text drift."""
import yaml

from forge import knowledge, providers

OVERLAY_PATH = "/tmp/forge-overlay.yml"

_KEYS = ("pkg_manager (bun|pnpm|yarn|npm), apt (system libs), dev_cmd, "
         "web_port, health_path, env")

_PROBE_PROMPT = f"""You are configuring a dev environment for the repo mounted at /work.
Inspect it (lockfiles, package.json, framework, configs) and determine ONLY what Forge
needs to run and verify it. Write a YAML overlay to {OVERLAY_PATH} with any of these keys
that apply: {_KEYS}. Use the package manager whose lockfile is present. List in `apt` any
system libraries missing to run the dev server or launch a headless browser (check with
ldd if a browser is involved). Do NOT modify anything under /work. Write ONLY the YAML file."""

_REPAIR_PROMPT = """Provisioning failed at the {phase} step for the repo at /work.
Failure output:
---
{logs}
---
Diagnose the ENVIRONMENT problem (missing system lib, wrong package manager, wrong
command/port). Write a YAML overlay to {path} with the minimal fix using keys: {keys}.
Do NOT modify anything under /work. Write ONLY the YAML file."""


def _run_and_read(env, prompt, model, provider=None):
    p = provider or providers.ClaudeProvider()
    r = env.exec(p.worker_cmd(prompt, model), service="forge")
    if r.exit_code != 0:
        return None
    cat = env.exec(["cat", OVERLAY_PATH], service="forge")
    if cat.exit_code != 0 or not cat.stdout.strip():
        return None
    try:
        return knowledge.validate(yaml.safe_load(cat.stdout) or {})
    except (ValueError, yaml.YAMLError):
        return None


def probe(env, model=None, max_iterations=6, provider=None):
    # max_iterations is reserved for a future multi-turn probe; the one-shot
    # CLI call is single-shot here. Kept in the signature so callers pin a budget.
    return _run_and_read(env, _PROBE_PROMPT, model, provider)


def repair(env, failure_phase, logs, model=None, max_iterations=6, provider=None):
    prompt = _REPAIR_PROMPT.format(phase=failure_phase, logs=(logs or "")[-2000:],
                                   path=OVERLAY_PATH, keys=_KEYS)
    return _run_and_read(env, prompt, model, provider)
