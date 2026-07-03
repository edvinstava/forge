"""Self-heal agent: probe an unfamiliar repo (or repair a failed instance) and
emit a small declarative overlay. The agent runs as the existing `forge`
worker service (its own instance only) and writes the overlay to OVERLAY_PATH,
which Forge reads back and validates — robust against free-text drift.

The probe is the universal fallback behind "spin up anything": when no
deterministic marker matched, the agent reads the repo like a developer would
(README first, then any ecosystem's manifests, then code) and describes a
complete environment — base image, setup commands, dev command, port, extra
service containers — which the resolver synthesizes into a compose."""
import yaml

from forge import knowledge, providers

OVERLAY_PATH = "/tmp/forge-overlay.yml"

_KEYS = """\
  pkg_manager: bun|pnpm|yarn|npm (JS repos: the one whose lockfile is committed)
  image: base docker image for the app when the default Node worker image cannot run
    this stack (e.g. python:3.12-slim, golang:1.23, ruby:3.3, eclipse-temurin:21,
    rust:1-slim, php:8.3-cli). Debian-based images work best with apt.
  apt: system packages to apt-get install before anything else
  setup_cmds: shell commands run once before the server, in order (install
    dependencies, build, migrate/seed a db) — e.g. ["pip install -e ."]
  dev_cmd: the command that runs the app server. It MUST stay in the foreground
    (no daemonizing) and MUST listen on 0.0.0.0 (health checks and the proxy come
    from another container; localhost-only binds look dead). $PORT is exported.
  web_port: the TCP port dev_cmd listens on
  health_path: an HTTP path that 2xx/3xxes once the app is up (default /)
  env: environment variables for the app service (never real secrets; dev-mode
    placeholder values are fine)
  services: extra containers the app needs, as name -> {image, environment,
    command} ONLY (no volumes/ports) — e.g. a postgres:16 or redis:7. They share
    a network with the app; reach them by service name (host `db`, not localhost),
    and wire the app to them via `env`."""

_PROBE_PROMPT = f"""You are configuring a dev environment for the repo mounted at /work, so an
automated system can run the app and verify changes against it. Figure out how to run this
repo the way a developer joining the project would:
  1. Read the README and any docs/CONTRIBUTING for run instructions.
  2. Read the manifests present — package.json, pyproject.toml, requirements.txt,
     setup.py, go.mod, Cargo.toml, Gemfile, pom.xml, build.gradle, mix.exs,
     composer.json, Makefile, Procfile — whatever the ecosystem is.
  3. Read the code if still unclear (entrypoints, app factories, default ports).
The repo may be ANY stack: JS, Python, Go, Rust, Ruby, JVM, PHP, a static site
(serve it, e.g. `python3 -m http.server`), anything. Prefer the repo's own dev-mode
command. Then write a YAML overlay to {OVERLAY_PATH} with the keys that apply:
{_KEYS}
Always include dev_cmd and web_port when the repo contains anything servable.
Do NOT modify anything under /work. Write ONLY the YAML file."""

_REPAIR_PROMPT = """Provisioning failed at the {phase} step for the repo at /work.
Failure output:
---
{logs}
---
Diagnose the ENVIRONMENT problem (missing system lib, wrong base image, missing setup
step or service, wrong package manager, wrong command/port, localhost-only bind).
Write a YAML overlay to {path} with the minimal fix using the keys that apply:
{keys}
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
