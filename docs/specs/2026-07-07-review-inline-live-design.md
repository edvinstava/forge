# Review: inline comments + live browser check — design

**Date:** 2026-07-07
**Status:** approved (design), pending implementation plan

## Problem

A forge review of a real PR (OSTAL-378) produced a single long review body.
Findings with exact file/line targets — a dead-styling nit at
`product-overview.tsx:102–105`, a scope note — sat in the summary instead of
being anchored to the changed lines, even though `review.py` already supports
inline anchoring and `session_review.py` posts through GitHub's `/reviews`
API. And although the review env provisions a live instance of the PR branch,
the reviewer never drove it: the prompt's "exercise it where it helps" nudge
is weak and the review worker gets no browser tooling (unlike QA turns).

Two gaps, one worker turn to fix both:

1. **Inline-first output** — findings must land as inline PR comments; the
   summary is an overview, not a findings dump.
2. **Live check** — the reviewer drives the PR's app in a real browser,
   derived from what the diff/PR says should change, and the review leads
   with a ✅/❌ verdict on it.

## Decisions (from brainstorming)

- **Approach:** harden the existing single review worker (no second QA-style
  worker, no prompt-only fix).
- **Live-test depth:** browser QA pass — the review worker gets the same
  shared-CDP-Chromium tooling QA turns get, watchable live in the #live
  workspace.
- **Verdict semantics:** stay advisory. Always `event=COMMENT`; a failed live
  check is reported loudly in the body, never `REQUEST_CHANGES`.
- **Triggers unchanged:** web `/api/review`, Slack review intent, and the
  `@edvin-forge review` comment-command all keep working with no interface
  change.

## Design

### 1. Worker prompt + `review.json` schema (`prompts.py`)

`_REVIEW_SCHEMA` is rewritten with inline-first rules:

- Every finding that names a specific file/line MUST be a `comments[]` entry
  anchored to a line in this PR's diff.
- `summary` is a short markdown overview (what was verified, overall
  assessment) — it must not restate findings that live in `comments[]`.
- New required `live_check` object:

```json
{"summary": "<short markdown overview>",
 "live_check": {"status": "pass|fail|skipped|blocked",
                "tested": ["<flow exercised>"],
                "notes": "<what happened / why skipped or blocked>"},
 "comments": [{"path": "<repo-relative>", "line": 1, "side": "RIGHT",
               "body": "<one finding>"}]}
```

`build_review_prompt(slug, number, app_url, credentials=None)` changes:

- When `app_url` is set: include the existing `_SHARED_BROWSER` block and an
  explicit instruction — *derive what this PR should change in the running
  app from `.forge/pr.diff` and the PR description, exercise exactly that in
  the browser, and record it in `live_check`* (`pass` only if the changed
  behavior was observed working; `fail` with specifics if broken).
- Credentials: same rendering as `build_qa_prompt`'s `cred_block`, plus the
  QA guardrail — never guess/brute-force logins; with no working credentials
  at a login wall, set `live_check.status = "blocked"` naming the exact
  role needed, and continue with the static review.
- When `app_url` is None: instruct `live_check.status = "skipped"` with a
  one-line reason.
- Screenshots optional: the worker may capture PNGs under
  `.forge/artifacts/` with the same `manifest.json` format QA uses.
- Advisory framing is unchanged: report, don't approve/block, don't modify
  code.

`build_self_review_prompt` (pre-PR self-review) is untouched; the parser
stays tolerant of its `review.json` without `live_check`.

### 2. Session wiring (`session_review.py`)

`_review_pass` mirrors the QA turn:

- `creds = self._qa_credentials(run_id)` → passed to
  `build_review_prompt(..., credentials=creds)`.
- Stream redaction: `redact=lambda s: redact_secrets(s, secrets)` on
  `_stream_worker`, where `secrets` are the credential passwords (exactly as
  the QA pass does).
- `browserview.start(self.cfg.runs_dir, run_id, env)` before the worker,
  `browserview.stop(...)` in a `finally:` — only when `self._app_url(run_id)`
  is set (same guard as `turn()`). Best-effort on both ends: browserview
  failure never fails the review.
- Reviews stay fire-and-forget: a login wall never pauses for an interactive
  checkpoint; it just reports `blocked` in the posted review.
- `self._reset_artifacts(run_id)` at review start so screenshots from a
  prior turn can't leak into this review's artifact set.

### 3. Anchoring robustness (`review.py`, pure logic)

`partition(review, line_map)` gains a snap step:

- A comment whose `(side, line)` is not in `line_map[path]` tries the
  nearest anchorable line **in the same file** within ±3 lines, preferring
  `RIGHT`-side anchors; ties break toward the smaller distance, then RIGHT.
- On a hit, the comment is re-anchored (new `line`/`side`) and treated as
  valid; on a miss it is demoted to the body as today.
- Pure function, no I/O; snapped comments count as `valid` in the posted
  stats.

### 4. Posted review shape (`review.py::build_payload`)

Body order:

1. Degrade header (unchanged — branding when posting without the GitHub
   App).
2. **Live check** section: `✅ pass` / `❌ fail` / `⏭️ skipped` /
   `🔒 blocked`, the `tested` list as bullets, then `notes`. Omitted
   entirely when `live_check` is absent (old workers, self-review) —
   fully backward compatible.
3. Short summary (as produced by the worker).
4. "Additional notes (couldn't anchor inline)" for demoted comments
   (unchanged).

Screenshots are **not** embedded in the PR body (public-repo raw-URL
limitation; already deliberately deferred). Slack-originated reviews surface
them by adding a `self._post_artifacts(run_id, channel, thread_ts)` call to
`slackbot._run_review` after the review-posted message (one line; helper
already exists and is thread-safe/best-effort).

### 5. Failure modes

- Browser/browserview failure → review completes without the live stream
  (QA precedent, best-effort).
- Worker writes no `live_check` → section omitted; never fails the post.
- Env has no web service → prompt says so; `skipped` with reason.
- Snap finds no anchor → body note, as today.
- Everything else (clone/provision/post failures) unchanged.

### 6. Testing (hermetic, existing patterns)

- `tests/test_review.py` (pure logic): `parse_review` tolerates
  missing/malformed `live_check`; snap anchoring hit/miss/window-edge/side
  preference; `build_payload` body sections in order, live-check emoji per
  status, omission when absent.
- Session-level (FakeHost/FakeRunner): review prompt contains
  `_SHARED_BROWSER` + credentials only when the env has a web service;
  `browserview.start`/`stop` bracket the worker including on worker error;
  secrets redacted from streamed events; artifacts reset at review start.
- Slack: `_run_review` posts artifacts after a successful review.

## Out of scope

- PR-embedded screenshots (deferred; revisit if repos are public).
- `REQUEST_CHANGES`/`APPROVE` verdicts.
- Interactive credential checkpoints during review.
- Deriving formal acceptance criteria from the PR (QA-gate style).
