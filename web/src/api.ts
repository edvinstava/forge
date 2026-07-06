import type { Repo, SessionSummary, SessionDetail, SseEvent, PrResult, VerifyResult, ProxyConfig, CheckpointAction, BatchResult, BrowserStatus } from "./types";
import { taskPath, checkpointPath, taskBody, checkpointBody } from "./coworker";

export function parseSseChunk(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const block of parts) {
    if (!block.trim()) continue;  // skip blank separators — no phantom events
    let kind = "message"; let data: any = {};
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) kind = line.slice(6).trim();
      else if (line.startsWith("data:")) { try { data = JSON.parse(line.slice(5).trim()); } catch {} }
    }
    events.push({ kind, data });
  }
  return { events, rest };
}

async function j<T>(p: string): Promise<T> { return (await fetch(p)).json(); }
export const getConfig = () => j<ProxyConfig>("/api/config");

// The config is static per daemon run; fetch it once and share the promise so
// every model picker doesn't refetch. A failed fetch clears the cache so the
// next consumer retries instead of caching the failure forever.
let configPromise: Promise<ProxyConfig> | null = null;
export function getConfigCached(): Promise<ProxyConfig> {
  configPromise ??= getConfig().catch((e) => { configPromise = null; throw e; });
  return configPromise;
}
export const listRepos = (q = "") => j<Repo[]>(`/api/repos?q=${encodeURIComponent(q)}`);
export const listSessions = () => j<SessionSummary[]>("/api/sessions");
export const getSession = (id: string) => j<SessionDetail>(`/api/sessions/${id}`);
export const getDiff = async (id: string) => (await j<{diff: string}>(`/api/sessions/${id}/diff`)).diff;
export const getBrowserStatus = (id: string) => j<BrowserStatus>(`/api/sessions/${id}/browser`);
export const getVerify = (id: string) => j<VerifyResult>(`/api/sessions/${id}/verify`);
export const openPr = (id: string): Promise<PrResult> =>
  fetch(`/api/sessions/${id}/pr`, {method: "POST"}).then(r => r.json());
export const stopTurn = (id: string) => fetch(`/api/sessions/${id}/stop`, {method: "POST"});
export const endSession = (id: string) => fetch(`/api/sessions/${id}`, {method: "DELETE"});
export const submitBatch = (items: { repo: string; task: string; model?: string }[]) =>
  fetch("/api/batch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  }).then(r => r.json() as Promise<BatchResult>);
export const cancelBatch = (batchId: string) =>
  fetch(`/api/batch/${batchId}`, { method: "DELETE" }).then(r => r.json());
export const sleepSession = (id: string) =>
  fetch(`/api/sessions/${id}/sleep`, {method: "POST"}).then(r => r.json());
export const wakeSession = (id: string, onEvent: (e: SseEvent) => void,
    signal?: AbortSignal) =>
  streamPost(`/api/sessions/${id}/wake`, {}, onEvent, signal);

export const uploadAttachment = async (id: string, file: File): Promise<string> => {
  const res = await fetch(
    `/api/sessions/${id}/attachments?name=${encodeURIComponent(file.name || "image.png")}`,
    { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => null);
    throw new Error(err?.detail ?? `upload failed (${res.status})`);
  }
  return (await res.json()).name as string;
};

export const startTask = (id: string, task: string, model: string,
    onEvent: (e: SseEvent) => void, attachments?: string[], signal?: AbortSignal) =>
  streamPost(taskPath(id), taskBody(task, model, attachments), onEvent, signal);

export const respondCheckpoint = (
  id: string, cid: number, action: CheckpointAction, body: string,
  model: string, onEvent: (e: SseEvent) => void, signal?: AbortSignal,
) => streamPost(checkpointPath(id, cid), checkpointBody(action, body, model), onEvent, signal);

/** Attach to a session's live event feed (GET SSE): every TurnEvent the engine
 * emits for this run, whichever surface drives it (web, Slack, CLI, queue).
 * `since=-1` tails from now; a reconnect passes the last seen seq so the gap
 * replays. `tail=0` fetches the buffered backlog and returns (catch-up).
 * Resolves when the stream ends; rejects on network/abort. */
export async function attachEvents(runId: string, since: number,
    onEvent: (e: SseEvent) => void, signal?: AbortSignal, tail = 1): Promise<void> {
  const res = await fetch(`/api/sessions/${runId}/events?since=${since}&tail=${tail}`,
                          { signal });
  if (!res.ok || !res.body) throw new Error(`events feed: ${res.status}`);
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    const { events, rest } = parseSseChunk(buf); buf = rest;
    events.forEach(onEvent);
  }
  buf += dec.decode();
  parseSseChunk(buf).events.forEach(onEvent);
}

export async function streamPost(path: string, body: unknown,
    onEvent: (e: SseEvent) => void, signal?: AbortSignal): Promise<void> {
  const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify(body), signal});
  if (!res.ok || !res.body) {
    // An error response (e.g. 409 at session cap) is JSON, not SSE — its body
    // has no "\n\n" so parseSseChunk yields nothing and the caller would
    // complete silently. Surface the server's message instead.
    const err = await res.json().catch(() => null);
    throw new Error(err?.error ?? err?.detail ?? `request failed (${res.status})`);
  }
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    const { events, rest } = parseSseChunk(buf); buf = rest;
    events.forEach(onEvent);
  }
  // Flush any bytes the decoder held back at a multi-byte boundary, then emit
  // any final complete frame still sitting in the buffer.
  buf += dec.decode();
  parseSseChunk(buf).events.forEach(onEvent);
}
