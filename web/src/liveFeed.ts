import type { SseEvent } from "./types";

/* ─────────────────────────────────────────────────────────────────────────────
   Live-feed gating (pure, unit-tested): decides what to do with each event
   arriving on GET /api/sessions/{id}/events — the bus feed that lets this tab
   watch turns driven from Slack / CLI / another tab.

   Rules:
   - events are seq-stamped by the bus; anything ≤ lastSeq was already seen
     (replay overlap after a reconnect) and is dropped.
   - while THIS tab drives a turn (its own POST stream renders the events), feed
     copies only advance lastSeq — never render twice.
   - a foreign turn opens one live bubble on its first "turn-y" event; ambient
     kinds (url, checkpoint, done, heartbeats) never open one by themselves.
   ───────────────────────────────────────────────────────────────────────────── */

/** Kinds that indicate a turn is actively streaming — worth opening a live
    bubble for. Mid-turn kinds (verify/repair/qa/…) always follow one of these
    in every engine flow, so they only ever render into an open bubble. */
const OPENER_KINDS = new Set(["model", "phase", "narration", "tool"]);

/** Kinds that end a foreign turn's live bubble. `stream_end` is the bus-only
    end-of-flow signal (some flows end without a `done` — wake stops at `url`,
    plan_task at `checkpoint`), so a follower never stays locked on a turn
    that already finished. */
export const CLOSER_KINDS = new Set(["done", "error", "checkpoint", "slept", "stream_end"]);

export interface FeedDecision {
  /** render this event (via the shared turn-event switch) */
  apply: boolean;
  /** this event should open the foreign live bubble first */
  opens: boolean;
  /** updated high-water mark to remember */
  lastSeq: number;
}

export function gateFeedEvent(
  e: SseEvent,
  lastSeq: number,
  selfDriving: boolean,
  foreignOpen: boolean,
): FeedDecision {
  const seq = typeof e.data?.seq === "number" ? e.data.seq : null;
  if (seq !== null && seq <= lastSeq) return { apply: false, opens: false, lastSeq };
  const next = seq ?? lastSeq;
  // ": ping" heartbeats parse as kind "message" with empty data — never render.
  if (e.kind === "message") return { apply: false, opens: false, lastSeq: next };
  if (selfDriving) return { apply: false, opens: false, lastSeq: next };
  return { apply: true, opens: !foreignOpen && OPENER_KINDS.has(e.kind), lastSeq: next };
}

/** The slice of a bus backlog that belongs to a turn still in flight: every
    event after the last closer. Completed turns are covered by the persisted
    transcript; this tail is exactly what a page refresh mid-turn would
    otherwise lose. Empty when the backlog ends with a closer (nothing is
    running) or the buffer is empty. */
export function inflightTail(events: SseEvent[]): SseEvent[] {
  let cut = 0;
  for (let i = 0; i < events.length; i++) {
    if (CLOSER_KINDS.has(events[i].kind)) cut = i + 1;
  }
  return events.slice(cut);
}

/** Which surface drove the event ("slack" | "web" | "queue" | "api"). */
export function feedOrigin(e: SseEvent): string {
  return typeof e.data?.origin === "string" && e.data.origin ? e.data.origin : "elsewhere";
}

/** Short human label for the checkpoint_answered note. */
export function answeredNote(e: SseEvent): string {
  const action = e.data?.action ?? "answered";
  const body = e.data?.body ? `: ${e.data.body}` : "";
  return `✅ checkpoint answered from ${feedOrigin(e)} — ${action}${body}`;
}
