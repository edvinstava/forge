import { useCallback, useRef, useState } from "react";

/* Draggable split for the Workspace: the app pane's width as a percentage of
   the shell. Persisted so the ratio survives reloads. Pure helpers below are
   unit-tested; the hook wires them to pointer drags and localStorage. */

export const STORAGE_KEY = "forge.workspace.split";
export const DEFAULT_SPLIT = 75; // app pane %
export const MIN_PCT = 10; // ultimate safety bounds (percentage-only clamp)
export const MAX_PCT = 90;
export const MIN_PANE_PX = 280; // neither pane drags narrower than this

/** Clamp an app-pane percentage into the safe range; junk → default. */
export function clampSplit(pct: number): number {
  if (!Number.isFinite(pct)) return DEFAULT_SPLIT;
  return Math.min(MAX_PCT, Math.max(MIN_PCT, pct));
}

/** Parse a stored split string into a clamped percentage; tolerate anything. */
export function parseSplit(raw: string | null): number {
  if (!raw) return DEFAULT_SPLIT;
  const n = Number(raw);
  return Number.isFinite(n) ? clampSplit(n) : DEFAULT_SPLIT;
}

/**
 * App-pane % for a pointer at `clientX`, given the shell's left edge and width.
 * Clamps so neither pane drags below MIN_PANE_PX (falling back to the safe
 * percentage bounds on very narrow shells).
 */
export function splitFromPointer(clientX: number, left: number, width: number): number {
  if (width <= 0) return DEFAULT_SPLIT;
  const raw = ((clientX - left) / width) * 100;
  const minPx = (MIN_PANE_PX / width) * 100;
  const lo = Math.max(MIN_PCT, minPx);
  const hi = Math.min(MAX_PCT, 100 - minPx);
  if (lo > hi) return clampSplit(raw); // shell too narrow for both minimums
  return Math.min(hi, Math.max(lo, raw));
}

function readSplit(): number {
  try {
    return parseSplit(localStorage.getItem(STORAGE_KEY));
  } catch {
    return DEFAULT_SPLIT;
  }
}

function saveSplit(pct: number): void {
  try {
    localStorage.setItem(STORAGE_KEY, String(clampSplit(pct)));
  } catch {
    // storage unavailable (private mode / quota) — keep in-memory state
  }
}

/**
 * Owns the app-pane percentage and the gutter's drag behaviour. Returns the
 * current split, a ref for the shell (to measure its bounds), and handlers to
 * spread onto the gutter element. Double-click resets to the default.
 */
export function useSplitPane() {
  const [splitPct, setSplitPct] = useState<number>(readSplit);
  const shellRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    draggingRef.current = true;
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    document.body.classList.add("is-resizing");
    e.preventDefault();
  }, []);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    const rect = shellRef.current?.getBoundingClientRect();
    if (!rect) return;
    setSplitPct(splitFromPointer(e.clientX, rect.left, rect.width));
  }, []);

  const endDrag = useCallback((e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
    document.body.classList.remove("is-resizing");
    setSplitPct((p) => {
      saveSplit(p);
      return p;
    });
  }, []);

  const reset = useCallback(() => {
    setSplitPct(DEFAULT_SPLIT);
    saveSplit(DEFAULT_SPLIT);
  }, []);

  const gutterHandlers = {
    onPointerDown,
    onPointerMove,
    onPointerUp: endDrag,
    onPointerCancel: endDrag,
    onDoubleClick: reset,
  };

  return { splitPct, shellRef, gutterHandlers };
}
