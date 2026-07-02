import type { SessionSummary } from "./types";
import { SessionRow } from "./SessionRow";

interface SessionFolderProps {
  displayName: string;
  sessions: SessionSummary[];
  open: boolean;
  activeId: string | null;
  onToggle: () => void;
  onSelect: (runId: string) => void;
  onCancel?: (runId: string) => void;
}

/** One repo folder: a clickable header (caret + name + count) over a
 *  collapsible body of session rows. */
export function SessionFolder({
  displayName,
  sessions,
  open,
  activeId,
  onToggle,
  onSelect,
  onCancel,
}: SessionFolderProps) {
  return (
    <div className="sidebar-folder">
      <button
        className="sidebar-folder-header"
        onClick={onToggle}
        title={displayName}
        aria-expanded={open}
      >
        <span className={`sidebar-folder-caret${open ? " is-open" : ""}`}>▸</span>
        <span className="sidebar-folder-name mono">{displayName}</span>
        <span className="sidebar-folder-count">{sessions.length}</span>
      </button>
      {open && (
        <div className="sidebar-folder-body">
          {sessions.map((s) => (
            <SessionRow
              key={s.run_id}
              session={s}
              isActive={s.run_id === activeId}
              onClick={() => onSelect(s.run_id)}
              onCancel={onCancel}
            />
          ))}
        </div>
      )}
    </div>
  );
}
