import { useState, useEffect, useCallback, useRef } from "react";
import { Sidebar } from "./Sidebar";
import { Chat } from "./Chat";
import { Inspector } from "./Inspector";
import type { NewSessionPayload } from "./Sidebar";
import type { SessionSummary, SseEvent, ProxyConfig } from "./types";
import { getConfig, listSessions, streamPost, endSession } from "./api";
import { parseSessionHash, sessionHash } from "./deepLink";
import { pickWebUrl, localPreviewUrl } from "./webUrl";

const POLL_INTERVAL_MS = 4000;

/**
 * App↔Chat provisioning contract:
 *   - While a new session is being provisioned, App collects SSE events
 *     in `provisioningEvents` keyed by run_id.
 *   - Chat receives the events for the active session and renders a live
 *     system bubble accumulating phase/narration lines until url/done/error.
 *   - Once provisioning ends, the array stays frozen (Chat ignores it after
 *     the stream closes) and getSession reloads the persisted transcript.
 */

export function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  // Deep link: #s=<run_id> (e.g. the 🧭 link in a Slack thread) opens straight
  // into that session; selecting one keeps the URL shareable.
  const [activeId, setActiveId] = useState<string | null>(
    () => parseSessionHash(window.location.hash)
  );
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const activate = useCallback((runId: string) => {
    setActiveId(runId);
    window.history.replaceState(null, "", sessionHash(runId));
  }, []);

  useEffect(() => {
    const onHash = () => {
      const id = parseSessionHash(window.location.hash);
      if (id) setActiveId(id);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  /**
   * Map of run_id → accumulated provisioning SSE events.
   * We keep the whole array so Chat can diff against the previous length
   * and process only new events on each render.
   */
  const [provisioningEvents, setProvisioningEvents] = useState<
    Record<string, SseEvent[]>
  >({});

  /** web_url surfaced by the active session (from SSE url events or session poll) */
  const [webUrl, setWebUrl] = useState<string | null>(null);

  /** proxy settings (fetched once) used to derive the DNS-free local preview URL */
  const [proxyConfig, setProxyConfig] = useState<ProxyConfig | null>(null);
  useEffect(() => {
    getConfig().then(setProxyConfig).catch(() => {});
  }, []);
  const localUrl = localPreviewUrl(activeId, webUrl, proxyConfig);

  /* ── Fetch sessions ── */
  const refreshSessions = useCallback(async () => {
    try {
      const data = await listSessions();
      setSessions(data);
    } catch {
      // silently ignore — server may not be running during dev
    }
  }, []);

  /* ── Polling ── */
  useEffect(() => {
    refreshSessions();
    intervalRef.current = setInterval(refreshSessions, POLL_INTERVAL_MS);
    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [refreshSessions]);

  /* ── Keep the Inspector's web_url in sync with the poll ──
   * Without this, a session that becomes live in the background (e.g. started
   * from Slack, or selected while still provisioning) keeps webUrl=null and the
   * Preview pane never loads — even though the sidebar shows the live link. */
  useEffect(() => {
    setWebUrl((prev) => pickWebUrl(prev, sessions, activeId));
  }, [sessions, activeId]);

  /* ── Select a session ── */
  const handleSelect = useCallback((runId: string) => {
    activate(runId);
    // Resolve web_url from the already-loaded session list on switch
    const s = sessions.find((x) => x.run_id === runId);
    setWebUrl(s?.web_url ?? null);
  }, [sessions, activate]);

  /* ── Create a new session ── */
  const handleNewSession = useCallback(
    async ({ repo, source }: NewSessionPayload) => {
      let capturedId: string | null = null;

      const onEvent = (e: SseEvent) => {
        if (e.kind === "session" && e.data?.run_id) {
          capturedId = e.data.run_id as string;
          activate(capturedId);
          // Initialise the event bucket for this run
          setProvisioningEvents((prev) => ({ ...prev, [capturedId!]: [] }));
          return;
        }

        // Route all other events into the bucket for the captured id
        if (capturedId) {
          setProvisioningEvents((prev) => ({
            ...prev,
            [capturedId!]: [...(prev[capturedId!] ?? []), e],
          }));
        }
      };

      try {
        await streamPost("/api/sessions", { repo, source }, onEvent);
      } catch {
        // Stream error — server will have persisted partial state
      } finally {
        // Signal end-of-provisioning so Chat closes the phase checklist even when
        // the stream ends without a url/done (e.g. a worker-only "noweb" repo).
        if (capturedId) {
          setProvisioningEvents((prev) => ({
            ...prev,
            [capturedId!]: [...(prev[capturedId!] ?? []), { kind: "done", data: {} }],
          }));
        }
        await refreshSessions();
      }
    },
    [refreshSessions, activate]
  );

  /* ── Cancel a queued run (no env to tear down; server marks it canceled) ── */
  const handleCancel = useCallback(async (runId: string) => {
    try {
      await endSession(runId);
    } finally {
      refreshSessions();
    }
  }, [refreshSessions]);

  /* ── Callbacks forwarded to Chat ── */
  const handleUrl = useCallback((url: string) => {
    setWebUrl(url);
  }, []);

  const handleTurnDone = useCallback(() => {
    refreshSessions();
  }, [refreshSessions]);

  /* ── Render ── */
  return (
    <div className="app-shell">
      {/* Sidebar */}
      <aside className="app-sidebar">
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          onSelect={handleSelect}
          onNewSession={handleNewSession}
          onRefresh={refreshSessions}
          onCancel={handleCancel}
        />
      </aside>

      {/* Center: Chat pane */}
      <main className="app-main" style={{ alignItems: "stretch", justifyContent: "stretch" }}>
        {activeId ? (
          <Chat
            key={activeId}
            sessionId={activeId}
            provisioningEvents={provisioningEvents[activeId]}
            onUrl={handleUrl}
            onTurnDone={handleTurnDone}
          />
        ) : (
          <div className="pane-placeholder">
            <div className="pane-placeholder-icon">⬝</div>
            <div className="pane-placeholder-label">select or create a session</div>
          </div>
        )}
      </main>

      {/* Right: Inspector pane */}
      <aside className="app-inspector">
        <Inspector activeId={activeId} webUrl={webUrl} localUrl={localUrl} />
      </aside>
    </div>
  );
}
