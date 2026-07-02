/* Hash deep-links: #s=<run_id> selects a session on load, so Slack messages
   can link straight into the web app (see slackmsg.web_session_link). */

export function parseSessionHash(hash: string): string | null {
  const m = /^#s=([A-Za-z0-9_-]+)$/.exec(hash || "");
  return m ? m[1] : null;
}

export function sessionHash(runId: string): string {
  return `#s=${runId}`;
}
