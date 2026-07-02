import { useCallback, useState } from "react";

export const STORAGE_KEY = "forge.collapsedFolders";

/** Parse the stored JSON array of collapsed repo keys; tolerate anything. */
export function parseCollapsed(raw: string | null): Set<string> {
  if (!raw) return new Set();
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed.map(String)) : new Set();
  } catch {
    return new Set();
  }
}

export function serializeCollapsed(set: Set<string>): string {
  return JSON.stringify([...set]);
}

/** Flip a key's membership, returning a NEW set (safe for React state). */
export function toggleInSet(set: Set<string>, key: string): Set<string> {
  const next = new Set(set);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

/**
 * Whether a folder should render expanded. A folder opens when filtering is
 * active (so matches are always visible), when it holds the active session,
 * or when it is simply not in the collapsed set.
 */
export function isFolderOpen(opts: {
  collapsed: Set<string>;
  repo: string;
  activeRepo: string | null;
  filtering: boolean;
}): boolean {
  const { collapsed, repo, activeRepo, filtering } = opts;
  if (filtering) return true;
  if (repo === activeRepo) return true;
  return !collapsed.has(repo);
}

function readStorage(): Set<string> {
  try {
    return parseCollapsed(localStorage.getItem(STORAGE_KEY));
  } catch {
    return new Set();
  }
}

/**
 * Owns the set of collapsed repo keys, persisting it to localStorage.
 * Returns the current set plus a `toggle` that flips and persists one key.
 */
export function useCollapsedFolders() {
  const [collapsed, setCollapsed] = useState<Set<string>>(readStorage);

  const toggle = useCallback((repo: string) => {
    setCollapsed((prev) => {
      const next = toggleInSet(prev, repo);
      try {
        localStorage.setItem(STORAGE_KEY, serializeCollapsed(next));
      } catch {
        // storage unavailable (private mode / quota) — keep in-memory state
      }
      return next;
    });
  }, []);

  return { collapsed, toggle };
}
