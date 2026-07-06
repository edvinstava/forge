# Live Workspace — design

**Date:** 2026-07-06
**Status:** approved, ready to implement

## Problem

When Forge finishes (or is mid-build), it hands out a raw link to the running
app. Clicking it drops you into the app alone — you can look, but to change
anything you have to switch back to Slack or the Forge dashboard, describe what
you want, and wait. There's no single surface where you can *watch* the app and
*steer* the agent at the same time.

We want a focused, linkable view: the **running app on the left (~75%)** and the
**Forge agent chat on the right (~25%)**, where you can prompt the agent ("fix
this", "style it differently") and watch the app update live as it works.

## Non-goals

- Not replacing the existing 3-pane dashboard (session list · chat · inspector).
  The dashboard stays exactly as-is for browsing and managing sessions.
- Not embedding screenshots/images in the view (that path stays in Slack; see
  the concise-PR-descriptions deferral).
- Not a draggable pane resizer in v1 (fixed 75/25; resizer is an easy follow-up).

## Shape

A new full-screen SPA view selected by the hash route **`#live=<run_id>`**,
living beside the existing `#s=<run_id>` dashboard route. It is a two-pane
layout — app-left, control-right — with no session sidebar.

```
dashboard (#s=<id>)                     workspace (#live=<id>)
┌ side │ chat │ inspector ┐             ┌ running app (75%) │ control (25%) ┐
│      │      │           │   ──link──▶ │  [iframe preview]  │ [chat|diff|   │
│      │      │           │             │                    │  verify]      │
└──────┴──────┴───────────┘             │                    │ + prompt box  │
                                        └────────────────────┴───────────────┘
```

## Routing

`web/src/deepLink.ts` currently parses `#s=<run_id>`. Extend it to return a
discriminated route:

```ts
type Route =
  | { view: "dashboard"; runId: string | null }
  | { view: "workspace"; runId: string };

parseRoute(hash): Route          // #live=<id> → workspace; #s=<id> / else → dashboard
workspaceHash(runId): string     // "#live=<id>"
```

`web/src/App.tsx` reads the route on load and on `hashchange`. When
`view === "workspace"`, it renders `<Workspace runId={...} />` full-screen;
otherwise the existing dashboard (`app-shell` grid) renders unchanged. Existing
`#s=` behaviour and `sessionHash()` are untouched.

## Left pane — `AppFrame`

Extracted from today's `Inspector.PreviewTab` so the dashboard preview tab and
the workspace left pane share **one** implementation. Responsibilities:

1. **Toolbar:** shown URL · `↻` manual refresh · `↗` open in new tab · `🌐`
   public share link (when it differs from the embedded src).

2. **Embed-URL resolution (fixes a real remote-viewer bug).** Today the preview
   always prefers `run-<id>.forge.localhost`, which only resolves *on the forge
   host*. A viewer who opened the workspace from a Slack link (remote) cannot
   resolve it, so the pane would always fail to the fallback card. `AppFrame`
   picks the src by where the page itself was loaded from:
   - page loaded from `localhost` / `127.0.0.1` / `*.forge.localhost`
     → prefer `localUrl` (DNS-free, reliable on the host);
   - page loaded from a public tunnel host → prefer `publicUrl`.

   This is a pure function `resolveEmbedSrc({ locationHostname, webUrl, localUrl })`
   → `{ src, share }`, unit-tested in isolation.

3. **Frame-failure fallback (two layers).** On iframe load failure or a ~6s
   timeout, try embedding the *other* URL once; only if that also fails show the
   existing "can't be embedded (X-Frame-Options / CSP)" card with open-in-new-tab
   + public-link buttons.

4. **Soft-nudge auto-refresh.** Dev-server HMR (Next/Vite) live-updates the
   iframe between turns on its own. On top of that, when a turn completes the
   pane shows an "↗ updated · refreshing…" pill and reloads the iframe **once**
   after a ~1.5s settle delay (bumping a `reloadKey` on the iframe). It never
   force-reloads mid-turn. Driven by a `reloadSignal` prop (a counter the parent
   increments on turn-done).

Props: `{ webUrl, localUrl, reloadSignal, onManualRefresh? }` — one clear
purpose, no session/chat knowledge.

## Right pane — control surface

- Small tab strip: `[ chat | diff | verify ]`, **chat** default.
- **chat:** the existing `<Chat>` component, reused verbatim. It already streams
  the live agent turn and has the follow-up prompt box (task/chat modes). Its
  `onTurnDone` callback is wired to increment the `reloadSignal` passed to
  `AppFrame` — that's the "watch it change live" loop. Its `onUrl` callback keeps
  the embedded `webUrl` fresh (e.g. after a wake/restart changes the app URL).
- **diff / verify:** `DiffView` / `VerifyView`, extracted from `Inspector`,
  collapsed behind tabs so code changes are one click away without stealing focus
  from steering.

## Backend — the Slack link

- `src/forge/slackmsg.py`: add `web_workspace_link(base_url, run_id)` →
  `{base}/#live=<run_id>` (mirrors the existing `web_session_link`; returns `""`
  when no base configured).
- `src/forge/slackbot.py`:
  - populate `state["workspace_url"]` wherever `forge_url` is set (via
    `web_workspace_link(self.cfg.forge_web_url, run_id)`);
  - in `_url_lines`, add a **`🗔`** workspace line.

**Link-hygiene principle:** when a run has a live app, the `🗔` workspace link is
the "richer surface" link (it embeds the app *and* carries chat/diff/verify), so
it supersedes the `🧭` dashboard-session line **on messages that have a live
app**. The `🧭` line stays for QA/answer messages with no live app. Net live
message lines: `🌐 <app>` · `🏠 <local>` · `🗔 <workspace>`.

No new auth surface: the workspace is served by the same Forge web app as `#s=`
and calls the same APIs, so it rides on the existing `forge_web_url` reachability
that dashboard deep links already depend on.

## Refactor (code touched anyway)

`web/src/Inspector.tsx` (~470 lines) mixes diff parsing with three tab bodies.
Split into reusable units the workspace can compose:

- `web/src/diffModel.ts` — pure `parseDiff` + `DiffFile`/`DiffRow` types (move
  the existing `diff.test.ts` target here).
- `web/src/AppFrame.tsx` — the iframe embed (§ left pane).
- `web/src/DiffView.tsx` — the diff panel presentation.
- `web/src/VerifyView.tsx` — the verify panel presentation.

`Inspector.tsx` then composes `AppFrame` + `DiffView` + `VerifyView`; no logic is
forked between dashboard and workspace.

## Testing

Frontend (vitest):
- `deepLink.test.ts` — `parseRoute` for `#live=<id>`, `#s=<id>`, empty; round-trip
  `workspaceHash`.
- `AppFrame` embed resolver — `resolveEmbedSrc` picks `localUrl` for a
  local-host page and `publicUrl` for a tunnel-host page; `share` set correctly.
- Existing `diff.test.ts` re-pointed at `diffModel.ts` still passes.

Backend (pytest):
- `web_workspace_link` — builds `#live=<id>`; `""` on empty base.
- `_url_lines` — emits the `🗔` line when `workspace_url` present; omits `🧭`
  when a live-app workspace link is present, keeps `🧭` for no-app messages.

## Known limitation

Apps that hard-block framing (`X-Frame-Options: DENY`, CSP
`frame-ancestors 'none'`) can't be embedded; the left pane degrades to the
open-in-new-tab card. Next.js dev — the common Forge case — works because Forge
already injects `allowedDevOrigins` (see `nextdev.py`).

## Build sequence

1. `diffModel.ts` extraction + re-point `diff.test.ts` (pure, no behaviour change).
2. `DiffView.tsx` / `VerifyView.tsx` extraction; `Inspector` composes them.
3. `AppFrame.tsx` extraction + `resolveEmbedSrc` + soft-nudge reload; `Inspector`
   preview tab composes it.
4. `deepLink.ts` route discriminant + tests.
5. `Workspace.tsx` (75/25 layout, tabs, wiring) + `App.tsx` route switch.
6. "🗔 open workspace" button in dashboard (App/SessionRow).
7. Backend `web_workspace_link` + `_url_lines` 🗔 line + tests.
8. Verify end-to-end; merge.
