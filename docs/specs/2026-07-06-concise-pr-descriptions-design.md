# Concise PR descriptions

**Date:** 2026-07-06
**Status:** approved (user directive: "improve the PR descriptions … quite short
and concise, and maybe include 1 image … if it's a visual fix or bug fix.")

## Problem

The PR bodies Forge opens are longer than they need to be. The agent writes
`.forge/pr.json` (`{title, body}`) guided by `_PR_META` in `prompts.py`, which
currently mandates **three** sections — `## Summary` (2–4 sentences),
`## Changes` (a bullet per file/area), and `## Testing`. For the small, focused
changes Forge typically ships, that reads as boilerplate: the diff already shows
the per-file changes, and whether verification passed is already signalled by
the PR's draft/ready state and its CI checks.

A reviewer opening a Forge PR should get the gist in a sentence or two, not a
templated wall.

## What we are *not* doing (and why)

The original ask floated embedding one screenshot of the change in the PR body.
We evaluated it and deliberately scoped it out:

- Forge **already** captures `before.png`/`after.png`/video into
  `.forge/artifacts/` (`_CAPTURE` in `prompts.py`) and uploads them to the
  Slack thread (`slackbot._post_artifacts`). That capture path is unchanged.
- Rendering an image *inside a GitHub PR body* needs a hosted URL. GitHub's
  attachment upload is cookie-gated (no bot-token API), so the only realistic
  mechanism is pushing the image into the repo and referencing a raw URL — which
  renders on **public** repos but not private ones (GitHub's image proxy can't
  authenticate), and adds a git side-effect (an extra media branch).

Given that trade-off the user chose **text-only PR bodies; screenshots stay in
Slack** (status quo for images). No image embedding is in scope. This section
records the decision so it isn't re-litigated later.

## Design

Two small, isolated changes. Both are prompt/composition tweaks — no change to
the PR-open flow (`session._finish_pr`), the artifacts path, or the git
side-effects.

### 1. Tighten the `_PR_META` instruction (`prompts.py`)

Rewrite the body guidance the agent follows so `.forge/pr.json`'s `body` is:

- `## Summary` — **1–2 sentences**: what changed and why.
- **Optionally** up to ~3 short bullets, included *only when they add signal*
  (a notable change, a caveat, a follow-up). Not a per-file list.
- A single `**Testing:** …` line stating what was verified — one line, not a
  `## Testing` section. Omit it if nothing meaningful was run.

Drop the mandatory `## Changes` and `## Testing` sections. Keep every existing
guardrail unchanged:

- title: imperative, specific, ≤72 chars, carry the task's issue key (ABC-123);
- write for the reviewer; concise and concrete;
- never mention Forge, sessions, or run ids; never paste the diff.

The QA prompt's own screenshot guidance (`prompts.py` ~244–290) is a separate
concern and is **not** touched.

### 2. Keep the no-body fallback concise (`prbody.compose_body`)

When the agent writes no `body`, `compose_body` today emits `## Task\n\n{task}`
— the entire task verbatim, which can be long. Add a small pure helper to clip
that fallback to a short summary (first paragraph / ~280 chars, word-boundary
ellipsis in the spirit of the existing `_clip`) so even the fallback path stays
short.

Everything else in `compose_body` is unchanged: the draft `> ⚠️ warning` still
comes first, `**Refs:**` and the `_Opened by forge · run … · branch …_` footer
are untouched. Those are Forge metadata, not part of the "concise" budget.

No hard clip is applied to an agent-*authored* body — clipping arbitrary
markdown mid-structure would mangle it. Length there is governed by the prompt.

## Components & boundaries

| Unit | Responsibility | Change |
|------|----------------|--------|
| `prompts._PR_META` | Instruct the agent how to write `pr.json` | Rewrite the body spec (Summary + optional bullets + one Testing line) |
| `prbody.compose_body` | Frame the final PR body (warning/refs/footer) | Clip the no-body `## Task` fallback |
| `prbody` (new helper) | Pure task→short-summary clip | Add + unit-test |
| `session._finish_pr` | Commit/push/open PR | **No change** |
| artifacts / Slack upload | Screenshots to Slack | **No change** |

## Testing

- `tests/test_session_pr.py` and any prompt-shape assertions updated to the new
  structure (no `## Changes`/`## Testing` requirement; Summary + one-line
  Testing note present in guidance).
- New unit test: the no-body fallback body is short (a long task is clipped;
  a short task passes through unchanged; warning/refs/footer still present).
- Full suite green before merge.

## Delivery

Implemented in a git worktree; on green tests, merged to `master` locally and
pushed to `origin`.
