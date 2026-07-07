import { useEffect, useState } from "react";
import type { BrowserStatus } from "./types";
import { browserFrameUrl, browserStreamUrl } from "./agentBrowser";

/**
 * The live agent-browser pane: an MJPEG stream of the browser the QA agent is
 * driving — frames render as the backend pushes them, no poll round-trip. If
 * the long-lived request dies (proxy buffering, network hiccup) the <img>
 * error handler drops to the poll-and-swap fallback (refetch on every status
 * `ts` bump) and retries the stream when the next screencast starts (`epoch`).
 */
export function AgentView({
  sessionId,
  status,
  epoch,
}: {
  sessionId: string;
  status: BrowserStatus;
  epoch: number;
}) {
  const [pollFallback, setPollFallback] = useState(false);
  useEffect(() => setPollFallback(false), [epoch]);
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
          key={pollFallback ? "poll" : `stream-${epoch}`}
          src={
            pollFallback
              ? browserFrameUrl(sessionId, status.ts)
              : browserStreamUrl(sessionId, epoch)
          }
          onError={() => setPollFallback(true)}
          alt="Agent browser (live)"
        />
      </div>
    </div>
  );
}
