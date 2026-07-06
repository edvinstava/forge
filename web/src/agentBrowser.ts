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
 *  and thus refetches — when a new frame actually landed. */
export function browserFrameUrl(sessionId: string, ts: number): string {
  return `/api/sessions/${sessionId}/browser/frame?t=${ts}`;
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
