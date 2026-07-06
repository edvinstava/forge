import { useState, useEffect, useRef } from "react";

/** True when the SPA itself is being served from the forge host (so the
 *  DNS-free run-<id>.forge.localhost URL resolves here). A remote viewer who
 *  opened the workspace from a Slack link is NOT local, so they must embed the
 *  public tunnel URL instead. */
function isLocalHost(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname.endsWith(".localhost") ||
    hostname === "forge.localhost"
  );
}

/** Pick the iframe src by where THIS page loaded from, plus a public share link
 *  to offer when the embedded src isn't already the public URL. Pure. */
export function resolveEmbedSrc(args: {
  locationHostname: string;
  webUrl: string | null;
  localUrl: string | null;
}): { src: string | null; share: string | null } {
  const { locationHostname, webUrl, localUrl } = args;
  const preferLocal = isLocalHost(locationHostname);
  const src = (preferLocal ? localUrl ?? webUrl : webUrl ?? localUrl) ?? null;
  const share = webUrl && webUrl !== src ? webUrl : null;
  return { src, share };
}

type FrameState = "pending" | "ok" | "failed";

export function AppFrame({
  webUrl,
  localUrl,
  reloadSignal = 0,
}: {
  webUrl: string | null;
  localUrl: string | null;
  reloadSignal?: number;
}) {
  const { src, share } = resolveEmbedSrc({
    locationHostname: window.location.hostname,
    webUrl,
    localUrl,
  });

  const [frameState, setFrameState] = useState<FrameState>("pending");
  // `reloadKey` bumps to force a fresh iframe load; `triedAlt` tracks the
  // one-shot fallback to the other URL after a load failure.
  const [reloadKey, setReloadKey] = useState(0);
  const [effectiveSrc, setEffectiveSrc] = useState(src);
  const [triedAlt, setTriedAlt] = useState(false);
  const [nudge, setNudge] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nudgeRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstSignal = useRef(reloadSignal);

  const alt = share && share !== effectiveSrc ? share : null;

  function handleFail() {
    if (!triedAlt && alt) {
      setTriedAlt(true);
      setEffectiveSrc(alt);
      setFrameState("pending");
      return;
    }
    setFrameState("failed");
  }

  // Reset when the resolved src changes (new session / URL surfaced).
  useEffect(() => {
    setEffectiveSrc(src);
    setTriedAlt(false);
    setFrameState("pending");
  }, [src]);

  // Soft-nudge auto-refresh: on a turn-completion signal (not the initial
  // value), show the pill and reload the iframe ONCE after a settle delay.
  useEffect(() => {
    if (reloadSignal === firstSignal.current) return;
    if (!effectiveSrc) return;
    setNudge(true);
    nudgeRef.current = setTimeout(() => {
      setReloadKey((k) => k + 1);
      setFrameState("pending");
      setNudge(false);
    }, 1500);
    return () => {
      if (nudgeRef.current) clearTimeout(nudgeRef.current);
    };
  }, [reloadSignal, effectiveSrc]);

  // Load-failure timeout (6s), with a one-shot swap to the alternate URL.
  useEffect(() => {
    if (effectiveSrc && frameState === "pending") {
      timeoutRef.current = setTimeout(() => handleFail(), 6000);
    }
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveSrc, frameState, reloadKey]);

  const handleLoad = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    if (frameState === "pending") setFrameState("ok");
  };

  const manualRefresh = () => {
    setReloadKey((k) => k + 1);
    setFrameState("pending");
  };

  if (!effectiveSrc) {
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
        <a href={effectiveSrc} target="_blank" rel="noopener noreferrer" className="btn btn-accent">
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
        <span className="preview-url" title={effectiveSrc}>
          {effectiveSrc.replace(/^https?:\/\//, "")}
        </span>
        {nudge && <span className="preview-nudge">↗ updated · refreshing…</span>}
        <button className="btn btn-sm" onClick={manualRefresh} title="Reload">↻</button>
        <a href={effectiveSrc} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Open in new tab">↗</a>
        {share && (
          <a href={share} target="_blank" rel="noopener noreferrer" className="btn btn-sm" title="Public share link (no DNS needed locally)">🌐</a>
        )}
      </div>
      <iframe
        key={reloadKey}
        src={effectiveSrc}
        className="preview-iframe"
        title="App preview"
        onLoad={handleLoad}
        onError={handleFail}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"
      />
    </div>
  );
}
