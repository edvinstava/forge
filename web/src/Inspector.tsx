import { useState, useEffect, useRef } from "react";
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";

/* ── Types ── */
interface InspectorProps {
  activeId: string | null;
  webUrl: string | null;
  /** DNS-free http://run-<id>.forge.localhost URL; preferred for the embed since
   *  it always resolves on the forge host even when the tunnel hostname won't. */
  localUrl: string | null;
}

type Tab = "preview" | "diff" | "verify";

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
        {tab === "diff" && <DiffView activeId={activeId} />}
        {tab === "verify" && <VerifyView activeId={activeId} />}
      </div>
    </div>
  );
}
