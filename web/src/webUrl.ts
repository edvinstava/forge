import type { ProxyConfig, SessionSummary } from "./types";

/**
 * Resolve the web_url to show for the active session, given the latest polled
 * session list and the current value. Fills in a URL the moment the poll knows
 * one (so a session that goes live in the background — e.g. started from Slack —
 * surfaces its live link), but never clobbers a known URL with a transient null
 * (an asleep/just-restarted poll), which would make the Inspector flicker.
 */
export function pickWebUrl(
  prev: string | null,
  sessions: SessionSummary[],
  activeId: string | null,
): string | null {
  const s = sessions.find((x) => x.run_id === activeId);
  return s?.web_url ?? prev;
}

/**
 * The DNS-free local preview URL for a run: http://run-<id>.<domain>:<port>.
 * `*.localhost` resolves to 127.0.0.1 in every browser with no external DNS, so
 * it opens even when the public tunnel hostname can't be resolved on this
 * network (e.g. a router that NXDOMAINs *.trycloudflare.com).
 *
 * Returns null unless the run is tunnel-fronted, signalled by a public
 * (non-loopback) web_url: a localhost web_url means no shared Caddy is routing
 * the run, so run-<id>.forge.localhost would resolve to nothing useful.
 */
export function localPreviewUrl(
  activeId: string | null,
  webUrl: string | null,
  config: ProxyConfig | null,
): string | null {
  if (!activeId || !webUrl || !config) return null;
  if (/^https?:\/\/(localhost|127\.0\.0\.1)\b/i.test(webUrl)) return null;
  return `http://run-${activeId}.${config.proxy_domain}:${config.proxy_port}`;
}
