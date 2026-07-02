import type { SessionSummary, SessionState } from "./types";
import { sessionStateMeta } from "./sessionState";

/* ── State Badge ── */
export function StateBadge({ state }: { state: SessionState }) {
  const meta = sessionStateMeta(state);
  return (
    <span className={`badge badge-${meta.cls}`}>
      <span className="badge-dot" />
      {meta.label}
    </span>
  );
}

/* ── Session Row ── */
interface SessionRowProps {
  session: SessionSummary;
  isActive: boolean;
  onClick: () => void;
  onCancel?: (runId: string) => void;
}

export function SessionRow({ session, isActive, onClick, onCancel }: SessionRowProps) {
  const label = session.title ?? session.repo.split("/").pop() ?? session.repo;
  // Show the link whenever the app is reachable. Keying on state === "live"
  // never matched: "live" is an *env* state, but session.state is the *run*
  // state (running/verifying/…), so the ↗ link was silently dead.
  const showUrl = !!session.web_url;
  const isQueued = session.state === "queued";

  return (
    <button
      className={`sidebar-session-row${isActive ? " is-active" : ""}`}
      onClick={onClick}
      title={session.repo}
    >
      <div className="sidebar-session-top">
        <span className="sidebar-session-label">{label}</span>
        <StateBadge state={session.state} />
        {isQueued && onCancel && (
          <span
            className="row-cancel"
            role="button"
            tabIndex={0}
            title="Cancel queued task"
            onClick={(e) => { e.stopPropagation(); onCancel(session.run_id); }}
          >
            ✕
          </span>
        )}
      </div>
      <div className="sidebar-session-meta">
        <span className="sidebar-session-repo mono">{session.repo}</span>
        {showUrl && (
          <a
            className="sidebar-session-url mono"
            href={session.web_url!}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            title={session.web_url!}
          >
            {session.web_url!.replace(/^https?:\/\//, "")}
            {" ↗"}
          </a>
        )}
      </div>
    </button>
  );
}
