import type { BrowserStatus } from "./types";
import { browserFrameUrl } from "./agentBrowser";

/**
 * The live agent-browser pane: the newest screencast frame of the browser the
 * QA agent is driving, refreshed whenever the polled status reports a new
 * frame (ts is the cache-buster). Presentational — polling lives in Workspace.
 */
export function AgentView({
  sessionId,
  status,
}: {
  sessionId: string;
  status: BrowserStatus;
}) {
  return (
    <div className="preview-wrap">
      <div className="preview-toolbar agent-toolbar">
        <span className="agent-live-dot" />
        <span className="agent-live-label">watching the agent</span>
        <span className="preview-url" title={status.url}>
          {status.url.replace(/^https?:\/\//, "")}
        </span>
        {status.title && <span className="agent-page-title">{status.title}</span>}
      </div>
      <div className="agent-frame-wrap">
        <img
          className="agent-frame"
          src={browserFrameUrl(sessionId, status.ts)}
          alt="Agent browser (live)"
        />
      </div>
    </div>
  );
}
