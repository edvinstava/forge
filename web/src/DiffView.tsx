import { useState, useEffect, useCallback } from "react";
import { getDiff } from "./api";
import { parseDiff, STATUS_GLYPH, type DiffFile } from "./diffModel";

export function DiffFileView({ file }: { file: DiffFile }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="diff-file">
      <button className="diff-file-head" onClick={() => setOpen((o) => !o)}>
        <span className="diff-file-caret">{open ? "▾" : "▸"}</span>
        <span className={`diff-file-status diff-file-status--${file.status}`}>
          {STATUS_GLYPH[file.status]}
        </span>
        <span className="diff-file-path" title={file.path}>
          {file.path}
        </span>
        <span className="diff-file-stat">
          {file.additions > 0 && <span className="diff-stat-add">+{file.additions}</span>}
          {file.deletions > 0 && <span className="diff-stat-del">−{file.deletions}</span>}
        </span>
      </button>
      {open && (
        <div className="diff-rows">
          {file.rows.map((r, i) =>
            r.type === "hunk" ? (
              <div key={i} className="dl dl-hunk">
                <span className="dl-text">{r.text}</span>
              </div>
            ) : (
              <div key={i} className={`dl dl-${r.type}`}>
                <span className="dl-num">{r.oldNo ?? ""}</span>
                <span className="dl-num">{r.newNo ?? ""}</span>
                <span className="dl-sign">{r.type === "add" ? "+" : r.type === "del" ? "−" : " "}</span>
                <span className="dl-text">{r.text || " "}</span>
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}

export function DiffView({ activeId }: { activeId: string }) {
  const [raw, setRaw] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRaw(await getDiff(activeId));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [activeId]);

  useEffect(() => {
    load();
  }, [load]);

  const files = raw != null ? parseDiff(raw) : [];
  const totalAdd = files.reduce((n, f) => n + f.additions, 0);
  const totalDel = files.reduce((n, f) => n + f.deletions, 0);

  return (
    <div className="inspector-pane">
      <div className="inspector-toolbar">
        <span className="inspector-section-label">
          {files.length > 0 ? `${files.length} file${files.length !== 1 ? "s" : ""}` : "diff"}
        </span>
        {files.length > 0 && (
          <span className="inspector-toolbar-stat">
            <span className="diff-stat-add">+{totalAdd}</span>
            <span className="diff-stat-del">−{totalDel}</span>
          </span>
        )}
        <button className="btn btn-sm" onClick={load} disabled={loading}>
          {loading ? "…" : "↻"}
        </button>
      </div>

      {error && <div className="inspector-error">{error}</div>}

      {!loading && !error && files.length === 0 && (
        <div className="inspector-empty">
          <div className="inspector-empty-icon">∅</div>
          <div className="inspector-empty-label">no changes yet</div>
        </div>
      )}

      <div className="diff-files scrollable">
        {files.map((f, i) => (
          <DiffFileView key={`${f.path}-${i}`} file={f} />
        ))}
      </div>
    </div>
  );
}
