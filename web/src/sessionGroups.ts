import type { SessionSummary } from "./types";

/** One repo folder: a raw grouping key, a friendly header label, and its chats. */
export interface SessionGroup {
  repo: string;
  displayName: string;
  sessions: SessionSummary[];
}

const UNKNOWN = "(unknown)";

/**
 * Friendly header label for a repo grouping key.
 *   - GitHub `owner/repo` → shown as-is.
 *   - Absolute filesystem path (local repo) → basename only.
 *   - Falsy → "(unknown)".
 */
export function repoDisplayName(repo: string): string {
  if (!repo) return UNKNOWN;
  if (repo.startsWith("/")) {
    const parts = repo.split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : UNKNOWN;
  }
  return repo;
}

/**
 * Group a flat, newest-first session list into per-repo folders.
 *
 * A single pass preserves the input ordering: a folder's slot is the position
 * of its first (newest) session, and sessions stay in input order within a
 * folder. The raw `repo` string is the grouping key, so two distinct repos
 * that share a basename never merge.
 */
export function groupSessionsByRepo(sessions: SessionSummary[]): SessionGroup[] {
  const byRepo = new Map<string, SessionGroup>();
  for (const sess of sessions) {
    const key = sess.repo || UNKNOWN;
    let group = byRepo.get(key);
    if (!group) {
      group = { repo: key, displayName: repoDisplayName(key), sessions: [] };
      byRepo.set(key, group);
    }
    group.sessions.push(sess);
  }
  return [...byRepo.values()];
}
