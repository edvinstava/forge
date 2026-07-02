import React, { useState, useEffect, useRef, useCallback } from "react";
import type { Repo } from "./types";
import { listRepos } from "./api";
import { SessionFolder } from "./SessionFolder";
import { BatchComposer } from "./BatchComposer";
import { groupSessionsByRepo } from "./sessionGroups";
import { summarize } from "./queueSummary";
import { filterSessions } from "./filterSessions";
import { useCollapsedFolders, isFolderOpen } from "./useCollapsedFolders";
import type { SessionSummary } from "./types";

/* ── Types ── */
export interface NewSessionPayload {
  repo: string;
  source: "local" | "github";
}

interface SidebarProps {
  sessions: SessionSummary[];
  activeId: string | null;
  onSelect: (runId: string) => void;
  onNewSession: (payload: NewSessionPayload) => void;
  onRefresh: () => void;
  onCancel: (runId: string) => void;
}

/* ── Repo Picker ── */
interface RepoPickerProps {
  onSelect: (payload: NewSessionPayload) => void;
  onClose: () => void;
}

function RepoPicker({ onSelect, onClose }: RepoPickerProps) {
  const [query, setQuery] = useState("");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [loading, setLoading] = useState(false);
  const [ghInput, setGhInput] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    searchRef.current?.focus();
    // Load initial repo list
    loadRepos("");
    // Clear any pending debounce timer if the picker unmounts mid-type.
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const loadRepos = useCallback((q: string) => {
    setLoading(true);
    listRepos(q)
      .then(setRepos)
      .catch(() => setRepos([]))
      .finally(() => setLoading(false));
  }, []);

  const handleQueryChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value;
    setQuery(q);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => loadRepos(q), 250);
  };

  const handleGhSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const v = ghInput.trim();
    if (!v) return;
    onSelect({ repo: v, source: "github" });
  };

  // Dismiss on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div className="repo-picker">
      <div className="repo-picker-header">
        <span className="repo-picker-title">new session</span>
        <button className="repo-picker-close" onClick={onClose} title="Close">✕</button>
      </div>

      {/* Local repos */}
      <div className="repo-picker-section">
        <div className="repo-picker-section-label">local workspace</div>
        <input
          ref={searchRef}
          className="input repo-picker-search"
          type="text"
          placeholder="filter repos…"
          value={query}
          onChange={handleQueryChange}
        />
        <div className="repo-picker-list scrollable">
          {loading && (
            <div className="repo-picker-empty">searching…</div>
          )}
          {!loading && repos.length === 0 && (
            <div className="repo-picker-empty">no repos found</div>
          )}
          {!loading && repos.map((r) => (
            <button
              key={r.path ?? r.name}
              className="repo-picker-row"
              onClick={() => onSelect({ repo: r.path ?? r.name, source: "local" })}
            >
              <span className="repo-picker-row-name">{r.name}</span>
              {r.remote && (
                <span className="repo-picker-row-remote mono">{r.remote}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* GitHub */}
      <div className="divider" />
      <div className="repo-picker-section">
        <div className="repo-picker-section-label">github — owner/repo</div>
        <form className="repo-picker-gh-form" onSubmit={handleGhSubmit}>
          <input
            className="input"
            type="text"
            placeholder="e.g. dhis2/dhis2-core"
            value={ghInput}
            onChange={(e) => setGhInput(e.target.value)}
          />
          <button
            className="btn btn-accent"
            type="submit"
            disabled={!ghInput.trim()}
          >
            clone ↗
          </button>
        </form>
      </div>
    </div>
  );
}

/* ── Sidebar ── */
export function Sidebar({ sessions, activeId, onSelect, onNewSession, onRefresh, onCancel }: SidebarProps) {
  const [showPicker, setShowPicker] = useState(false);
  const [showBatch, setShowBatch] = useState(false);
  const [query, setQuery] = useState("");
  const { collapsed, toggle } = useCollapsedFolders();
  const sum = summarize(sessions);

  const handleNewSession = useCallback(
    (payload: NewSessionPayload) => {
      setShowPicker(false);
      onNewSession(payload);
    },
    [onNewSession]
  );

  // Filter → group. Folders are derived from the (possibly filtered) list;
  // the active session's repo and an active filter both force folders open.
  const filtering = query.trim().length > 0;
  const groups = groupSessionsByRepo(filterSessions(sessions, query));
  const activeSession = sessions.find((s) => s.run_id === activeId);
  const activeRepo = activeSession ? activeSession.repo || "(unknown)" : null;

  return (
    <nav className="sidebar">
      {/* Header */}
      <div className="sidebar-header">
        <span className="sidebar-logo">forge</span>
        <button
          className="btn btn-accent sidebar-new-btn"
          onClick={() => setShowPicker((v) => !v)}
          title="New session"
        >
          + new
        </button>
        <button
          className="btn sidebar-batch-btn"
          onClick={() => setShowBatch((v) => !v)}
          title="Queue a batch of tasks"
        >
          batch
        </button>
      </div>

      {/* Repo picker (inline, below header) */}
      {showPicker && (
        <RepoPicker
          onSelect={handleNewSession}
          onClose={() => setShowPicker(false)}
        />
      )}

      {/* Batch composer (inline, below header) */}
      {showBatch && (
        <BatchComposer
          onSubmitted={onRefresh}
          onClose={() => setShowBatch(false)}
        />
      )}

      {/* Queue summary */}
      {(sum.queued > 0 || sum.running > 0) && (
        <div className="queue-summary mono">
          {sum.queued} queued · {sum.running} running · {sum.done} done
        </div>
      )}

      <div className="divider" />

      {/* Filter box */}
      {sessions.length > 0 && (
        <div className="sidebar-filter">
          <input
            className="input"
            type="text"
            placeholder="filter chats…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      )}

      {/* Session list, grouped into per-repo folders */}
      <div className="sidebar-sessions scrollable">
        {sessions.length === 0 && (
          <div className="sidebar-empty">
            <span className="sidebar-empty-icon">⊡</span>
            <span>no sessions yet</span>
          </div>
        )}
        {sessions.length > 0 && groups.length === 0 && (
          <div className="sidebar-empty">
            <span>no chats match</span>
          </div>
        )}
        {groups.map((g) => (
          <SessionFolder
            key={g.repo}
            displayName={g.displayName}
            sessions={g.sessions}
            open={isFolderOpen({ collapsed, repo: g.repo, activeRepo, filtering })}
            activeId={activeId}
            onToggle={() => toggle(g.repo)}
            onSelect={onSelect}
            onCancel={onCancel}
          />
        ))}
      </div>

      {/* Footer */}
      <div className="sidebar-footer">
        <span className="mono sidebar-footer-text">
          {sessions.length} session{sessions.length !== 1 ? "s" : ""}
        </span>
      </div>
    </nav>
  );
}
