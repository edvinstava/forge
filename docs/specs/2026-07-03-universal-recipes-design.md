# Universal recipes: spin up (just about) anything

**Date:** 2026-07-03
**Status:** approved (autonomous session; user directive: "give it a repo, and it
should understand and create a spin-up recipe … based on readme, package.json or
the code in general. Should be able to spin up just about anything.")

## Problem

Recipe resolution is marker-based: CHAP markers, `supabase/config.toml` + Next,
`package.json`, or the repo's own compose. Anything else — Python, Go, Rust,
Ruby, JVM, PHP, static sites, Makefile-driven apps — falls through to the
worker-only `none` recipe: Forge can edit and PR but never *runs* the app.

The pieces to fix this already exist but don't reach far enough:

* The **self-heal probe** (an agent inspecting the repo in the worker container)
  already runs on low-confidence resolutions, but its prompt is JS-centric and
  its output — the **overlay** — can only *patch* a recipe that already has a
  web service. `apply_overlay` returns the `none` recipe unchanged, so for a
  non-JS repo the probe's knowledge is unusable.
* `.forge/env.yml` is documented as precedence #1 and read into the Probe, but
  `resolve()` never looks at it.

## Design

One idea: make the overlay schema rich enough to *describe* an arbitrary
single-web-app environment, and teach the resolver to *synthesize* a recipe
from an overlay when no deterministic marker matched. The agent probe becomes
the universal fallback: read the README, the manifests, the code; emit an
overlay; Forge builds and runs the compose. Learned once per repo (knowledge
store), self-healed on failure by the existing repair loop.

### Alternatives considered

* **Agent emits a full docker-compose file** and Forge wraps it via the
  existing `repo_compose_recipe` machinery. Maximal expressiveness, but
  unvalidated LLM-authored compose on every miss (volumes, privileged, host
  ports…), and it abandons the knowledge-store merge semantics (apt union,
  lessons, repair deltas). Rejected.
* **Bake more deterministic templates** (python-web, go-web, rails…). Endless
  whack-a-mole; still fails on the long tail, which is the point of the
  feature. Rejected — deterministic markers stay as the fast path only.

### Overlay schema additions (`knowledge.py`)

| key          | shape                                   | meaning |
|--------------|-----------------------------------------|---------|
| `image`      | non-empty string, no whitespace         | base image for the app service (default: worker image) |
| `setup_cmds` | list of strings                         | shell steps before the dev server (install deps, build) |
| `services`   | map name → {image, environment, command}| extra containers the app needs (db, cache) |

`services` validation is strict containment: names are `[a-z0-9-]`, must not
collide with `web`/`forge`; each entry allows ONLY `image` (required string),
`environment` (string map), `command` (string or list). No volumes → no host
mounts; no ports → nothing published. Service containers are reachable from
the app by service name on the project network, same as compose normally
works. Merge semantics: `services` and `setup_cmds` replace wholesale (delta
wins) — partial merges of ordered command lists or service definitions are
ambiguous.

### Synthesized recipe (`recipe.py`)

`synthesized_recipe(workspace, worker_image, overlay)` → `Recipe("synthesized", …)`:

* `web` service: `image` = overlay image or worker image, repo mounted at
  `/work`, `entrypoint: sh -lc`, command = optional apt prefix (root user when
  apt present) + `setup_cmds` + `PORT=<web_port> <dev_cmd>`, env from overlay,
  port published loopback-only (`127.0.0.1::<port>`).
* overlay `services` appended verbatim (post-validation).
* the standard `forge` worker service injected.
* `health_path` from overlay (default `/`). Health polling already runs from
  the `forge` service across the network (`health.py` host param), so the app
  image needs no curl/bash.
* the Recipe remembers the overlay it was built from (`synth_overlay`) so the
  repair path can rebuild it.

`resolve()` precedence becomes (first match wins):

1. `.forge/env.yml` committed in the repo, when it validates as an overlay and
   declares `dev_cmd` + `web_port` → synthesized from it (merged over any
   learned overlay; committed beats learned). This finally implements the
   documented precedence #1. An env.yml that validates but declares no app
   acts as an overlay patch on whatever resolves below. An invalid env.yml is
   ignored (never fatal).
2. CHAP markers → dhis2-chap (unchanged)
3. supabase + next → next-supabase (unchanged)
4. package.json → node-web (unchanged)
5. repo's own compose → wrapped (unchanged)
6. learned overlay with `dev_cmd` + `web_port` → **synthesized**
7. else → none (worker-only)

Security note: a committed env.yml runs arbitrary commands/images in the
project's containers — the same trust level as `package.json`'s dev script or
the repo's own compose, both of which Forge already executes. Containment is
the container, not the recipe source.

`apply_overlay` on a synthesized recipe rebuilds web + extra services from
`merge_overlay(recipe.synth_overlay, delta)` but carries the existing `forge`
service over verbatim — session-applied mutations (codex auth mount) survive
repair. For other recipes, behavior is unchanged.

`runtime_facts` for synthesized recipes reports `dev_cmd` from the overlay
(pkg_manager stays None; test_cmds empty — verify.sh / repo.yml cover that).

### Probe/repair prompts (`envprobe.py`)

Rewritten stack-agnostic. The probe agent is told to:

* read README/docs first, then manifests across ecosystems (package.json,
  pyproject.toml, requirements.txt, go.mod, Cargo.toml, Gemfile, pom.xml,
  build.gradle, mix.exs, composer.json, Makefile, Procfile), then code;
* emit the full key set including `image`, `setup_cmds`, `services`;
* pick a sensible public base image when the node worker image can't run the
  stack (python:3.12-slim, golang:1.23, ruby:3.3, eclipse-temurin:21, …);
* make `dev_cmd` run in the FOREGROUND and bind 0.0.0.0 (the port is probed
  from another container);
* point the app at extra services via `env` using the service name as host;
* declare seed/bootstrap steps in `setup_cmds`, not by hand-running them.

The repair prompt gets the same key list, so the existing failure loop
(health-fail → agent diagnoses live container → overlay delta → retry once)
now covers wrong image / missing setup steps / missing db, not just apt/pm.

### What doesn't change

Session provisioning flow, knowledge store location and ownership, resource
caps (`apply_resource_limits` already caps any web service via setdefault),
tunnel/proxy/URL plumbing, verify plans, the repair-once policy, and all
existing recipes. The probe still runs only when resolution is low-confidence
and the repo has no overlay yet.

## Testing

TDD throughout: schema validation/merge in `test_knowledge.py`; synthesis,
precedence, env.yml, repair-rebuild, runtime facts in `test_recipe.py`; prompt
contract in `test_envprobe.py`; a provision-through-synthesis path in the
session tests. Full suite must pass before merge to master.
