# Live Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a focused, deep-linkable web view (`#live=<run_id>`) pairing the running app (~75%, left) with the Forge agent chat (~25%, right), so you can watch the app update live while steering the agent, and link Slack straight to it.

**Architecture:** Extract the reusable pieces out of `Inspector.tsx` (diff model, diff/verify panels, the app-preview iframe) so both the existing dashboard and a new `Workspace` view compose the same code. `App.tsx` route-switches on a hash discriminant. The backend gains a `#live=` link helper wired into the Slack live-app message.

**Tech Stack:** React 18 + TypeScript + Vite + Vitest (web/); Python 3.11 + pytest (src/forge/). No new dependencies.

## Global Constraints

- No new npm or Python dependencies.
- Existing `#s=<run_id>` dashboard route and `sessionHash()` behaviour must remain unchanged.
- The dashboard 3-pane layout (`app-shell` grid) must render exactly as before.
- Reuse the existing `Chat` component verbatim in the workspace (no fork).
- Run web tests with `npm test` (from `web/`); Python tests with `python3 -m pytest` (from repo root). `python` is not on PATH — always `python3`.
- Slack link glyph for the workspace is `🗔`; keep `🌐` (public app), `🏠` (local), `🧭` (dashboard session) semantics intact.
- Commit after every task (frequent commits).

---

### Task 1: Extract `diffModel.ts` (pure diff parser)

Move the diff types + `parseDiff` out of `Inspector.tsx` into a standalone pure module so `DiffView` and any consumer import from one place. No behaviour change.

**Files:**
- Create: `web/src/diffModel.ts`
- Modify: `web/src/Inspector.tsx` (remove the moved code; import from `./diffModel`)
- Modify: `web/src/diff.test.ts:2` (import `parseDiff` from `./diffModel` instead of `./Inspector`)

**Interfaces:**
- Produces: `export type RowType = "ctx" | "add" | "del" | "hunk"`; `export interface DiffRow { type: RowType; oldNo: number | null; newNo: number | null; text: string }`; `export interface DiffFile { path: string; status: "added" | "deleted" | "renamed" | "modified"; additions: number; deletions: number; rows: DiffRow[] }`; `export function parseDiff(raw: string): DiffFile[]`; `export const STATUS_GLYPH: Record<DiffFile["status"], string>`.

- [ ] **Step 1: Re-point the existing test to the new module**

In `web/src/diff.test.ts` line 2, change:
```ts
import { parseDiff } from "./diffModel";
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- diff.test.ts`
Expected: FAIL — `Failed to resolve import "./diffModel"` (module doesn't exist yet).

- [ ] **Step 3: Create `web/src/diffModel.ts`**

Move verbatim from `Inspector.tsx`: the `RowType`, `DiffRow`, `DiffFile` types, the `parseFile` helper, `parseDiff`, and the `STATUS_GLYPH` constant. Export the types, `parseDiff`, and `STATUS_GLYPH`. `parseFile` stays module-private (not exported).

```ts
export type RowType = "ctx" | "add" | "del" | "hunk";

export interface DiffRow {
  type: RowType;
  oldNo: number | null;
  newNo: number | null;
  text: string;
}

export interface DiffFile {
  path: string;
  status: "added" | "deleted" | "renamed" | "modified";
  additions: number;
  deletions: number;
  rows: DiffRow[];
}

function parseFile(chunk: string): DiffFile {
  /* …move the exact body from Inspector.tsx parseFile (lines 37–102)… */
}

export function parseDiff(raw: string): DiffFile[] {
  if (!raw.trim()) return [];
  return raw
    .split(/^(?=diff --git )/m)
    .filter((c) => c.trim())
    .map(parseFile);
}

export const STATUS_GLYPH: Record<DiffFile["status"], string> = {
  added: "A",
  deleted: "D",
  renamed: "R",
  modified: "M",
};
```

- [ ] **Step 4: Update `Inspector.tsx` to import from the new module**

Remove the moved type/function/constant definitions from `Inspector.tsx`. At the top, add:
```ts
import { parseDiff, STATUS_GLYPH, type DiffFile } from "./diffModel";
```
Delete the now-unused re-export path: `diff.test.ts` no longer imports from `Inspector`, so `Inspector` no longer needs to export `parseDiff`. Keep `DiffFileView` (it uses `DiffFile` + `STATUS_GLYPH`) in `Inspector.tsx` for now — it moves in Task 2.

- [ ] **Step 5: Run the diff test + typecheck**

Run: `npm test -- diff.test.ts`
Expected: PASS (4 tests).
Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/diffModel.ts web/src/Inspector.tsx web/src/diff.test.ts
git commit -m "refactor(web): extract pure diff parser into diffModel.ts"
```

---

### Task 2: Extract `DiffView.tsx` and `VerifyView.tsx` (presentational panels)

Pull the diff-tab and verify-tab bodies out of `Inspector.tsx` into standalone components the workspace can reuse. `Inspector` composes them.

**Files:**
- Create: `web/src/DiffView.tsx`
- Create: `web/src/VerifyView.tsx`
- Modify: `web/src/Inspector.tsx` (delete `DiffFileView`, `DiffTab`, `VerifyTab`; import + render the new components)

**Interfaces:**
- Consumes: `parseDiff`, `STATUS_GLYPH`, `DiffFile` from `./diffModel`; `getDiff`, `getVerify` from `./api`; `VerifyResult` from `./types`.
- Produces: `export function DiffView({ activeId }: { activeId: string }): JSX.Element`; `export function VerifyView({ activeId }: { activeId: string }): JSX.Element`.

- [ ] **Step 1: Create `web/src/DiffView.tsx`**

Move `DiffFileView` (Inspector.tsx lines 119–156) and the `DiffTab` body (lines 159–217) into this file. Rename the exported component `DiffTab` → `DiffView`. Keep all CSS class names identical. Imports:
```ts
import { useState, useEffect, useCallback } from "react";
import { getDiff } from "./api";
import { parseDiff, STATUS_GLYPH, type DiffFile } from "./diffModel";
```
Export: `export function DiffView({ activeId }: { activeId: string }) { … }` (same body as the old `DiffTab`). `DiffFileView` stays module-private.

- [ ] **Step 2: Create `web/src/VerifyView.tsx`**

Move the `VerifyTab` body (Inspector.tsx lines 220–317) into this file, renamed `VerifyTab` → `VerifyView`. Imports:
```ts
import { useState, useEffect, useCallback } from "react";
import type { VerifyResult } from "./types";
import { getVerify } from "./api";
```
Export: `export function VerifyView({ activeId }: { activeId: string }) { … }`.

- [ ] **Step 3: Update `Inspector.tsx` to compose them**

Delete `DiffFileView`, `DiffTab`, `VerifyTab` from `Inspector.tsx`. Add imports:
```ts
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";
```
In the render, replace `<DiffTab activeId={activeId} />` → `<DiffView activeId={activeId} />` and `<VerifyTab activeId={activeId} />` → `<VerifyView activeId={activeId} />`.

- [ ] **Step 4: Run the full web suite + typecheck**

Run: `npm test`
Expected: PASS (81 tests — unchanged; these are pure moves).
Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/DiffView.tsx web/src/VerifyView.tsx web/src/Inspector.tsx
git commit -m "refactor(web): extract DiffView and VerifyView from Inspector"
```

---

### Task 3: Extract `AppFrame.tsx` with `resolveEmbedSrc` + soft-nudge reload

Move the app-preview iframe out of `Inspector.PreviewTab` into a reusable `AppFrame`. Add a pure `resolveEmbedSrc` that picks the embed URL by the page's own host (fixing the remote-viewer bug), and a `reloadSignal`-driven soft-nudge auto-refresh. `Inspector` composes `AppFrame`.

**Files:**
- Create: `web/src/AppFrame.tsx`
- Create: `web/src/AppFrame.test.ts`
- Modify: `web/src/Inspector.tsx` (delete `PreviewTab`; render `<AppFrame …/>`; drop the now-internal `FrameState` plumbing it no longer owns)

**Interfaces:**
- Produces:
  - `export function resolveEmbedSrc(args: { locationHostname: string; webUrl: string | null; localUrl: string | null }): { src: string | null; share: string | null }`
  - `export function AppFrame(props: { webUrl: string | null; localUrl: string | null; reloadSignal?: number }): JSX.Element`
- Consumes (Task 5): `AppFrame` is rendered by `Workspace`; `resolveEmbedSrc` is unit-tested directly.

- [ ] **Step 1: Write the failing test for `resolveEmbedSrc`**

Create `web/src/AppFrame.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { resolveEmbedSrc } from "./AppFrame";

const PUBLIC = "https://demo.trycloudflare.com";
const LOCAL = "http://run-1.forge.localhost:8088";

describe("resolveEmbedSrc", () => {
  it("prefers the local URL when the page is served locally", () => {
    for (const host of ["localhost", "127.0.0.1", "forge.localhost"]) {
      const { src, share } = resolveEmbedSrc({
        locationHostname: host, webUrl: PUBLIC, localUrl: LOCAL });
      expect(src).toBe(LOCAL);
      expect(share).toBe(PUBLIC); // public link still offered for opening out
    }
  });

  it("prefers the public URL when the page is served from a tunnel host", () => {
    const { src, share } = resolveEmbedSrc({
      locationHostname: "forge.example.com", webUrl: PUBLIC, localUrl: LOCAL });
    expect(src).toBe(PUBLIC);
    expect(share).toBeNull(); // src already is the public URL
  });

  it("falls back to whichever URL exists", () => {
    expect(resolveEmbedSrc({ locationHostname: "localhost", webUrl: PUBLIC, localUrl: null }).src).toBe(PUBLIC);
    expect(resolveEmbedSrc({ locationHostname: "forge.example.com", webUrl: null, localUrl: LOCAL }).src).toBe(LOCAL);
    expect(resolveEmbedSrc({ locationHostname: "localhost", webUrl: null, localUrl: null }).src).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- AppFrame.test.ts`
Expected: FAIL — `Failed to resolve import "./AppFrame"`.

- [ ] **Step 3: Create `web/src/AppFrame.tsx`**

```tsx
import { useState, useEffect, useRef } from "react";

/** True when the SPA itself is being served from the forge host (so the
 *  DNS-free run-<id>.forge.localhost URL resolves here). A remote viewer who
 *  opened the workspace from a Slack link is NOT local, so they must embed the
 *  public tunnel URL instead. */
function isLocalHost(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname.endsWith(".localhost") ||
    hostname === "forge.localhost"
  );
}

/** Pick the iframe src by where THIS page loaded from, plus a public share link
 *  to offer when the embedded src isn't already the public URL. Pure. */
export function resolveEmbedSrc(args: {
  locationHostname: string;
  webUrl: string | null;
  localUrl: string | null;
}): { src: string | null; share: string | null } {
  const { locationHostname, webUrl, localUrl } = args;
  const preferLocal = isLocalHost(locationHostname);
  const src = (preferLocal ? localUrl ?? webUrl : webUrl ?? localUrl) ?? null;
  const share = webUrl && webUrl !== src ? webUrl : null;
  return { src, share };
}

type FrameState = "pending" | "ok" | "failed";

export function AppFrame({
  webUrl,
  localUrl,
  reloadSignal = 0,
}: {
  webUrl: string | null;
  localUrl: string | null;
  reloadSignal?: number;
}) {
  const { src, share } = resolveEmbedSrc({
    locationHostname: window.location.hostname,
    webUrl,
    localUrl,
  });

  const [frameState, setFrameState] = useState<FrameState>("pending");
  // `reloadKey` bumps to force a fresh iframe load; `triedAlt` tracks the
  // one-shot fallback to the other URL after a load failure.
  const [reloadKey, setReloadKey] = useState(0);
  const [effectiveSrc, setEffectiveSrc] = useState(src);
  const [triedAlt, setTriedAlt] = useState(false);
  const [nudge, setNudge] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nudgeRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstSignal = useRef(reloadSignal);

  // Reset when the resolved src changes (new session / URL surfaced).
  useEffect(() => {
    setEffectiveSrc(src);
    setTriedAlt(false);
    setFrameState("pending");
  }, [src]);

  // Soft-nudge auto-refresh: on a turn-completion signal (not the initial
  // value), show the pill and reload the iframe ONCE after a settle delay.
  useEffect(() => {
    if (reloadSignal === firstSignal.current) return;
    if (!effectiveSrc) return;
    setNudge(true);
    nudgeRef.current = setTimeout(() => {
      setReloadKey((k) => k + 1);
      setFrameState("pending");
      setNudge(false);
    }, 1500);
    return () => {
      if (nudgeRef.current) clearTimeout(nudgeRef.current);
    };
  }, [reloadSignal, effectiveSrc]);

  // Load-failure timeout (6s), with a one-shot swap to the alternate URL.
  useEffect(() => {
    if (effectiveSrc && frameState === "pending") {
      timeoutRef.current = setTimeout(() => handleFail(), 6000);
    }
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveSrc, frameState, reloadKey]);

  const alt = share && share !== effectiveSrc ? share : null;

  function handleFail() {
    if (!triedAlt && alt) {
      setTriedAlt(true);
      setEffectiveSrc(alt);
      setFrameState("pending");
      return;
    }
    setFrameState("failed");
  }

  const handleLoad = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    if (frameState === "pending") setFrameState("ok");
  };

  const manualRefresh = () => {
    setReloadKey((k) => k + 1);
    setFrameState("pending");
  };

  if (!effectiveSrc) {
    return (
      <div className="inspector-empty">
        <div className="inspector-empty-icon">◻</div>
        <div className="inspector-empty-label">no web service</div>
        <div className="inspector-empty-hint">starts once the app is live</div>
      </div>
    );
  }

  if (frameState === "failed") {
    return (
      <div className="preview-fallback">
        <div className="preview-fallback-icon">⃠</div>
        <div className="preview-fallback-msg">
          This app can’t be embedded
          <span className="preview-fallback-note">(blocked by X-Frame-Options / CSP)</span>
        </div>
        <a href={effectiveSrc} target="_blank" rel="noopener noreferrer" className="btn btn-accent">
          open app in new tab ↗
        </a>
        {share && (
          <a href={share} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Public share link">
            🌐 public link ↗
          </a>
        )}
      </div>
    );
  }

  return (
    <div className="preview-wrap">
      <div className="preview-toolbar">
        <span className="preview-dot" />
        <span className="preview-url" title={effectiveSrc}>
          {effectiveSrc.replace(/^https?:\/\//, "")}
        </span>
        {nudge && <span className="preview-nudge">↗ updated · refreshing…</span>}
        <button className="btn btn-sm" onClick={manualRefresh} title="Reload">↻</button>
        <a href={effectiveSrc} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Open in new tab">↗</a>
        {share && (
          <a href={share} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Public share link (no DNS needed locally)">🌐</a>
        )}
      </div>
      <iframe
        key={reloadKey}
        src={effectiveSrc}
        className="preview-iframe"
        title="App preview"
        onLoad={handleLoad}
        onError={handleFail}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"
      />
    </div>
  );
}
```

- [ ] **Step 4: Run the resolver test to verify it passes**

Run: `npm test -- AppFrame.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Replace `PreviewTab` in `Inspector.tsx` with `AppFrame`**

Delete `PreviewTab` (Inspector.tsx lines 319–410) and the `FrameState` type + `frameState` state + the `useEffect` that resets it (lines ~414–424 relating to preview). Keep the tab bar. Import and render `AppFrame`:
```ts
import { AppFrame } from "./AppFrame";
```
`previewUrl` (for enabling the tab + choosing default tab) stays: `const previewUrl = localUrl ?? webUrl;`. Render:
```tsx
{tab === "preview" && <AppFrame webUrl={webUrl} localUrl={localUrl} />}
```
(The Inspector doesn't pass `reloadSignal` — HMR + manual `↻` cover it there; the workspace drives the nudge.)

- [ ] **Step 6: Add the nudge pill style**

Append to `web/src/styles.css` near `.preview-url` (after line ~1658):
```css
.preview-nudge {
  font-size: var(--text-xs);
  color: var(--text-accent, #6ea8fe);
  white-space: nowrap;
  opacity: 0.9;
}
```

- [ ] **Step 7: Run full suite + typecheck**

Run: `npm test`
Expected: PASS (84 tests: 81 + 3 new).
Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add web/src/AppFrame.tsx web/src/AppFrame.test.ts web/src/Inspector.tsx web/src/styles.css
git commit -m "refactor(web): extract AppFrame with host-aware embed + soft reload"
```

---

### Task 4: Route discriminant in `deepLink.ts`

Add `#live=<run_id>` parsing beside `#s=<run_id>`, returning a discriminated route the App switches on.

**Files:**
- Modify: `web/src/deepLink.ts`
- Modify: `web/src/deepLink.test.ts`

**Interfaces:**
- Produces: `export type Route = { view: "dashboard"; runId: string | null } | { view: "workspace"; runId: string }`; `export function parseRoute(hash: string): Route`; `export function workspaceHash(runId: string): string`. (`parseSessionHash`/`sessionHash` stay unchanged.)

- [ ] **Step 1: Add failing tests**

Append to `web/src/deepLink.test.ts`:
```ts
import { parseRoute, workspaceHash } from "./deepLink";

describe("parseRoute", () => {
  it("routes #live=<id> to the workspace", () => {
    expect(parseRoute("#live=abc123")).toEqual({ view: "workspace", runId: "abc123" });
  });
  it("routes #s=<id> to the dashboard with the id", () => {
    expect(parseRoute("#s=abc123")).toEqual({ view: "dashboard", runId: "abc123" });
  });
  it("routes anything else to the dashboard with no id", () => {
    expect(parseRoute("")).toEqual({ view: "dashboard", runId: null });
    expect(parseRoute("#live=")).toEqual({ view: "dashboard", runId: null });
    expect(parseRoute("#live=<script>")).toEqual({ view: "dashboard", runId: null });
  });
  it("round-trips workspaceHash", () => {
    expect(parseRoute(workspaceHash("run-1"))).toEqual({ view: "workspace", runId: "run-1" });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npm test -- deepLink.test.ts`
Expected: FAIL — `parseRoute is not a function` / import error.

- [ ] **Step 3: Implement in `deepLink.ts`**

Append (keep existing exports):
```ts
export type Route =
  | { view: "dashboard"; runId: string | null }
  | { view: "workspace"; runId: string };

export function parseRoute(hash: string): Route {
  const live = /^#live=([A-Za-z0-9_-]+)$/.exec(hash || "");
  if (live) return { view: "workspace", runId: live[1] };
  return { view: "dashboard", runId: parseSessionHash(hash) };
}

export function workspaceHash(runId: string): string {
  return `#live=${runId}`;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `npm test -- deepLink.test.ts`
Expected: PASS (existing 3 + new 4).

- [ ] **Step 5: Commit**

```bash
git add web/src/deepLink.ts web/src/deepLink.test.ts
git commit -m "feat(web): add #live=<id> workspace route discriminant"
```

---

### Task 5: `Workspace.tsx` + `App.tsx` route switch + layout CSS

Build the two-pane workspace and switch the App between dashboard and workspace on the hash route.

**Files:**
- Create: `web/src/Workspace.tsx`
- Modify: `web/src/App.tsx` (route switch)
- Modify: `web/src/styles.css` (workspace grid + tabs)

**Interfaces:**
- Consumes: `AppFrame` (Task 3), `Chat` (`web/src/Chat.tsx`, props `{ sessionId, onUrl?, onTurnDone? }`), `DiffView`/`VerifyView` (Task 2), `parseRoute` (Task 4), `getConfig`/`getSession` (`web/src/api.ts`), `localPreviewUrl` (`web/src/webUrl.ts`), `ProxyConfig` (`web/src/types.ts`).
- Produces: `export function Workspace({ runId }: { runId: string }): JSX.Element`.

- [ ] **Step 1: Create `web/src/Workspace.tsx`**

```tsx
import { useState, useEffect, useCallback } from "react";
import { AppFrame } from "./AppFrame";
import { Chat } from "./Chat";
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";
import { getConfig, getSession } from "./api";
import { localPreviewUrl } from "./webUrl";
import type { ProxyConfig } from "./types";

type Tab = "chat" | "diff" | "verify";

export function Workspace({ runId }: { runId: string }) {
  const [webUrl, setWebUrl] = useState<string | null>(null);
  const [proxyConfig, setProxyConfig] = useState<ProxyConfig | null>(null);
  const [reloadSignal, setReloadSignal] = useState(0);
  const [tab, setTab] = useState<Tab>("chat");

  useEffect(() => {
    getConfig().then(setProxyConfig).catch(() => {});
  }, []);

  // Resolve the app URL from the persisted session on mount and whenever a
  // turn completes (a wake/restart can change it).
  const refreshUrl = useCallback(() => {
    getSession(runId)
      .then((s) => setWebUrl((prev) => s.web_url ?? prev))
      .catch(() => {});
  }, [runId]);
  useEffect(() => { refreshUrl(); }, [refreshUrl]);

  const localUrl = localPreviewUrl(runId, webUrl, proxyConfig);

  const handleUrl = useCallback((url: string) => setWebUrl(url), []);
  const handleTurnDone = useCallback(() => {
    setReloadSignal((n) => n + 1);
    refreshUrl();
  }, [refreshUrl]);

  const TABS: { id: Tab; label: string }[] = [
    { id: "chat", label: "chat" },
    { id: "diff", label: "diff" },
    { id: "verify", label: "verify" },
  ];

  return (
    <div className="workspace-shell">
      <section className="workspace-app">
        <AppFrame webUrl={webUrl} localUrl={localUrl} reloadSignal={reloadSignal} />
      </section>
      <aside className="workspace-control">
        <div className="inspector-tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`inspector-tab${tab === t.id ? " is-active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
          <a className="inspector-tab workspace-exit" href="#" title="Back to dashboard">▸ dashboard</a>
        </div>
        <div className="workspace-control-body">
          {/* Chat stays mounted across tab switches so the live stream isn't
              interrupted; diff/verify overlay it. */}
          <div style={{ display: tab === "chat" ? "flex" : "none", flex: 1, minHeight: 0, flexDirection: "column" }}>
            <Chat key={runId} sessionId={runId} onUrl={handleUrl} onTurnDone={handleTurnDone} />
          </div>
          {tab === "diff" && <DiffView activeId={runId} />}
          {tab === "verify" && <VerifyView activeId={runId} />}
        </div>
      </aside>
    </div>
  );
}
```

Note: the `▸ dashboard` link uses `href="#"` — clicking clears the hash, which `App`'s `hashchange` handler resolves to the dashboard route (Step 2 makes `App` react to route changes).

- [ ] **Step 2: Route-switch in `App.tsx`**

Keep the existing `parseSessionHash, sessionHash` import; add the new symbols:
```ts
import { parseSessionHash, sessionHash, parseRoute } from "./deepLink";
import { Workspace } from "./Workspace";
```
Add `route` state, initialised from the current hash, alongside the existing `activeId` state (after line 29):
```ts
const [route, setRoute] = useState(() => parseRoute(window.location.hash));
```
Replace the existing `hashchange` effect (lines 37–44) — the one that only handled `#s=` — with a single effect that updates both the route and (for dashboard deep links) the active id:
```ts
useEffect(() => {
  const onHash = () => {
    const r = parseRoute(window.location.hash);
    setRoute(r);
    if (r.view === "dashboard" && r.runId) setActiveId(r.runId);
  };
  window.addEventListener("hashchange", onHash);
  return () => window.removeEventListener("hashchange", onHash);
}, []);
```
`activate()` (lines 32–35) still writes `sessionHash(runId)` — unchanged — so selecting a session keeps deep links working; that `replaceState` won't fire `hashchange`, which is fine because `activate` already set `activeId`. At the very top of the returned JSX (before `return (<div className="app-shell">`), short-circuit to the workspace:
```tsx
if (route.view === "workspace") {
  return <Workspace runId={route.runId} />;
}
```
Leave the existing dashboard render untouched below it.

- [ ] **Step 3: Add workspace CSS**

Append to `web/src/styles.css`:
```css
/* ── Live workspace: running app (75%) + control (25%) ── */
.workspace-shell {
  display: grid;
  grid-template-columns: 3fr 1fr;
  grid-template-rows: 100vh;
  height: 100vh;
  overflow: hidden;
}
.workspace-app {
  grid-column: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--bg-base);
}
.workspace-app .preview-wrap { flex: 1; min-height: 0; }
.workspace-control {
  grid-column: 2;
  border-left: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-width: 320px;
}
.workspace-control-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.workspace-exit {
  margin-left: auto;
  text-decoration: none;
  opacity: 0.7;
}
@media (max-width: 720px) {
  .workspace-shell { grid-template-columns: 1fr; grid-template-rows: 60vh 40vh; }
  .workspace-control { grid-column: 1; border-left: none; border-top: 1px solid var(--border-subtle); }
}
```

- [ ] **Step 4: Manual smoke via build + typecheck**

Run: `npx tsc --noEmit`
Expected: no errors.
Run: `npm run build`
Expected: build succeeds (bundles `Workspace`).

- [ ] **Step 5: Run full web suite**

Run: `npm test`
Expected: PASS (88 tests).

- [ ] **Step 6: Commit**

```bash
git add web/src/Workspace.tsx web/src/App.tsx web/src/styles.css
git commit -m "feat(web): live workspace view (app-left, chat-right) on #live=<id>"
```

---

### Task 6: "🗔 open workspace" button in the dashboard

Give the dashboard a way into the workspace for any live session.

**Files:**
- Modify: `web/src/Inspector.tsx` (add an "open workspace" action in the preview toolbar area, shown when there's a live app)
- Modify: `web/src/App.tsx` (pass `activeId` through so the Inspector can build the link) — only if not already available.

**Interfaces:**
- Consumes: `workspaceHash` (Task 4).

- [ ] **Step 1: Add the link in `Inspector.tsx`**

Import: `import { workspaceHash } from "./deepLink";`. In the Inspector tab bar (after the `TABS.map(...)`), when `previewUrl` is truthy and `activeId` is set, render an anchor that opens the workspace in the same tab:
```tsx
{previewUrl && (
  <a
    className="inspector-tab workspace-open"
    href={workspaceHash(activeId)}
    title="Open the live workspace (app + chat side by side)"
  >
    🗔 workspace
  </a>
)}
```

- [ ] **Step 2: Add a subtle style**

Append to `web/src/styles.css`:
```css
.workspace-open { margin-left: auto; text-decoration: none; }
```

- [ ] **Step 3: Typecheck + build + test**

Run: `npx tsc --noEmit` → no errors.
Run: `npm test` → PASS (88 tests, unchanged).

- [ ] **Step 4: Commit**

```bash
git add web/src/Inspector.tsx web/src/styles.css
git commit -m "feat(web): add 'open workspace' link to the dashboard inspector"
```

---

### Task 7: Backend — `web_workspace_link` + Slack `🗔` line

Add the workspace deep-link helper and surface it in the Slack live-app message, superseding the `🧭` dashboard line when a live app is present.

**Files:**
- Modify: `src/forge/slackmsg.py` (add `web_workspace_link`)
- Modify: `src/forge/slackbot.py` (`_workspace_link`, set `workspace_url` in state, update `_url_lines`)
- Modify: `tests/test_slackmsg.py` (test `web_workspace_link`)
- Modify: `tests/test_slackbot.py` (test the `🗔` line + `🧭` suppression)

**Interfaces:**
- Consumes: existing `web_session_link` pattern; `self.cfg.forge_web_url`.
- Produces: `def web_workspace_link(base_url: str, run_id: str) -> str` (returns `""` on empty base).

- [ ] **Step 1: Failing test for `web_workspace_link`**

Append to `tests/test_slackmsg.py` (near the deep_link tests):
```python
def test_web_workspace_link_builds_live_hash():
    from forge.slackmsg import web_workspace_link
    assert web_workspace_link("https://forge.example.com", "run-1") \
        == "https://forge.example.com/#live=run-1"

def test_web_workspace_link_empty_base_is_blank():
    from forge.slackmsg import web_workspace_link
    assert web_workspace_link("", "run-1") == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_slackmsg.py -k web_workspace_link -q`
Expected: FAIL — `ImportError: cannot import name 'web_workspace_link'`.

- [ ] **Step 3: Implement `web_workspace_link` in `slackmsg.py`**

Add directly below `web_session_link` (after line 345):
```python
def web_workspace_link(base_url: str, run_id: str) -> str:
    """Deep link to the live workspace (running app + agent chat, side by side)
    in the forge web app ('' when no base is configured). The SPA resolves
    #live=<run_id> to the workspace view on load."""
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/#live={run_id}"
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_slackmsg.py -k web_workspace_link -q`
Expected: PASS (2).

- [ ] **Step 5: Failing test for the `🗔` line + `🧭` suppression**

Append to `tests/test_slackbot.py`:
```python
def test_url_event_surfaces_workspace_link_and_hides_dashboard(tmp_path):
    # With a configured forge_web_url and a live app, the live message shows the
    # 🗔 workspace link (the richer surface) and drops the 🧭 dashboard line.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(start_events=[
        TE("url", web_url="https://demo.trycloudflare.com",
           local_url="http://run-1.forge.localhost:8088"),
    ])
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.cfg.forge_web_url = "https://forge.example.com"
    bot.handle_message("D1", "U1", "fix the landing page repo")
    joined = "\n".join(u.text for u in client.updates)
    assert "🗔 https://forge.example.com/#live=run-1" in joined
    assert "🧭" not in joined  # workspace supersedes the dashboard link here
```

- [ ] **Step 6: Run to verify failure**

Run: `python3 -m pytest tests/test_slackbot.py -k workspace_link_and_hides -q`
Expected: FAIL — `🗔` not found (line not emitted yet).

- [ ] **Step 7: Implement in `slackbot.py`**

7a. Import the helper — extend the existing import (line 15 area):
```python
from forge.slackmsg import (greeting_head, qa_head, clean_summary, deep_link,
                            truncate_for_slack, web_session_link,
                            web_workspace_link, ...)  # keep the rest as-is
```

7b. Add a helper beside `_session_link` (after line 735):
```python
def _workspace_link(self, run_id) -> str:
    """Deep link to the live workspace (app + chat) in the forge web app."""
    return web_workspace_link(getattr(self.cfg, "forge_web_url", ""), run_id)
```

7c. In the `url`-event branch, right after `state["local_url"] = d.get("local_url")` (line 593), also set the workspace link (`run_id` is in scope — it's the first arg to `self.tunnel.start(...)` on line 591):
```python
state["workspace_url"] = self._workspace_link(run_id)
```

7d. Update `_url_lines` (lines 744–751) so the workspace link supersedes the dashboard line when present:
```python
@staticmethod
def _url_lines(state):
    """The public tunnel link (share it) plus, when present, the local
    *.forge.localhost link that opens on the forge host with no external DNS.
    When a live app has a workspace link, 🗔 (app + chat side by side) is the
    richer surface and supersedes the 🧭 dashboard-session line; 🧭 remains for
    messages with no live app."""
    lines = []
    if state.get("public_url"):
        lines.append(f"🌐 {state['public_url']}")
    if state.get("local_url"):
        lines.append(f"🏠 {state['local_url']} (local, no DNS)")
    if state.get("workspace_url"):
        lines.append(f"🗔 {state['workspace_url']} (app + chat in forge web)")
    elif state.get("forge_url"):
        lines.append(f"🧭 {state['forge_url']} (session in forge web)")
    return lines
```

- [ ] **Step 8: Run both new backend tests + the existing url-event test**

Run: `python3 -m pytest tests/test_slackbot.py -k "workspace_link_and_hides or url_event_surfaces_local" tests/test_slackmsg.py -k web_workspace_link -q`
Expected: PASS. (The existing `test_url_event_surfaces_local_preview_link` still passes: its cfg has no `forge_web_url`, so no `workspace_url` → the `run-1.forge.localhost` assertion is unaffected.)

- [ ] **Step 9: Run the full slack test group + slackmsg**

Run: `python3 -m pytest tests/test_slackbot.py tests/test_slackmsg.py -q`
Expected: PASS (no regressions).

- [ ] **Step 10: Commit**

```bash
git add src/forge/slackmsg.py src/forge/slackbot.py tests/test_slackmsg.py tests/test_slackbot.py
git commit -m "feat(slack): 🗔 workspace deep link supersedes dashboard link for live apps"
```

---

### Task 8: Full verification

- [ ] **Step 1: Full web suite + typecheck + build**

Run: `npm test` (expect all green), `npx tsc --noEmit` (no errors), `npm run build` (succeeds).

- [ ] **Step 2: Full Python suite**

Run: `python3 -m pytest -q`
Expected: PASS (prior baseline was 1081 passed, 2 skipped; expect +3 new backend tests → 1084 passed).

- [ ] **Step 3: End-to-end drive (verify skill)**

Follow the project `verify` / `run` skill to launch the web app, open `#live=<a-live-run-id>`, and confirm: app renders on the left, chat on the right, a follow-up prompt streams, and the app pane shows the nudge + reloads once on turn completion. Capture what was observed. (If no live run is available locally, drive the `resolveEmbedSrc`/route units + a `Workspace` render smoke instead, and note the limitation.)

- [ ] **Step 4: Final commit if verification produced fixes**

```bash
git add -A && git commit -m "test: verify live workspace end-to-end"
```
