/* ─────────────────────────────────────────────────────────────────────────────
   Diff parsing — a proper unified-diff model with per-file stats and gutters.
   Pure, no React; shared by DiffView and any other consumer.
   ───────────────────────────────────────────────────────────────────────────── */

export type RowType = "ctx" | "add" | "del" | "hunk";

export interface DiffRow {
  type: RowType;
  oldNo: number | null;
  newNo: number | null;
  text: string;
}

export interface DiffFile {
  path: string;
  status: "added" | "deleted" | "renamed" | "modified";
  additions: number;
  deletions: number;
  rows: DiffRow[];
}

function parseFile(chunk: string): DiffFile {
  const lines = chunk.split("\n");
  let aPath = "";
  let bPath = "";
  let status: DiffFile["status"] = "modified";
  const rows: DiffRow[] = [];
  let oldNo = 0;
  let newNo = 0;
  let additions = 0;
  let deletions = 0;

  for (const line of lines) {
    if (line.startsWith("diff --git")) {
      const m = line.match(/^diff --git a\/(.+) b\/(.+)$/);
      if (m) {
        aPath = m[1];
        bPath = m[2];
      }
      continue;
    }
    if (line.startsWith("new file")) {
      status = "added";
      continue;
    }
    if (line.startsWith("deleted file")) {
      status = "deleted";
      continue;
    }
    if (line.startsWith("rename ")) {
      status = "renamed";
      continue;
    }
    if (
      line.startsWith("index ") ||
      line.startsWith("similarity ") ||
      line.startsWith("old mode") ||
      line.startsWith("new mode") ||
      line.startsWith("--- ") ||
      line.startsWith("+++ ")
    ) {
      continue;
    }
    if (line.startsWith("@@")) {
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[2], 10);
      }
      rows.push({ type: "hunk", oldNo: null, newNo: null, text: line });
      continue;
    }
    if (line.startsWith("\\")) continue; // "\ No newline at end of file"
    if (line.startsWith("+")) {
      additions++;
      rows.push({ type: "add", oldNo: null, newNo: newNo++, text: line.slice(1) });
    } else if (line.startsWith("-")) {
      deletions++;
      rows.push({ type: "del", oldNo: oldNo++, newNo: null, text: line.slice(1) });
    } else {
      const text = line.startsWith(" ") ? line.slice(1) : line;
      rows.push({ type: "ctx", oldNo: oldNo++, newNo: newNo++, text });
    }
  }
  const path = status === "deleted" ? aPath : bPath || aPath;
  return { path, status, additions, deletions, rows };
}

export function parseDiff(raw: string): DiffFile[] {
  if (!raw.trim()) return [];
  return raw
    .split(/^(?=diff --git )/m)
    .filter((c) => c.trim())
    .map(parseFile);
}

export const STATUS_GLYPH: Record<DiffFile["status"], string> = {
  added: "A",
  deleted: "D",
  renamed: "R",
  modified: "M",
};
