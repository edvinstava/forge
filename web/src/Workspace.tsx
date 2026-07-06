import { useState, useEffect, useCallback } from "react";
import { AppFrame } from "./AppFrame";
import { Chat } from "./Chat";
import { DiffView } from "./DiffView";
import { VerifyView } from "./VerifyView";
import { getConfig, getSession } from "./api";
import { localPreviewUrl } from "./webUrl";
import type { ProxyConfig } from "./types";

type Tab = "chat" | "diff" | "verify";

/**
 * Focused, deep-linkable view (#live=<run_id>): the running app on the left
 * (~75%) and the agent chat on the right (~25%). Prompt the agent and watch the
 * app update live. Reuses AppFrame, Chat, and the diff/verify panels.
 */
export function Workspace({ runId }: { runId: string }) {
  const [webUrl, setWebUrl] = useState<string | null>(null);
  const [proxyConfig, setProxyConfig] = useState<ProxyConfig | null>(null);
  const [reloadSignal, setReloadSignal] = useState(0);
  const [tab, setTab] = useState<Tab>("chat");

  useEffect(() => {
    getConfig().then(setProxyConfig).catch(() => {});
  }, []);

  // Resolve the app URL from the persisted session on mount and whenever a
  // turn completes (a wake/restart can change it).
  const refreshUrl = useCallback(() => {
    getSession(runId)
      .then((s) => setWebUrl((prev) => s.web_url ?? prev))
      .catch(() => {});
  }, [runId]);
  useEffect(() => { refreshUrl(); }, [refreshUrl]);

  const localUrl = localPreviewUrl(runId, webUrl, proxyConfig);

  const handleUrl = useCallback((url: string) => setWebUrl(url), []);
  const handleTurnDone = useCallback(() => {
    setReloadSignal((n) => n + 1);
    refreshUrl();
  }, [refreshUrl]);

  const TABS: { id: Tab; label: string }[] = [
    { id: "chat", label: "chat" },
    { id: "diff", label: "diff" },
    { id: "verify", label: "verify" },
  ];

  return (
    <div className="workspace-shell">
      <section className="workspace-app">
        <AppFrame webUrl={webUrl} localUrl={localUrl} reloadSignal={reloadSignal} />
      </section>
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
