import { useState, useEffect, useRef, useCallback } from "react";
import type { VerifyResult } from "./types";
import { getDiff, getVerify } from "./api";

/* ── Types ── */
interface InspectorProps {
  activeId: string | null;
  webUrl: string | null;
  /** DNS-free http://run-<id>.forge.localhost URL; preferred for the embed since
   *  it always resolves on the forge host even when the tunnel hostname won't. */
  localUrl: string | null;
}

type Tab = "preview" | "diff" | "verify";

/* ─────────────────────────────────────────────────────────────────────────────
   Diff parsing — a proper unified-diff model with per-file stats and gutters.
   ───────────────────────────────────────────────────────────────────────────── */

type RowType = "ctx" | "add" | "del" | "hunk";

interface DiffRow {
  type: RowType;
  oldNo: number | null;
  newNo: number | null;
  text: string;
}

interface DiffFile {
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

const STATUS_GLYPH: Record<DiffFile["status"], string> = {
  added: "A",
  deleted: "D",
  renamed: "R",
  modified: "M",
};

function DiffFileView({ file }: { file: DiffFile }) {
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

/* ── Diff tab ── */
function DiffTab({ activeId }: { activeId: string }) {
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

/* ── Verify tab ── */
function VerifyTab({ activeId }: { activeId: string }) {
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await getVerify(activeId));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [activeId]);

  useEffect(() => {
    load();
  }, [load]);

  const ok = result?.verify_ok ?? null;
  const tone = ok === true ? "pass" : ok === false ? "fail" : "none";
  const label = ok === true ? "passing" : ok === false ? "failing" : "no checks";
  const sub =
    ok === true
      ? "all configured checks passed"
      : ok === false
      ? "one or more checks failed"
      : "this repo has no verification configured";

  return (
    <div className="inspector-pane">
      <div className="inspector-toolbar">
        <span className="inspector-section-label">verify</span>
        <button className="btn btn-sm" onClick={load} disabled={loading}>
          {loading ? "…" : "↻"}
        </button>
      </div>

      {error && <div className="inspector-error">{error}</div>}

      {!loading && !error && result && (
        <div className="verify-body scrollable">
          <div className={`verify-card verify-card--${tone}`}>
            <div className="verify-icon">{ok === true ? "✓" : ok === false ? "✕" : "—"}</div>
            <div className="verify-text">
              <div className="verify-label">{label}</div>
              <div className="verify-sub">{sub}</div>
            </div>
          </div>

          <div className="verify-meta">
            {result.diff_files != null && (
              <div className="verify-meta-item">
                <span className="verify-meta-k">files changed</span>
                <span className="verify-meta-v">{result.diff_files}</span>
              </div>
            )}
            {result.model && (
              <div className="verify-meta-item">
                <span className="verify-meta-k">model</span>
                <span className="verify-meta-v">⚡ {result.model}</span>
              </div>
            )}
          </div>

          {result.verify_failed.length > 0 && (
            <div className="verify-failed">
              <span className="inspector-section-label">failed checks</span>
              <div className="verify-failed-chips">
                {result.verify_failed.map((name) => (
                  <span key={name} className="verify-chip">
                    {name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {result.verify_output && (
            <div className="verify-output-wrap">
              <span className="inspector-section-label">output</span>
              <pre className="verify-output scrollable">{result.verify_output}</pre>
            </div>
          )}
        </div>
      )}

      {!loading && !error && !result && (
        <div className="inspector-empty">
          <div className="inspector-empty-icon">—</div>
          <div className="inspector-empty-label">no verify data</div>
        </div>
      )}
    </div>
  );
}

/* ── Preview tab ── */
type FrameState = "pending" | "ok" | "failed";

function PreviewTab({
  webUrl,
  localUrl,
  frameState,
  onFrameResult,
}: {
  webUrl: string | null;
  localUrl: string | null;
  frameState: FrameState;
  onFrameResult: (s: FrameState) => void;
}) {
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Prefer the DNS-free local URL for the embed: the Inspector is viewed on the
  // forge host, where run-<id>.forge.localhost always resolves but the public
  // tunnel hostname may not. The public URL stays available as a share link.
  const src = localUrl ?? webUrl;
  const share = webUrl && webUrl !== src ? webUrl : null;

  useEffect(() => {
    if (src && frameState === "pending") {
      timeoutRef.current = setTimeout(() => onFrameResult("failed"), 6000);
    }
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, [src, frameState, onFrameResult]);

  const handleLoad = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    if (frameState === "pending") onFrameResult("ok");
  };

  if (!src) {
    return (
      <div className="inspector-empty">
        <div className="inspector-empty-icon">◻</div>
        <div className="inspector-empty-label">no web service</div>
        <div className="inspector-empty-hint">starts once the app is live</div>
      </div>
    );
  }

  if (frameState === "failed") {
    return (
      <div className="preview-fallback">
        <div className="preview-fallback-icon">⃠</div>
        <div className="preview-fallback-msg">
          This app can’t be embedded
          <span className="preview-fallback-note">(blocked by X-Frame-Options / CSP)</span>
        </div>
        <a href={src} target="_blank" rel="noopener noreferrer" className="btn btn-accent">
          open app in new tab ↗
        </a>
        {share && (
          <a href={share} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Public share link">
            🌐 public link ↗
          </a>
        )}
      </div>
    );
  }

  return (
    <div className="preview-wrap">
      <div className="preview-toolbar">
        <span className="preview-dot" />
        <span className="preview-url" title={src}>
          {src.replace(/^https?:\/\//, "")}
        </span>
        <a href={src} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Open in new tab">
          ↗
        </a>
        {share && (
          <a href={share} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Public share link (no DNS needed locally)">
            🌐
          </a>
        )}
      </div>
      <iframe
        src={src}
        className="preview-iframe"
        title="App preview"
        onLoad={handleLoad}
        onError={() => onFrameResult("failed")}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"
      />
    </div>
  );
}

/* ── Inspector ── */
export function Inspector({ activeId, webUrl, localUrl }: InspectorProps) {
  const [tab, setTab] = useState<Tab>("preview");
  const [frameState, setFrameState] = useState<FrameState>("pending");
  const previewUrl = localUrl ?? webUrl;

  useEffect(() => {
    setFrameState("pending");
  }, [previewUrl]);

  useEffect(() => {
    setTab(previewUrl ? "preview" : "diff");
  }, [activeId, previewUrl]);

  if (!activeId) {
    return (
      <div className="inspector">
        <div className="pane-placeholder">
          <div className="pane-placeholder-icon">⬚</div>
          <div className="pane-placeholder-label">select a session</div>
        </div>
      </div>
    );
  }

  const previewDisabled = !previewUrl;
  const TABS: { id: Tab; label: string; disabled?: boolean }[] = [
    { id: "preview", label: "preview", disabled: previewDisabled },
    { id: "diff", label: "diff" },
    { id: "verify", label: "verify" },
  ];

  return (
    <div className="inspector">
      <div className="inspector-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`inspector-tab${tab === t.id ? " is-active" : ""}${t.disabled ? " is-disabled" : ""}`}
            onClick={() => !t.disabled && setTab(t.id)}
            disabled={t.disabled}
            title={t.disabled ? "No web service available" : undefined}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="inspector-body">
        {tab === "preview" && (
          <PreviewTab webUrl={webUrl} localUrl={localUrl} frameState={frameState} onFrameResult={setFrameState} />
        )}
        {tab === "diff" && <DiffTab activeId={activeId} />}
        {tab === "verify" && <VerifyTab activeId={activeId} />}
      </div>
    </div>
  );
}
