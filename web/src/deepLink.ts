/* Hash deep-links: #s=<run_id> selects a session on load, so Slack messages
   can link straight into the web app (see slackmsg.web_session_link). */

export function parseSessionHash(hash: string): string | null {
  const m = /^#s=([A-Za-z0-9_-]+)$/.exec(hash || "");
  return m ? m[1] : null;
}

export function sessionHash(runId: string): string {
  return `#s=${runId}`;
}

/* Route discriminant: #live=<id> opens the focused workspace (running app +
   agent chat, side by side); #s=<id> (or anything else) stays on the dashboard.
   See slackmsg.web_workspace_link / web_session_link. */
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
