import { useState, useEffect, useCallback, useRef } from "react";
import { AppFrame } from "./AppFrame";
import { AgentView } from "./AgentView";
import { FilesView } from "./FilesView";
import { Chat } from "./Chat";
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";
import { getConfig, getSession, getBrowserStatus } from "./api";
import { localPreviewUrl } from "./webUrl";
import { useSplitPane } from "./splitPane";
import { resolvePane, nextPin, nextEpoch, cookieSafeWorkspaceUrl } from "./agentBrowser";
import type { PanePin, Pane } from "./agentBrowser";
import { touchAction, pushTouch, type FileTouch } from "./filesModel";
import type { ProxyConfig, BrowserStatus } from "./types";

type Tab = "chat" | "diff" | "verify";

const BROWSER_POLL_MS = 1000;

const IDLE_BROWSER: BrowserStatus = { active: false, ts: 0, url: "", title: "" };

/**
 * Focused, deep-linkable view (#live=<run_id>): the running app on the left and
 * the agent chat on the right, split by a draggable gutter (default ~75/25,
 * persisted). Prompt the agent and watch the app update live. The left pane
 * follows the agent: while a turn screencasts the agent's browser it shows
 * that (AgentView); while a turn is editing files with no browser up it shows
 * the live files view (FilesView) so you can watch edits land; idle, it shows
 * the app. A pin toggle forces any pane. Reuses AppFrame, Chat, and the
 * diff/verify panels.
 */
export function Workspace({ runId }: { runId: string }) {
  const [webUrl, setWebUrl] = useState<string | null>(null);
  const [proxyConfig, setProxyConfig] = useState<ProxyConfig | null>(null);
  const [reloadSignal, setReloadSignal] = useState(0);
  const [tab, setTab] = useState<Tab>("chat");
  const [browser, setBrowser] = useState<BrowserStatus>(IDLE_BROWSER);
  const [pin, setPin] = useState<PanePin>("auto");
  // Screencast generation: bumps on each inactive→active edge so AgentView
  // opens a fresh MJPEG connection per screencast (the old stream has ended).
  const [epoch, setEpoch] = useState(0);
  const prevActive = useRef(false);
  // File-touching tool calls streamed by the current turn (via Chat) — feeds
  // the files pane; editsLive keeps auto mode on it until the turn completes.
  const [touches, setTouches] = useState<FileTouch[]>([]);
  const [editsLive, setEditsLive] = useState(false);
  // Files pane stays mounted once visited so tree state survives pane flips.
  const [filesMounted, setFilesMounted] = useState(false);
  const { splitPct, shellRef, gutterHandlers } = useSplitPane();

  useEffect(() => {
    getConfig().then(setProxyConfig).catch(() => {});
  }, []);

  // Same-site hop: embedded-app logins only keep their session cookies when
  // this page shares a registrable domain with the run-<id>.<proxy_domain>
  // iframe — opened from 127.0.0.1/localhost, jump to the proxy-domain host
  // once (see cookieSafeWorkspaceUrl).
  useEffect(() => {
    if (!proxyConfig) return;
    const to = cookieSafeWorkspaceUrl(window.location, proxyConfig.proxy_domain);
    if (to) window.location.replace(to);
  }, [proxyConfig]);

  // Resolve the app URL from the persisted session on mount and whenever a
  // turn completes (a wake/restart can change it).
  const refreshUrl = useCallback(() => {
    getSession(runId)
      .then((s) => setWebUrl((prev) => s.web_url ?? prev))
      .catch(() => {});
  }, [runId]);
  useEffect(() => { refreshUrl(); }, [refreshUrl]);

  // Agent-browser status poll: gates the pane switch and feeds url/title.
  // Frames themselves arrive over the MJPEG stream in AgentView (this poll is
  // only their fallback cache-buster); hidden tabs skip the fetch entirely.
  useEffect(() => {
    let stopped = false;
    const apply = (s: BrowserStatus) => {
      if (stopped) return;
      setEpoch((e) => nextEpoch(prevActive.current, s.active, e));
      prevActive.current = s.active;
      setBrowser(s);
    };
    const tick = () => {
      if (document.hidden) return;
      getBrowserStatus(runId)
        .then(apply)
        .catch(() => apply(IDLE_BROWSER));
    };
    tick();
    const iv = setInterval(tick, BROWSER_POLL_MS);
    return () => { stopped = true; clearInterval(iv); };
  }, [runId]);

  const localUrl = localPreviewUrl(runId, webUrl, proxyConfig);

  const handleUrl = useCallback((url: string) => setWebUrl(url), []);
  const handleTurnDone = useCallback(() => {
    setReloadSignal((n) => n + 1);
    setEditsLive(false);
    refreshUrl();
  }, [refreshUrl]);

  // Tool events with a workspace path (Edit src/x.ts, Read …) — whichever
  // surface drives the turn, Chat's feed relays them here.
  const handleFile = useCallback((name: string, path: string) => {
    const action = touchAction(name);
    if (!action || !path) return;
    setTouches((ts) => pushTouch(ts, { path, action, ts: Date.now() }));
    if (action === "edit") setEditsLive(true);
  }, []);

  const pane = resolvePane(pin, browser.active, editsLive);
  useEffect(() => {
    if (pane === "files") setFilesMounted(true);
  }, [pane]);

  const TABS: { id: Tab; label: string }[] = [
    { id: "chat", label: "chat" },
    { id: "diff", label: "diff" },
    { id: "verify", label: "verify" },
  ];

  return (
    <div
      className="workspace-shell"
      ref={shellRef}
      style={{ ["--split" as string]: splitPct }}
    >
      <section className="workspace-app">
        {/* AppFrame (and, once visited, FilesView) stay mounted while another
            pane overlays them, so taking the pane back doesn't reload the app
            iframe or drop the file tree's state. */}
        <div
          style={{
            display: pane === "app" ? "flex" : "none",
            flexDirection: "column",
            flex: 1,
            minHeight: 0,
          }}
        >
          <AppFrame webUrl={webUrl} localUrl={localUrl} reloadSignal={reloadSignal} />
        </div>
        {filesMounted && (
          <div
            style={{
              display: pane === "files" ? "flex" : "none",
              flexDirection: "column",
              flex: 1,
              minHeight: 0,
            }}
          >
            <FilesView sessionId={runId} touches={touches} reloadSignal={reloadSignal} />
          </div>
        )}
        {pane === "agent" && <AgentView sessionId={runId} status={browser} epoch={epoch} />}
        <div className="pane-toggle" role="tablist" aria-label="Left pane source">
          {(["app", "files", "agent"] as Pane[])
            .filter((p) => p !== "agent" || browser.active)
            .map((p) => (
              <button
                key={p}
                className={`pane-toggle-btn${pane === p ? " is-active" : ""}`}
                onClick={() => setPin(nextPin(pin, browser.active, p, editsLive))}
                title={
                  p === "agent" ? "Watch the agent's browser live"
                    : p === "files" ? "Watch the agent edit workspace files"
                    : "Show the running app"
                }
              >
                {p === "agent" ? "● agent" : p === "files" ? "⦿ files" : "app"}
              </button>
            ))}
        </div>
      </section>
      <div
        className="workspace-gutter"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize app and chat panes"
        title="Drag to resize · double-click to reset"
        {...gutterHandlers}
      />
      <aside className="workspace-control">
        <div className="inspector-tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`inspector-tab${tab === t.id ? " is-active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
          <a className="inspector-tab workspace-exit" href="#" title="Back to dashboard">▸ dashboard</a>
        </div>
        <div className="workspace-control-body">
          {/* Chat stays mounted across tab switches so the live stream isn't
              interrupted; diff/verify overlay it. */}
          <div
            style={{
              display: tab === "chat" ? "flex" : "none",
              flex: 1,
              minHeight: 0,
              flexDirection: "column",
            }}
          >
            <Chat key={runId} sessionId={runId} onUrl={handleUrl} onTurnDone={handleTurnDone} onFile={handleFile} />
          </div>
          {tab === "diff" && <DiffView activeId={runId} />}
          {tab === "verify" && <VerifyView activeId={runId} />}
        </div>
      </aside>
    </div>
  );
}
