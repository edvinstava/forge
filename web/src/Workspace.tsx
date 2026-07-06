import { useState, useEffect, useCallback } from "react";
import { AppFrame } from "./AppFrame";
import { AgentView } from "./AgentView";
import { Chat } from "./Chat";
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";
import { getConfig, getSession, getBrowserStatus } from "./api";
import { localPreviewUrl } from "./webUrl";
import { useSplitPane } from "./splitPane";
import { resolvePane, nextPin, cookieSafeWorkspaceUrl } from "./agentBrowser";
import type { PanePin } from "./agentBrowser";
import type { ProxyConfig, BrowserStatus } from "./types";

type Tab = "chat" | "diff" | "verify";

const BROWSER_POLL_MS = 1500;

const IDLE_BROWSER: BrowserStatus = { active: false, ts: 0, url: "", title: "" };

/**
 * Focused, deep-linkable view (#live=<run_id>): the running app on the left and
 * the agent chat on the right, split by a draggable gutter (default ~75/25,
 * persisted). Prompt the agent and watch the app update live. While a QA turn
 * screencasts the agent's browser, the left pane follows it (AgentView) so you
 * can watch the agent log in and click through the app; a pin toggle forces
 * either pane. Reuses AppFrame, Chat, and the diff/verify panels.
 */
export function Workspace({ runId }: { runId: string }) {
  const [webUrl, setWebUrl] = useState<string | null>(null);
  const [proxyConfig, setProxyConfig] = useState<ProxyConfig | null>(null);
  const [reloadSignal, setReloadSignal] = useState(0);
  const [tab, setTab] = useState<Tab>("chat");
  const [browser, setBrowser] = useState<BrowserStatus>(IDLE_BROWSER);
  const [pin, setPin] = useState<PanePin>("auto");
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

  // Agent-browser heartbeat: cheap local poll (frames bypass the SSE bus). A
  // new frame bumps `ts`, which re-renders the <img> in AgentView; hidden tabs
  // skip the fetch entirely.
  useEffect(() => {
    let stopped = false;
    const tick = () => {
      if (document.hidden) return;
      getBrowserStatus(runId)
        .then((s) => { if (!stopped) setBrowser(s); })
        .catch(() => { if (!stopped) setBrowser(IDLE_BROWSER); });
    };
    tick();
    const iv = setInterval(tick, BROWSER_POLL_MS);
    return () => { stopped = true; clearInterval(iv); };
  }, [runId]);

  const localUrl = localPreviewUrl(runId, webUrl, proxyConfig);

  const handleUrl = useCallback((url: string) => setWebUrl(url), []);
  const handleTurnDone = useCallback(() => {
    setReloadSignal((n) => n + 1);
    refreshUrl();
  }, [refreshUrl]);

  const pane = resolvePane(pin, browser.active);

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
        {/* AppFrame stays mounted while the agent view overlays it, so taking
            the pane back doesn't reload the app iframe (and lose its state). */}
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
        {pane === "agent" && <AgentView sessionId={runId} status={browser} />}
        {browser.active && (
          <div className="pane-toggle" role="tablist" aria-label="Left pane source">
            {(["app", "agent"] as const).map((p) => (
              <button
                key={p}
                className={`pane-toggle-btn${pane === p ? " is-active" : ""}`}
                onClick={() => setPin(nextPin(pin, browser.active, p))}
                title={p === "agent" ? "Watch the agent's browser live" : "Show the running app"}
              >
                {p === "agent" ? "● agent" : "app"}
              </button>
            ))}
          </div>
        )}
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
            <Chat key={runId} sessionId={runId} onUrl={handleUrl} onTurnDone={handleTurnDone} />
          </div>
          {tab === "diff" && <DiffView activeId={runId} />}
          {tab === "verify" && <VerifyView activeId={runId} />}
        </div>
      </aside>
    </div>
  );
}
