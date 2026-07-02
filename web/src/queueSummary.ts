import type { SessionSummary } from "./types";

/** Fleet counts for the queue header. `pr_open` is a done terminal. */
export function summarize(sessions: SessionSummary[]) {
  const out = { queued: 0, running: 0, done: 0 };
  for (const s of sessions) {
    if (s.state === "queued") out.queued++;
    else if (s.state === "running") out.running++;
    else if (s.state === "done" || s.state === "pr_open") out.done++;
  }
  return out;
}
