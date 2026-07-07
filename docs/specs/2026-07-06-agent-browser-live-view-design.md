# Agent-browser live view + same-site workspace login — design

**Date:** 2026-07-06
**Status:** approved (built in `worktree-agent-browser`)

## Goal

Make the `#live=<run_id>` workspace show *the agent at work*, Devin-style: when
the QA agent drives the app in its browser (logging in, navigating, verifying
acceptance criteria), the user watches those exact browser frames live in the
workspace. Also fix the workspace's embedded-app login loop (sign in →
redirected back to sign-in) caused by cross-site iframe cookies.

## Part 1 — live agent-browser streaming

### Approach chosen: shared CDP browser + in-container screencaster

At the start of every browser-QA turn, forge:

1. Prepares `<runs>/<run_id>/workspace/.forge/live/` (host-visible bind mount =
   `/work/.forge/live/` in the worker container) and writes `screencast.cjs`
   into it.
2. `exec_detached`s the screencaster in the worker service (`node` with
   `NODE_PATH=$(npm root -g)` so the image's global playwright resolves).
3. The screencaster spawns the image's baked Chromium
   (`--headless=new --remote-debugging-port=9222 --no-sandbox`), attaches via
   `chromium.connectOverCDP`, and every ~700 ms screenshots the *newest active
   page* to `frame.jpg` (atomic tmp+rename) plus `meta.json`
   (`{url, title, ts, seq}`).
4. The QA prompt tells the agent a shared browser is already running at
   `http://127.0.0.1:9222` — connect with `connectOverCDP` and reuse the open
   page so the teammate can watch; fall back to launching its own browser if
   connecting fails (degrades to no stream, never blocks QA).
5. At turn end forge writes `.forge/live/stop`; the screencaster exits within a
   tick and kills its Chromium (`pkill` does not exist in the image — the stop
   file is the only shutdown path).

Validated live in the `forge-worker` image before implementation: two
`connectOverCDP` clients attach concurrently; the screencaster sees pages the
agent creates **even in fresh browser contexts**; frames stream during
`fill`/`setContent`; the stop file ends everything cleanly.

Rejected alternatives:
- **Prompt-driven step screenshots** (agent saves a PNG after each action):
  slideshow, depends on agent diligence, slows every step.
- **Xvfb + VNC/noVNC**: true interactivity but needs a worker-image rebuild
  (X stack), websockify plumbing, and per-run port publishing — far more moving
  parts than frames-over-HTTP for the same "watch it work" payoff.

### Serving frames (no SSE contract change)

Two new read-only endpoints in `webapp.py` (loopback-only like the rest of the
API):

- `GET /api/sessions/{run_id}/browser` → `{active, ts, url, title}`;
  `active` = frame fresher than ~6 s. 404 for unknown runs.
- `GET /api/sessions/{run_id}/browser/frame` → the JPEG (`Cache-Control:
  no-store`), 404 when no frame exists.

Frames deliberately bypass the EventBus: base64 frames would bloat the replay
buffer and the Slack tap; polling a 30–80 KB local JPEG at ~1 Hz is cheaper and
self-recovering.

### Frontend (workspace `#live=<id>`)

- `useAgentBrowser(runId)` polls the status endpoint every 1.5 s (pauses while
  the tab is hidden).
- While `active`, the left pane auto-switches from the app iframe to the
  **agent view**: latest frame as `<img>` (bumped by `ts`), a "watching the
  agent" toolbar with the page URL. A pin toggle (`auto` behavior default:
  app ⇄ agent) lets the user force either pane; when frames go stale the view
  returns to the app.
- Pure helpers (`agentView.ts`) unit-tested: pane resolution from
  (pin, active) and frame-URL construction.

## Part 2 — workspace login loop (same-site fix)

The workspace page is served from `127.0.0.1:8099` but embeds the app iframe
from `run-<id>.forge.localhost:8088`. Those are different *sites*, so browsers
drop `SameSite=Lax` session cookies inside the iframe: login POSTs succeed but
the session cookie never sticks → redirected back to sign-in.

`localhost` is not on the Public Suffix List, so `forge.localhost` and
`run-<id>.forge.localhost` share the registrable domain `forge.localhost` —
same-site. Fix:

- When the SPA renders the workspace route from `localhost`/`127.0.0.1` and the
  configured `proxy_domain` ends in `.localhost` (the default), it redirects
  once to `http://<proxy_domain>:<same-port>/#live=<id>` (e.g.
  `http://forge.localhost:8099/#live=…`). `*.localhost` always resolves to
  loopback in browsers, so this needs no DNS.
- `public_request_allowed` gains `*.localhost` in its loopback allowlist so the
  API keeps working under the new Host when a public tunnel fronts the daemon
  (same trust model as the literal loopback names — browsers hardcode
  `.localhost` to loopback).

Dashboard (`#s=`) keeps its host — the redirect is scoped to the workspace,
which is the surface built for actually *using* the app.

## Error handling

- `browserview.start/stop` are strictly best-effort: any failure logs and the
  QA turn proceeds without a stream.
- The screencaster skips ticks when a page is mid-navigation, exits on browser
  death, stop file, or a 2 h safety cap; stderr/stdout land in
  `.forge/live/screencast.log` for debugging.
- `.forge/` is already excluded from diffs/PRs (`exclude_forge_scratch`), so
  live frames never leak into commits.

## Testing

- Python: `browserview` unit tests (dir prep, start/stop exec shapes, status
  freshness) with a fake env; endpoint tests via FastAPI TestClient; `_qa`
  wiring test asserting start→stream→stop ordering and stop-on-crash.
- Frontend: vitest for pane gating + frame URL helpers.
- Pre-merge: full `uv run pytest`, `npm test`, `vite build` (dist is tracked).

## Addendum 2026-07-07 — fast path (push screencast + MJPEG)

The v1 pipeline was poll-on-poll: a 700 ms `page.screenshot()` tick × a
1.5 s UI status poll ≈ 0.7 fps with 1.5–2.5 s lag. Both ends replaced:

- **Production:** the screencaster now uses CDP `Page.startScreencast` via
  `context.newCDPSession(page)` — Chromium pushes a JPEG per paint. Every
  frame is acked immediately (no backpressure stall); disk writes are
  throttled to ~12 fps (`MIN_FRAME_MS`). Where screencast setup fails the old
  explicit-screenshot loop remains as fallback. Spike in the forge-worker
  image: ~30 fps delivered while a page is driven, ~2 frames/s static.
- **Liveness:** a static page emits *no* frames, so `active` can no longer key
  off frame mtime alone — the control loop (250 ms) heartbeats `meta.json`
  (`beat`, epoch ms) and `status()` treats fresh-beat-or-fresh-frame as
  active. Without this the workspace pane flaps back to the app whenever the
  agent pauses to think. Live-validated: beat gap ≤ 0.25 s during an 8 s
  static phase, frames correctly silent.
- **Delivery:** `GET /api/sessions/{id}/browser/stream` serves
  `multipart/x-mixed-replace` (MJPEG): `stream_frames()` watches `frame.jpg`
  every 80 ms and pushes each new frame down one long-lived request that the
  `<img>` renders natively. Ends cleanly when the stream goes stale, after a
  3 s grace if no frame ever appears, or at the screencaster's 2 h cap.
  Validated against real uvicorn: first part in 30 ms, no buffering.
- **Frontend:** AgentView tries the stream first and drops to the old
  poll-and-swap `/browser/frame` on `<img>` error; a screencast *epoch*
  (bumped on each inactive→active edge of the status poll, now 1 s) forces a
  fresh MJPEG connection per turn. Net effect: ~10 fps with sub-second
  latency, degrading to v1 behavior when anything in the fast path fails.
