import type { SessionSummary } from "./types";
import { repoDisplayName } from "./sessionGroups";

/**
 * Narrow a session list to those whose chat title or repo display name
 * contains `query` (case-insensitive, trimmed). An empty/whitespace query
 * returns the list unchanged. Local repos match on their basename, since
 * that is the name the user actually sees in the folder header.
 */
export function filterSessions(
  sessions: SessionSummary[],
  query: string,
): SessionSummary[] {
  const q = query.trim().toLowerCase();
  if (!q) return sessions;
  return sessions.filter((s) => {
    const title = (s.title ?? "").toLowerCase();
    const repo = repoDisplayName(s.repo).toLowerCase();
    return title.includes(q) || repo.includes(q);
  });
}
