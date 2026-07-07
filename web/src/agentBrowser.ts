/* ─────────────────────────────────────────────────────────────────────────────
   Agent-browser live view (pure, unit-tested): while a QA turn runs, the
   backend screencasts the agent's shared Chromium into
   GET /api/sessions/{id}/browser/frame. These helpers decide which pane the
   workspace shows and where the frames come from; the polling itself lives in
   Workspace.tsx.
   ───────────────────────────────────────────────────────────────────────────── */

/** What the user pinned: `auto` follows the stream (agent while it's live,
 *  app otherwise); `app`/`agent` are explicit choices that stick. */
export type PanePin = "auto" | "app" | "agent";

export type Pane = "app" | "agent";

/** Which pane the workspace's left side shows. A dead stream never wins:
 *  pinning "agent" with no frames would show a stale screenshot as if live,
 *  so an inactive stream always falls back to the app. */
export function resolvePane(pin: PanePin, active: boolean): Pane {
  if (!active) return "app";
  return pin === "app" ? "app" : "agent";
}

/** Clicking the pane toggle: choosing what `auto` already shows just stays in
 *  follow mode (no surprise lock-in); choosing the other pane pins it. */
export function nextPin(pin: PanePin, active: boolean, clicked: Pane): PanePin {
  if (pin === "auto" && resolvePane("auto", active) === clicked) return "auto";
  return clicked;
}

/** Frame URL with the status ts as cache-buster: the <img> src only changes —
 *  and thus refetches — when a new frame actually landed. Poll-mode fallback;
 *  the MJPEG stream below is the fast path. */
export function browserFrameUrl(sessionId: string, ts: number): string {
  return `/api/sessions/${sessionId}/browser/frame?t=${ts}`;
}

/** MJPEG stream URL: one long-lived request the <img> renders natively, each
 *  frame pushed the moment the screencaster writes it (~10 fps, sub-second
 *  latency). `epoch` counts screencast starts — a bump forces a fresh
 *  connection, since the previous stream ended with the last turn. */
export function browserStreamUrl(sessionId: string, epoch: number): string {
  return `/api/sessions/${sessionId}/browser/stream?e=${epoch}`;
}

/** Epoch advances only on the inactive→active edge (a new screencast started);
 *  staying active or going inactive keeps the current stream connection. */
export function nextEpoch(prevActive: boolean, active: boolean, epoch: number): number {
  return active && !prevActive ? epoch + 1 : epoch;
}

/**
 * The workspace embeds the app from run-<id>.<proxy_domain>:<proxy_port>. When
 * the SPA itself is opened from 127.0.0.1/localhost those are different
 * *sites*, so browsers drop SameSite=Lax session cookies inside the iframe —
 * login succeeds but bounces straight back to sign-in. `localhost` is not on
 * the Public Suffix List, so <proxy_domain> (default forge.localhost) and its
 * run-* subdomains share a registrable domain: serving the workspace from the
 * proxy domain makes the iframe same-site and cookies stick.
 *
 * Returns the same-site URL to redirect to, or null when already fine (or the
 * proxy domain isn't a .localhost name we know resolves to loopback).
 */
export function cookieSafeWorkspaceUrl(
  loc: { hostname: string; port: string; pathname: string; hash: string },
  proxyDomain: string | undefined,
): string | null {
  if (!proxyDomain) return null;
  const d = proxyDomain.toLowerCase();
  if (d !== "localhost" && !d.endsWith(".localhost")) return null; // custom domain — may not resolve here
  const h = loc.hostname.toLowerCase();
  if (h !== "127.0.0.1" && h !== "localhost") return null; // already same-site, or a remote/tunnel viewer
  const port = loc.port ? `:${loc.port}` : "";
  return `http://${d}${port}${loc.pathname}${loc.hash}`;
}
