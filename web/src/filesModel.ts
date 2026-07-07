/* ─────────────────────────────────────────────────────────────────────────────
   Live files pane model (pure, unit-tested): while the agent edits the
   workspace, tool events carry the touched path; these helpers turn the tool
   stream + the /files listing into a followable tree. Rendering and fetch
   timing live in FilesView.tsx.
   ───────────────────────────────────────────────────────────────────────────── */
import type { WorkspaceFile } from "./types";

export type TouchAction = "read" | "edit";

/** One file-touching tool call, in stream order. */
export interface FileTouch {
  path: string;
  action: TouchAction;
  ts: number;
}

const READ_TOOLS = new Set(["Read", "NotebookRead"]);
const EDIT_TOOLS = new Set(["Edit", "Write", "NotebookEdit"]);

/** Map a tool name to how it touches a file; null for non-file tools (their
 *  events carry no path anyway — this is the belt to that suspender). */
export function touchAction(toolName: string): TouchAction | null {
  if (EDIT_TOOLS.has(toolName)) return "edit";
  if (READ_TOOLS.has(toolName)) return "read";
  return null;
}

/** Append bounded: the pane only ever looks backwards a little (follow +
 *  flash), so a long turn must not grow state without limit. */
export function pushTouch(touches: FileTouch[], t: FileTouch, cap = 200): FileTouch[] {
  const next = touches.length >= cap ? touches.slice(touches.length - cap + 1) : touches.slice();
  next.push(t);
  return next;
}

export function lastEdit(touches: FileTouch[]): FileTouch | null {
  for (let i = touches.length - 1; i >= 0; i--)
    if (touches[i].action === "edit") return touches[i];
  return null;
}

export function lastTouch(touches: FileTouch[]): FileTouch | null {
  return touches.length ? touches[touches.length - 1] : null;
}

/** Which file the detail panel shows: an explicit pick (click) wins; follow
 *  mode tracks the agent's most recent edit/write. */
export function selectedPath(pinned: string | null, touches: FileTouch[]): string | null {
  return pinned ?? lastEdit(touches)?.path ?? null;
}

export interface TreeDir {
  name: string;
  path: string; // "" for the root
  dirs: TreeDir[];
  files: WorkspaceFile[];
}

/** Fold the flat (sorted) listing into a directory tree. Input order is kept,
 *  so lexicographic input yields lexicographic dirs and files. */
export function buildTree(files: WorkspaceFile[]): TreeDir {
  const root: TreeDir = { name: "", path: "", dirs: [], files: [] };
  const byPath = new Map<string, TreeDir>([["", root]]);
  const dirFor = (path: string): TreeDir => {
    const hit = byPath.get(path);
    if (hit) return hit;
    const cut = path.lastIndexOf("/");
    const parent = dirFor(cut === -1 ? "" : path.slice(0, cut));
    const node: TreeDir = { name: path.slice(cut + 1), path, dirs: [], files: [] };
    parent.dirs.push(node);
    byPath.set(path, node);
    return node;
  };
  for (const f of files) {
    const cut = f.path.lastIndexOf("/");
    dirFor(cut === -1 ? "" : f.path.slice(0, cut)).files.push(f);
  }
  return root;
}

export function ancestorDirs(path: string): string[] {
  const out: string[] = [];
  let i = path.indexOf("/");
  while (i !== -1) {
    out.push(path.slice(0, i));
    i = path.indexOf("/", i + 1);
  }
  return out;
}

/** Dirs that render open with no explicit user toggle: every ancestor of a
 *  changed file and of the current selection — the tree opens onto the
 *  action instead of burying it under collapsed folders. */
export function autoOpenDirs(files: WorkspaceFile[], selected: string | null): Set<string> {
  const open = new Set<string>();
  for (const f of files)
    if (f.status !== "clean") for (const d of ancestorDirs(f.path)) open.add(d);
  if (selected) for (const d of ancestorDirs(selected)) open.add(d);
  return open;
}

/** A user's explicit open/close click overrides the auto state permanently
 *  (for the pane's lifetime); otherwise follow the auto set. */
export function isDirOpen(
  path: string,
  overrides: ReadonlyMap<string, boolean>,
  auto: ReadonlySet<string>,
): boolean {
  return overrides.get(path) ?? auto.has(path);
}
