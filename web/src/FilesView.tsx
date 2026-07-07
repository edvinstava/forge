import { useState, useEffect, useMemo, useCallback, type ReactNode } from "react";
import { getFiles, getFile } from "./api";
import type { WorkspaceFile, FileDetail, FileStatus } from "./types";
import { parseDiff } from "./diffModel";
import { DiffFileView } from "./DiffView";
import {
  buildTree, autoOpenDirs, isDirOpen, selectedPath, lastEdit, lastTouch,
  type FileTouch, type TreeDir,
} from "./filesModel";

const STATUS_DOT: Record<FileStatus, string> = {
  clean: "", modified: "M", added: "A", deleted: "D", renamed: "R",
  untracked: "A",
};

/* Refetch pacing: listing + detail follow the tool stream, but git status on
   every single Edit event would hammer the host during a fast turn. */
const LIST_DEBOUNCE_MS = 800;
const DETAIL_DEBOUNCE_MS = 400;

function TreeDirRow({
  dir, depth, overrides, auto, onToggle, renderFile,
}: {
  dir: TreeDir;
  depth: number;
  overrides: ReadonlyMap<string, boolean>;
  auto: ReadonlySet<string>;
  onToggle: (path: string, open: boolean) => void;
  renderFile: (f: WorkspaceFile, depth: number) => ReactNode;
}) {
  const open = isDirOpen(dir.path, overrides, auto);
  return (
    <div>
      <button
        className="files-row files-dir"
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => onToggle(dir.path, !open)}
      >
        <span className="files-caret">{open ? "▾" : "▸"}</span>
        <span className="files-name">{dir.name}</span>
      </button>
      {open && (
        <div>
          {dir.dirs.map((d) => (
            <TreeDirRow key={d.path} dir={d} depth={depth + 1} overrides={overrides}
              auto={auto} onToggle={onToggle} renderFile={renderFile} />
          ))}
          {dir.files.map((f) => renderFile(f, depth + 1))}
        </div>
      )}
    </div>
  );
}

/**
 * The live files pane: the workspace tree on the left (changed files marked,
 * the agent's latest touch flashing), the picked file's diff or content on the
 * right. Follow mode (default) keeps the detail on whatever the agent last
 * edited — the "watch the agent work" view for turns that never open a
 * browser; clicking any file pins it, ⦿ follow resumes.
 */
export function FilesView({
  sessionId,
  touches,
  reloadSignal = 0,
}: {
  sessionId: string;
  touches: FileTouch[];
  /** Bumped when a turn completes — refresh both listing and detail. */
  reloadSignal?: number;
}) {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [listTruncated, setListTruncated] = useState(false);
  const [pinned, setPinned] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Map<string, boolean>>(new Map());
  const [detail, setDetail] = useState<FileDetail | null>(null);
  const [view, setView] = useState<"diff" | "file">("diff");

  const edit = lastEdit(touches);
  const flash = lastTouch(touches);
  const selected = selectedPath(pinned, touches);

  /* ── Listing: on mount, then debounced after each edit, and on turn end ── */
  const loadList = useCallback(() => {
    getFiles(sessionId)
      .then((r) => { setFiles(r.files); setListTruncated(r.truncated); })
      .catch(() => {});
  }, [sessionId]);
  useEffect(() => { loadList(); }, [loadList, reloadSignal]);
  useEffect(() => {
    if (!edit) return;
    const t = setTimeout(loadList, LIST_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [edit?.ts, loadList]);

  /* ── Detail: follows the selection; re-fetches when the selected file is
     edited again mid-turn (its ts changes) or the turn completes. ── */
  const detailTs = selected && edit?.path === selected ? edit.ts : 0;
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    let stale = false;
    const t = setTimeout(() => {
      getFile(sessionId, selected)
        .then((d) => { if (!stale) setDetail(d); })
        .catch(() => { if (!stale) setDetail(null); });
    }, DETAIL_DEBOUNCE_MS);
    return () => { stale = true; clearTimeout(t); };
  }, [sessionId, selected, detailTs, reloadSignal]);

  const tree = useMemo(() => buildTree(files), [files]);
  const auto = useMemo(() => autoOpenDirs(files, selected), [files, selected]);
  const onToggle = useCallback((path: string, open: boolean) => {
    setOverrides((m) => new Map(m).set(path, open));
  }, []);

  const renderFile = (f: WorkspaceFile, depth: number) => {
    const isSel = f.path === selected;
    const isFlash = flash?.path === f.path;
    return (
      <button
        key={isFlash ? `${f.path}@${flash!.ts}` : f.path}
        className={
          `files-row files-leaf files-status--${f.status}` +
          (isSel ? " is-selected" : "") +
          (isFlash ? ` files-flash--${flash!.action}` : "")
        }
        style={{ paddingLeft: 8 + depth * 14 }}
        title={f.path}
        onClick={() => setPinned(f.path)}
      >
        <span className="files-name">{f.path.split("/").pop()}</span>
        {STATUS_DOT[f.status] && (
          <span className="files-status-glyph">{STATUS_DOT[f.status]}</span>
        )}
      </button>
    );
  };

  const diffFiles = detail?.diff ? parseDiff(detail.diff) : [];
  const showDiff = view === "diff" && diffFiles.length > 0;

  return (
    <div className="files-wrap">
      <div className="preview-toolbar files-toolbar">
        <span className="files-mode-label">
          {pinned === null ? "⦿ following the agent" : "pinned"}
        </span>
        <span className="preview-url" title={selected ?? ""}>{selected ?? ""}</span>
        {detail && diffFiles.length > 0 && (
          <span className="files-view-toggle">
            {(["diff", "file"] as const).map((v) => (
              <button
                key={v}
                className={`pane-toggle-btn${(showDiff ? "diff" : "file") === v ? " is-active" : ""}`}
                onClick={() => setView(v)}
              >
                {v}
              </button>
            ))}
          </span>
        )}
        {pinned !== null && (
          <button className="btn btn-sm" title="Resume following the agent's edits"
            onClick={() => setPinned(null)}>
            ⦿ follow
          </button>
        )}
        <button className="btn btn-sm" onClick={loadList} title="Refresh listing">↻</button>
      </div>
      <div className="files-body">
        <div className="files-tree scrollable">
          {listTruncated && <div className="files-note">listing truncated</div>}
          {files.length === 0 && (
            <div className="files-note">no workspace files yet</div>
          )}
          {tree.dirs.map((d) => (
            <TreeDirRow key={d.path} dir={d} depth={0} overrides={overrides}
              auto={auto} onToggle={onToggle} renderFile={renderFile} />
          ))}
          {tree.files.map((f) => renderFile(f, 0))}
        </div>
        <div className="files-detail scrollable">
          {!selected && (
            <div className="inspector-empty">
              <div className="inspector-empty-icon">⦿</div>
              <div className="inspector-empty-label">waiting for the agent's first edit</div>
              <div className="inspector-empty-hint">or pick a file on the left</div>
            </div>
          )}
          {selected && detail?.binary && (
            <div className="files-note">binary file ({detail.size} bytes)</div>
          )}
          {selected && detail?.missing && !detail?.diff && (
            <div className="files-note">file deleted</div>
          )}
          {selected && detail && showDiff &&
            diffFiles.map((f, i) => <DiffFileView key={`${f.path}-${i}`} file={f} />)}
          {selected && detail && !showDiff && !detail.binary && !detail.missing && (
            <>
              {detail.truncated && (
                <div className="files-note">
                  showing first {Math.round(detail.content.length / 1024)} kB of {Math.round(detail.size / 1024)} kB
                </div>
              )}
              <pre className="files-content">{detail.content}</pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
