import React, {
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
  useReducer,
} from "react";
import type { SessionDetail, Message, SseEvent, ModelChoice } from "./types";
import { useModelChoices } from "./modelChoices";
import { getSession, streamPost, openPr, stopTurn, endSession, sleepSession, wakeSession, startTask, respondCheckpoint, attachEvents, uploadAttachment } from "./api";
import { reducer, initialChatState, uid, type Bubble } from "./chatReducer";
import { gateFeedEvent, feedOrigin, answeredNote, inflightTail, CLOSER_KINDS } from "./liveFeed";
import { normalizeCheckpoint, normalizePlan } from "./coworker";
import { PlanCard } from "./PlanCard";
import type { CheckpointAction } from "./types";


/* ─────────────────────────────────────────────────────────────────────────────
   Props
   ───────────────────────────────────────────────────────────────────────────── */

export interface ChatProps {
  sessionId: string;
  provisioningEvents?: SseEvent[];
  onUrl?: (url: string) => void;
  onTurnDone?: () => void;
  /** A tool event touched a workspace file (name = tool, path = repo-relative)
   *  — feeds the workspace's live files pane, foreign turns included. */
  onFile?: (name: string, path: string) => void;
}

/* ── persisted messages → bubbles ── */
function messagesToBubbles(messages: Message[]): Bubble[] {
  return messages.map((m) => ({ id: uid(), role: m.role, content: m.content }));
}

/* A queued image attachment. `uploadedName` is set once the file has been
   uploaded to the session's workspace — a re-attempted send (e.g. after a
   later file in the same batch fails to upload) skips re-uploading it. */
interface PendingAttachment {
  id: string;
  file: File;
  uploadedName?: string;
}

/* Read a web_url from a url SSE event. Backend sends {web_url}; we also accept
   {url} defensively so either shape works. (This mismatch was the original
   reason the Preview tab never received a URL.) */
function readUrl(data: any): string | null {
  if (typeof data === "string") return data;
  return data?.web_url ?? data?.url ?? null;
}

/* ─────────────────────────────────────────────────────────────────────────────
   Chat
   ───────────────────────────────────────────────────────────────────────────── */

export function Chat({ sessionId, provisioningEvents, onUrl, onTurnDone, onFile }: ChatProps) {
  const [state, dispatch] = useReducer(reducer, undefined, initialChatState);

  const [input, setInput] = useState("");
  const [model, setModel] = useState<ModelChoice>("auto");
  const [mode, setMode] = useState<"task" | "chat">("task");
  const [session, setSession] = useState<SessionDetail | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pendingFiles, setPendingFiles] = useState<PendingAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  /* ── Queue images picked via drag-drop / paste / the 📎 picker; capped at 5,
     silently dropping non-image files (no error — the picker itself filters
     via `accept="image/*"`, this guards paste/drag). ── */
  const addFiles = useCallback((incoming: FileList | File[]) => {
    const imgs = Array.from(incoming).filter((f) => f.type.startsWith("image/"));
    if (imgs.length)
      setPendingFiles((cur) => [...cur, ...imgs.map((file) => ({ id: uid(), file }))].slice(0, 5));
  }, []);

  /* ── Thumbnail preview URLs: one object URL per pending file, keyed by the
     attachment's stable `id` (not the array reference) so marking a file's
     `uploadedName` mid-send doesn't recreate every other thumbnail's URL, and
     unrelated re-renders (e.g. every keystroke in the textarea) never touch
     them at all. Revocation happens only here — in the effect's cleanup, when
     the id set changes or the component unmounts — never on <img onLoad>,
     which used to miss removal-before-load, clear-on-send and decode errors. */
  const pendingIds = pendingFiles.map((p) => p.id).join(",");
  const previewUrls = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of pendingFiles) map.set(p.id, URL.createObjectURL(p.file));
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingIds]);
  useEffect(() => {
    return () => { previewUrls.forEach((url) => URL.revokeObjectURL(url)); };
  }, [previewUrls]);

  /* ── Load session + transcript ── */
  const loadSession = useCallback(async () => {
    try {
      const s = await getSession(sessionId);
      setSession(s);
      dispatch({ type: "RESET", bubbles: messagesToBubbles(s.messages) });
      // Reconnect resilience: rebuild a pending approval gate from persisted state
      // (reload / tab-switch / wake mid-gate never loses the open checkpoint).
      dispatch({ type: "SET_GATE", plan: normalizePlan(s.plan), checkpoint: normalizeCheckpoint(s.checkpoint) });
    } catch {
      /* dev: server may be momentarily unavailable */
    }
  }, [sessionId]);

  /* ── Provisioning events → live system bubble (phase checklist) ── */
  const provisioningOpenRef = useRef(false);
  const prevProvisioningLengthRef = useRef(0);

  useEffect(() => {
    prevProvisioningLengthRef.current = 0;
    provisioningOpenRef.current = false;
    setPendingFiles([]);
  }, [sessionId]);

  useEffect(() => {
    if (!provisioningEvents || provisioningEvents.length === 0) return;
    const newEvents = provisioningEvents.slice(prevProvisioningLengthRef.current);
    prevProvisioningLengthRef.current = provisioningEvents.length;

    const open = () => {
      if (!provisioningOpenRef.current) {
        provisioningOpenRef.current = true;
        dispatch({ type: "START_LIVE", id: uid(), role: "system" });
      }
    };
    const close = (final?: Partial<Bubble>, stepStatus?: "done" | "error") => {
      if (provisioningOpenRef.current) {
        provisioningOpenRef.current = false;
        dispatch({ type: "CLOSE_LIVE", final, stepStatus });
      }
    };

    for (const e of newEvents) {
      if (e.kind === "phase") {
        const label =
          typeof e.data === "string" ? e.data : e.data?.label ?? e.data?.name ?? "working";
        open();
        dispatch({ type: "LIVE_STEP", label });
      } else if (e.kind === "narration" || e.kind === "tool") {
        const text =
          typeof e.data === "string" ? e.data : e.data?.text ?? e.data?.message ?? "";
        open();
        if (text) dispatch({ type: "LIVE_TEXT", text: text + "\n" });
      } else if (e.kind === "url") {
        const url = readUrl(e.data);
        if (url && onUrl) onUrl(url);
        close();
      } else if (e.kind === "done") {
        close();
      } else if (e.kind === "error") {
        const msg =
          typeof e.data === "string"
            ? e.data
            : e.data?.detail ?? e.data?.message ?? "Provisioning failed";
        if (provisioningOpenRef.current) {
          close({ variant: "error", content: msg }, "error");
        } else {
          dispatch({
            type: "APPEND_SYSTEM",
            bubble: { id: uid(), role: "system", content: msg, variant: "error" },
          });
        }
      }
    }
  }, [provisioningEvents, onUrl]);

  /* ── Auto-scroll ── */
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [state.bubbles]);

  /* ── Live feed: follow turns driven from OTHER surfaces (Slack, CLI, another
     tab) via GET /events. Events this tab renders through its own POST streams
     only advance the seq high-water mark (see liveFeed.gateFeedEvent); foreign
     turns open a live bubble and route through the same event switch, so a
     Slack-driven build streams here in real time — plan, checkpoint gate and
     all. ── */
  const lastSeqRef = useRef(-1);
  const selfDrivingRef = useRef(false);
  const foreignOpenRef = useRef(false);
  const [foreignLive, setForeignLive] = useState(false);
  const feedHandlerRef = useRef<(e: SseEvent) => void>(() => {});

  // Aborts this session's in-flight POST streams (task / checkpoint / wake)
  // when the pane unmounts — App keys <Chat> on the session id, so switching
  // sessions tears the old stream down instead of letting it keep dispatching
  // into (and firing onUrl/onTurnDone for) the session you navigated away from.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    return () => ctrl.abort();
  }, []);

  // While App's provisioning stream renders this session, feed copies of those
  // events must not double-render.
  const provisioningActive =
    provisioningEvents !== undefined &&
    !provisioningEvents.some((e) => e.kind === "done" || e.kind === "error");
  const provisioningActiveRef = useRef(provisioningActive);
  provisioningActiveRef.current = provisioningActive;

  /* Reassigned every render so the long-lived feed loop sees fresh closures. */
  feedHandlerRef.current = (e: SseEvent) => {
    const d = gateFeedEvent(
      e, lastSeqRef.current,
      selfDrivingRef.current || provisioningActiveRef.current,
      foreignOpenRef.current,
    );
    lastSeqRef.current = d.lastSeq;
    if (!d.apply) return;
    if (e.kind === "checkpoint_answered") {
      // Answered on the other surface — drop our gate; progress follows live.
      dispatch({ type: "CLEAR_GATE" });
      dispatch({
        type: "APPEND_SYSTEM",
        bubble: { id: uid(), role: "system", content: answeredNote(e) },
      });
      return;
    }
    if (d.opens) {
      foreignOpenRef.current = true;
      setForeignLive(true);
      dispatch({ type: "START_LIVE", id: uid(), role: "assistant" });
      dispatch({ type: "LIVE_TEXT", text: `⇄ live — driven from ${feedOrigin(e)}\n` });
    }
    const wasForeign = foreignOpenRef.current || d.opens;
    makeOnEvent()(e);
    // Some flows end without a `done` (wake stops at url, plan_task at
    // checkpoint, plus the bus-only stream_end signal): close the live bubble
    // so the gate / sleep banner / composer take over. Only when a foreign
    // bubble is actually open — stragglers after our own turn are no-ops.
    if (wasForeign
        && (e.kind === "checkpoint" || e.kind === "slept" || e.kind === "stream_end")) {
      dispatch({ type: "CLOSE_LIVE" });
      if (e.kind === "slept" || e.kind === "stream_end") loadSession();
    }
    if (CLOSER_KINDS.has(e.kind)) {
      foreignOpenRef.current = false;
      setForeignLive(false);
    }
  };

  useEffect(() => {
    let stopped = false;
    const ctrl = new AbortController();
    lastSeqRef.current = -1;
    foreignOpenRef.current = false;
    setForeignLive(false);
    (async () => {
      // Transcript first: RESET must land before any replayed live events, or
      // it would wipe the rebuilt bubble.
      await loadSession();
      if (stopped) return;
      // The persisted transcript only covers *completed* turns. If a turn is
      // in flight (e.g. driven from Slack), everything it has streamed so far
      // lives only in the bus buffer — fetch the backlog and replay the
      // in-flight tail, so a refresh mid-turn doesn't start from a blank
      // bubble. Older backlog events just advance the high-water mark.
      try {
        const backlog: SseEvent[] = [];
        await attachEvents(sessionId, 0, (e) => backlog.push(e), ctrl.signal, 0);
        if (stopped) return;
        for (const e of inflightTail(backlog)) feedHandlerRef.current(e);
        for (const e of backlog) {
          const seq = e.data?.seq;
          if (typeof seq === "number" && seq > lastSeqRef.current) lastSeqRef.current = seq;
        }
      } catch {
        /* best-effort — the live tail below still attaches from now */
      }
      while (!stopped) {
        try {
          await attachEvents(sessionId, lastSeqRef.current,
                             (e) => feedHandlerRef.current(e), ctrl.signal);
        } catch {
          /* server restart / network blip — retry below */
        }
        if (!stopped) await new Promise((r) => setTimeout(r, 3000));
      }
    })();
    return () => { stopped = true; ctrl.abort(); };
  }, [sessionId, loadSession]);

  /* After our own POST stream ends, the feed's duplicate copies of that turn
     may still be in flight; snap the high-water mark to the server's current
     seq so they can never replay as a phantom "foreign" turn. */
  const catchUpSeq = useCallback(async () => {
    try {
      await attachEvents(sessionId, Math.max(lastSeqRef.current, 0), (e) => {
        const seq = e.data?.seq;
        if (typeof seq === "number" && seq > lastSeqRef.current) lastSeqRef.current = seq;
      }, undefined, 0);
    } catch { /* best-effort */ }
  }, [sessionId]);

  /* ── Wake a sleeping session (re-provision) ── */
  const runWakeStream = useCallback(async (): Promise<void> => {
    let opened = false;
    const open = () => {
      if (!opened) { opened = true; dispatch({ type: "START_LIVE", id: uid(), role: "system" }); }
    };
    const onEvent = (e: SseEvent) => {
      switch (e.kind) {
        case "phase": {
          const label =
            typeof e.data === "string" ? e.data : e.data?.label ?? e.data?.name ?? "working";
          open();
          dispatch({ type: "LIVE_STEP", label });
          break;
        }
        case "narration":
        case "tool": {
          const text = typeof e.data === "string" ? e.data : e.data?.text ?? "";
          open();
          if (text) dispatch({ type: "LIVE_TEXT", text: text + "\n" });
          break;
        }
        case "url": {
          const url = readUrl(e.data);
          if (url && onUrl) onUrl(url);
          dispatch({ type: "CLOSE_LIVE" });
          break;
        }
        case "done":
          dispatch({ type: "CLOSE_LIVE" });
          break;
        case "error": {
          const msg =
            typeof e.data === "string" ? e.data : e.data?.detail ?? e.data?.message ?? "Wake failed";
          if (opened) dispatch({ type: "CLOSE_LIVE", final: { variant: "error", content: msg }, stepStatus: "error" });
          else dispatch({ type: "APPEND_SYSTEM", bubble: { id: uid(), role: "system", content: msg, variant: "error" } });
          break;
        }
      }
    };
    await wakeSession(sessionId, onEvent, abortRef.current?.signal);
    const s = await getSession(sessionId).catch(() => null);
    if (s) setSession(s);
    onTurnDone?.();
  }, [sessionId, onUrl, onTurnDone]);

  const handleWake = useCallback(async () => {
    dispatch({ type: "SET_STREAMING", value: true });
    selfDrivingRef.current = true;
    try { await runWakeStream(); } finally {
      selfDrivingRef.current = false;
      catchUpSeq();
      dispatch({ type: "SET_STREAMING", value: false });
    }
  }, [runWakeStream, catchUpSeq]);

  const handleSleep = useCallback(async () => {
    try {
      await sleepSession(sessionId);
      const s = await getSession(sessionId).catch(() => null);
      if (s) setSession(s);
      onTurnDone?.();
    } catch {}
  }, [sessionId, onTurnDone]);

  /* ── Shared SSE handler ── used by the Plan & build task stream, the
     checkpoint-response stream, and the plain Chat turn. The coworker kinds
     (plan/checkpoint/repair/qa/retrospective) simply never fire on a Chat turn. */
  const makeOnEvent = useCallback(
    () => (e: SseEvent) => {
      switch (e.kind) {
        case "model":
          dispatch({ type: "LIVE_MODEL", model: e.data?.resolved ?? e.data?.choice ?? "" });
          break;
        case "phase":
          break; // the live cursor already conveys "working"
        case "narration":
          dispatch({ type: "LIVE_TEXT", text: (e.data?.text ?? "") + "\n" });
          break;
        case "tool":
          dispatch({ type: "LIVE_TOOL", name: e.data?.name ?? e.data?.text ?? "tool", target: e.data?.target });
          if (e.data?.path && onFile) onFile(e.data?.name ?? "", e.data.path);
          break;
        case "verify":
          dispatch({ type: "LIVE_VERIFY", pass: Boolean(e.data?.ok) });
          break;
        case "repair":
          dispatch({ type: "LIVE_REPAIR", iter: e.data?.iter ?? 0, failed: e.data?.failed ?? [] });
          break;
        case "qa":
          dispatch({ type: "LIVE_QA", checked: e.data?.checked ?? 0, failed: e.data?.failed ?? [],
            unverifiable: e.data?.unverifiable ?? [] });
          break;
        case "retrospective":
          dispatch({ type: "LIVE_RETRO", added: e.data?.added ?? 0 });
          break;
        case "plan":
          dispatch({ type: "SET_GATE", plan: normalizePlan(e.data) });
          break;
        case "checkpoint":
          dispatch({ type: "SET_GATE", checkpoint: normalizeCheckpoint(e.data) });
          break;
        case "url": {
          const url = readUrl(e.data);
          if (url && onUrl) onUrl(url);
          break;
        }
        case "done":
          dispatch({ type: "CLOSE_LIVE" });
          dispatch({ type: "CLEAR_GATE" });
          if (onTurnDone) onTurnDone();
          getSession(sessionId)
            .then((s) => {
              setSession(s);
              dispatch({ type: "RESET", bubbles: messagesToBubbles(s.messages) });
              dispatch({ type: "SET_GATE", plan: normalizePlan(s.plan), checkpoint: normalizeCheckpoint(s.checkpoint) });
            })
            .catch(() => {});
          break;
        case "error": {
          const msg =
            typeof e.data === "string" ? e.data : e.data?.detail ?? e.data?.message ?? "Turn failed";
          dispatch({ type: "CLOSE_LIVE", final: { variant: "error", content: msg, role: "system" } });
          break;
        }
      }
    },
    [sessionId, onUrl, onTurnDone, onFile]
  );

  /* ── Submit a message turn ── */
  const handleSubmit = useCallback(
    async (retryContent?: string) => {
      const prompt = retryContent ?? input.trim();
      if (!prompt || state.streaming) return;

      // Sending to a sleeping session wakes it first, then continues the turn.
      // Guard with selfDrivingRef (as handleWake does) so the published wake
      // events we're already rendering here aren't re-applied off the feed as a
      // phantom "foreign" turn.
      if (session?.state === "asleep") {
        selfDrivingRef.current = true;
        try { await runWakeStream(); }
        finally { selfDrivingRef.current = false; }
      }

      // Retries deliberately don't re-send attachments — the original upload
      // already landed in the session's workspace on the first attempt.
      const files = retryContent ? [] : pendingFiles;
      const attachments: string[] = [];
      if (files.length) {
        // Sequential, not Promise.all: if file 2 of 3 fails to upload, file 1's
        // uploadedName is already recorded in state, so the next attempt skips
        // re-uploading it instead of creating an orphan copy on the server.
        for (const p of files) {
          if (p.uploadedName) { attachments.push(p.uploadedName); continue; }
          try {
            const name = await uploadAttachment(sessionId, p.file);
            attachments.push(name);
            setPendingFiles((cur) => cur.map((x) => (x.id === p.id ? { ...x, uploadedName: name } : x)));
          } catch (err) {
            // Text and files are left exactly as the user had them, ready for retry.
            dispatch({ type: "APPEND_USER", bubble: { id: uid(), role: "system", variant: "error",
              content: err instanceof Error ? err.message : "Attachment upload failed" } });
            return;
          }
        }
      }

      // Only clear the typed prompt once uploads have actually succeeded — an
      // upload failure above returns before this, leaving the input intact.
      if (!retryContent) setInput("");

      dispatch({ type: "APPEND_USER", bubble: { id: uid(), role: "user",
        content: files.length ? `${prompt}\n📎 ${files.map((f) => f.file.name).join(", ")}` : prompt } });
      const liveId = uid();
      dispatch({ type: "START_LIVE", id: liveId, prompt });

      const onEvent = makeOnEvent();
      selfDrivingRef.current = true;
      try {
        // Plan & build hands the task through the planner gate; Chat is a plain turn.
        const signal = abortRef.current?.signal;
        if (mode === "task") await startTask(sessionId, prompt, model, onEvent, attachments, signal);
        else await streamPost(`/api/sessions/${sessionId}/messages`,
          { prompt, model, ...(attachments.length ? { attachments } : {}) }, onEvent, signal);
        // The /task stream ends at a `checkpoint` (no `done`); close the live
        // bubble so the gate takes over. No-op if `done` already closed it.
        dispatch({ type: "CLOSE_LIVE" });
        // Only clear the queue once the message has actually been sent — a
        // failed send (catch below) keeps the uploaded files queued so the
        // user's next attempt neither loses nor re-uploads them.
        if (files.length) setPendingFiles([]);
      } catch (err) {
        dispatch({
          type: "CLOSE_LIVE",
          final: {
            variant: "error",
            content: err instanceof Error ? err.message : "Stream error",
            role: "system",
          },
        });
      } finally {
        selfDrivingRef.current = false;
        catchUpSeq();
      }
    },
    [input, model, mode, state.streaming, sessionId, session, pendingFiles, runWakeStream, makeOnEvent, catchUpSeq]
  );

  /* ── Respond to an open checkpoint (approve / edit / reject) ── opens a new
     SSE stream into the same live model; the result re-populates the gate
     (re-plan or escalation) or ends in done. */
  const handleRespond = useCallback(
    async (action: CheckpointAction, body: string) => {
      if (!state.checkpoint) return;
      const cid = state.checkpoint.id;
      dispatch({ type: "CLEAR_GATE" });
      dispatch({ type: "START_LIVE", id: uid(), role: "system" });
      const onEvent = makeOnEvent();
      selfDrivingRef.current = true;
      try {
        await respondCheckpoint(sessionId, cid, action, body, model, onEvent,
                                abortRef.current?.signal);
        // edit→re-plan and escalation paths end at a `checkpoint` (no `done`);
        // close the live bubble so the new gate renders. No-op after `done`.
        dispatch({ type: "CLOSE_LIVE" });
      } catch (err) {
        dispatch({
          type: "CLOSE_LIVE",
          final: {
            variant: "error",
            content: err instanceof Error ? err.message : "Stream error",
            role: "system",
          },
        });
      } finally {
        selfDrivingRef.current = false;
        catchUpSeq();
      }
    },
    [state.checkpoint, sessionId, model, makeOnEvent, catchUpSeq]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit]
  );

  /* ── Open PR ── */
  const handleOpenPr = useCallback(async () => {
    dispatch({ type: "SET_PR_LOADING", value: true });
    dispatch({ type: "SET_PR_ERROR", error: null });
    try {
      const result = await openPr(sessionId);
      if (result?.pr_url) dispatch({ type: "SET_PR_URL", url: result.pr_url });
      else if (result?.error || result?.reason)
        dispatch({ type: "SET_PR_ERROR", error: result.error ?? `Cannot open PR: ${result.reason}` });
    } catch (err) {
      dispatch({ type: "SET_PR_ERROR", error: err instanceof Error ? err.message : "Failed to open PR" });
    } finally {
      dispatch({ type: "SET_PR_LOADING", value: false });
    }
  }, [sessionId]);

  const handleStop = useCallback(async () => {
    try {
      await stopTurn(sessionId);
    } catch {}
  }, [sessionId]);

  const handleEnd = useCallback(async () => {
    try {
      await endSession(sessionId);
      dispatch({ type: "APPEND_SYSTEM", bubble: { id: uid(), role: "system", content: "Session ended." } });
      onTurnDone?.();
    } catch {}
  }, [sessionId, onTurnDone]);

  if (!session && state.bubbles.length === 0) {
    return (
      <div className="chat-empty">
        <div className="chat-empty-icon">◌</div>
        <div className="chat-empty-label">loading session…</div>
      </div>
    );
  }

  const isStreaming = state.streaming;
  const isAsleep = session?.state === "asleep";
  const isDeleted = session?.state === "deleted";
  const gateOpen = state.checkpoint !== null;
  const canSend = !isStreaming && !foreignLive && !isDeleted && !gateOpen
    && input.trim().length > 0;

  return (
    <div className="chat">
      {/* ── Header ── */}
      <div className="chat-header">
        <div className="chat-header-title">
          <span className="chat-header-repo">{session?.repo ?? sessionId}</span>
          {session?.state && (
            <span className={`badge badge-${session.state}`}>
              <span className="badge-dot" />
              {session.state}
            </span>
          )}
        </div>
        <div className="chat-header-actions">
          {state.prUrl ? (
            <a href={state.prUrl} target="_blank" rel="noreferrer" className="btn btn-accent">
              ↗ View PR
            </a>
          ) : (
            <button
              className="btn btn-accent"
              onClick={handleOpenPr}
              disabled={isStreaming || state.prLoading}
              title="Open pull request"
            >
              {state.prLoading ? "Opening…" : "Open PR"}
            </button>
          )}
          <button className="btn" onClick={handleStop} disabled={!isStreaming && !foreignLive} title="Stop current turn">
            Stop
          </button>
          {isAsleep && (
            <button className="btn" onClick={handleWake} disabled={isStreaming} title="Wake session">
              Wake
            </button>
          )}
          {!isAsleep && !isDeleted && (
            <button
              className="btn"
              onClick={handleSleep}
              disabled={isStreaming}
              title="Sleep session (free resources, resumable)"
            >
              Sleep
            </button>
          )}
          <button className="btn" onClick={handleEnd} title="End session" disabled={isDeleted}>
            End
          </button>
        </div>
      </div>

      {state.prError && <div className="chat-pr-error">{state.prError}</div>}

      {isAsleep && (
        <div className="chat-banner chat-banner--sleep">
          💤 This session is sleeping.{" "}
          <button className="btn btn-sm" onClick={handleWake} disabled={isStreaming}>
            Wake it
          </button>{" "}
          or just send a message.
        </div>
      )}
      {isDeleted && (
        <div className="chat-banner chat-banner--gone">
          🪦 This session was deleted. Its code is on branch{" "}
          <span className="mono">{session?.branch ?? "—"}</span>
          {session?.pr_url && (
            <>
              {" · "}
              <a href={session.pr_url} target="_blank" rel="noreferrer">
                view PR ↗
              </a>
            </>
          )}
          .
        </div>
      )}

      {/* ── Transcript ── */}
      <div className="chat-messages scrollable" ref={scrollRef}>
        {state.bubbles.length === 0 && (
          <div className="chat-no-messages">
            <span>send a message to start</span>
          </div>
        )}
        {state.bubbles.map((b) => (
          <BubbleView
            key={b.id}
            bubble={b}
            onRetry={b.variant === "error" && b.prompt ? () => handleSubmit(b.prompt) : undefined}
          />
        ))}
      </div>

      {/* ── Approval gate ── (live checkpoint or rebuilt from persisted state) */}
      {state.checkpoint && (
        <PlanCard
          plan={state.plan}
          checkpoint={state.checkpoint}
          disabled={isStreaming}
          onRespond={handleRespond}
        />
      )}

      {/* ── Input ── */}
      <div className="chat-input-area">
        <div
          className="chat-input-wrap"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); addFiles(e.dataTransfer.files); }}
        >
          {pendingFiles.length > 0 && (
            <div className="attach-row">
              {pendingFiles.map((p) => (
                <div className="attach-thumb" key={p.id}>
                  <img src={previewUrls.get(p.id)} alt={p.file.name} />
                  <button
                    aria-label={`remove ${p.file.name}`}
                    onClick={() => setPendingFiles((cur) => cur.filter((x) => x.id !== p.id))}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
          <textarea
            className="chat-input"
            placeholder={
              foreignLive
                ? "A turn is running from another surface (Slack/CLI) — following live…"
                : gateOpen
                ? "Respond to the plan above to continue…"
                : mode === "task"
                ? "Describe a task to plan & build…  (Enter to send · Shift+Enter for newline)"
                : "Send a message…  (Enter to send · Shift+Enter for newline)"
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={(e) => {
              if (e.clipboardData.files.length) { e.preventDefault(); addFiles(e.clipboardData.files); }
            }}
            disabled={isStreaming || isDeleted || gateOpen || foreignLive}
            rows={3}
          />
          <div className="chat-input-foot">
            <div className="composer-mode" role="tablist" aria-label="composer mode">
              <button
                className={`composer-mode-btn ${mode === "task" ? "is-active" : ""}`}
                onClick={() => setMode("task")}
                disabled={isStreaming || gateOpen}
                role="tab"
                aria-selected={mode === "task"}
              >
                Plan &amp; build
              </button>
              <button
                className={`composer-mode-btn ${mode === "chat" ? "is-active" : ""}`}
                onClick={() => setMode("chat")}
                disabled={isStreaming || gateOpen}
                role="tab"
                aria-selected={mode === "chat"}
              >
                Chat
              </button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.currentTarget.value = ""; }}
            />
            <button
              className="attach-btn"
              title="Attach images"
              aria-label="Attach images"
              onClick={() => fileInputRef.current?.click()}
              disabled={isStreaming || isDeleted || gateOpen || foreignLive}
            >
              📎
            </button>
            <ModelSelect value={model} onChange={setModel} disabled={isStreaming || gateOpen} />
            <button className="btn btn-accent chat-send-btn" onClick={() => handleSubmit()} disabled={!canSend}>
              {isStreaming ? "Working…" : mode === "task" ? "Plan & build ↵" : "Send ↵"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────────────
   Model selector
   ───────────────────────────────────────────────────────────────────────────── */

function ModelSelect({
  value,
  onChange,
  disabled,
}: {
  value: ModelChoice;
  onChange: (m: ModelChoice) => void;
  disabled?: boolean;
}) {
  const choices = useModelChoices();
  return (
    <label className="model-select" title="Model — auto picks based on the task">
      <span className="model-select-glyph">⚡</span>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as ModelChoice)}
      >
        {choices.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    </label>
  );
}

/* ─────────────────────────────────────────────────────────────────────────────
   BubbleView + segment rendering
   ───────────────────────────────────────────────────────────────────────────── */

const TOOL_META: Record<string, { verb: string; glyph: string }> = {
  Bash: { verb: "run", glyph: "›_" },
  Read: { verb: "read", glyph: "≡" },
  Edit: { verb: "edit", glyph: "✎" },
  MultiEdit: { verb: "edit", glyph: "✎" },
  Write: { verb: "write", glyph: "+" },
  Grep: { verb: "grep", glyph: "⌕" },
  Glob: { verb: "find", glyph: "⌕" },
  Task: { verb: "agent", glyph: "✦" },
  Agent: { verb: "agent", glyph: "✦" },
  WebFetch: { verb: "fetch", glyph: "↯" },
  WebSearch: { verb: "search", glyph: "⌕" },
  TodoWrite: { verb: "plan", glyph: "☰" },
  NotebookEdit: { verb: "edit", glyph: "✎" },
};

function ToolRow({ name, target }: { name: string; target?: string }) {
  const meta = TOOL_META[name] ?? { verb: name.toLowerCase(), glyph: "•" };
  return (
    <div className="seg-tool" title={`${name}${target ? `  ${target}` : ""}`}>
      <span className="seg-tool-glyph">{meta.glyph}</span>
      <span className="seg-tool-verb">{meta.verb}</span>
      {target && <span className="seg-tool-target">{target}</span>}
    </div>
  );
}

function StepRow({ label, status }: { label: string; status: "active" | "done" | "error" }) {
  const icon = status === "done" ? "✓" : status === "error" ? "✕" : "";
  return (
    <div className={`seg-step seg-step--${status}`}>
      <span className="seg-step-marker">
        {status === "active" ? <span className="seg-step-spinner" /> : icon}
      </span>
      <span className="seg-step-label">{label}</span>
    </div>
  );
}

interface BubbleViewProps {
  bubble: Bubble;
  onRetry?: () => void;
}

function BubbleView({ bubble, onRetry }: BubbleViewProps) {
  const { role, content, variant, live, segments, model } = bubble;

  const cls = ["bubble", `bubble-${role}`, variant ? `bubble-${variant}` : "", live ? "bubble-live" : ""]
    .filter(Boolean)
    .join(" ");

  const hasSegments = segments && segments.length > 0;

  return (
    <div className={cls}>
      <div className="bubble-role">
        <span>{role}</span>
        {model && <span className="bubble-model">⚡ {model}</span>}
      </div>
      <div className="bubble-content">
        {hasSegments ? (
          <div className="bubble-stream">
            {segments!.map((s, i) => {
              if (s.kind === "text")
                return s.text.trim() ? (
                  <div key={i} className="seg-text">
                    {s.text.trimEnd()}
                  </div>
                ) : null;
              if (s.kind === "tool") return <ToolRow key={i} name={s.name} target={s.target} />;
              if (s.kind === "step") return <StepRow key={i} label={s.label} status={s.status} />;
              if (s.kind === "verify")
                return (
                  <div key={i} className={`seg-verify seg-verify--${s.pass ? "pass" : "fail"}`}>
                    {s.pass ? "✓ verification passed" : "✕ verification failed"}
                  </div>
                );
              if (s.kind === "repair")
                return (
                  <div key={i} className="seg-repair">
                    ↻ Repair attempt {s.iter}
                    {s.failed.length ? ` — ${s.failed.join(", ")}` : ""}
                  </div>
                );
              if (s.kind === "qa") {
                const unv = s.unverifiable?.length
                  ? ` · ${s.unverifiable.length} not verifiable from the sandbox: ${s.unverifiable.join(", ")}`
                  : "";
                return (
                  <div key={i} className={`seg-qa seg-qa--${s.pass ? "pass" : "fail"}`}>
                    {s.pass
                      ? `✓ Browser QA — ${s.checked - (s.unverifiable?.length ?? 0)} checks passed${unv}`
                      : `✕ Browser QA — ${s.failed.join(", ")}${unv}`}
                  </div>
                );
              }
              if (s.kind === "retrospective")
                return s.added > 0 ? (
                  <div key={i} className="seg-retro">
                    ✦ Learned {s.added} lesson{s.added === 1 ? "" : "s"}
                  </div>
                ) : null;
              return null;
            })}
            {live && <span className="bubble-cursor" />}
          </div>
        ) : (
          content || (live ? <span className="bubble-cursor" /> : null)
        )}
      </div>
      {variant === "error" && onRetry && (
        <button className="bubble-retry btn" onClick={onRetry}>
          ↺ Retry
        </button>
      )}
    </div>
  );
}
