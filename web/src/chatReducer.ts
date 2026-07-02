import type { PlanData, CheckpointData } from "./types";

/* ─────────────────────────────────────────────────────────────────────────────
   Live-turn segment model (extracted from Chat.tsx so the event→state mapping is
   unit-testable without a DOM). A streaming turn is an ordered list of segments;
   the gate (plan + checkpoint) is held as top-level state so live-stream and
   reconnect-from-SessionDetail both render through the same PlanCard.
   ───────────────────────────────────────────────────────────────────────────── */

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "tool"; name: string; target?: string }
  | { kind: "step"; label: string; status: "active" | "done" | "error" }
  | { kind: "verify"; pass: boolean }
  | { kind: "repair"; iter: number; failed: string[] }
  | { kind: "qa"; pass: boolean; checked: number; failed: string[] }
  | { kind: "retrospective"; added: number };

export interface Bubble {
  id: string;
  role: "user" | "assistant" | "system";
  /** Plain content for persisted / simple bubbles (and error text). */
  content: string;
  /** Ordered activity stream while live; undefined for persisted bubbles. */
  segments?: Segment[];
  variant?: "error" | null;
  live?: boolean;
  /** The user prompt that produced this bubble — enables Retry on errors. */
  prompt?: string;
  /** Resolved model for this turn (shown as a chip). */
  model?: string;
}

export interface ChatState {
  bubbles: Bubble[];
  liveId: string | null;
  streaming: boolean;
  prUrl: string | null;
  prError: string | null;
  prLoading: boolean;
  /** Gate: the proposed plan + open checkpoint (live event or persisted reconnect). */
  plan: PlanData | null;
  checkpoint: CheckpointData | null;
}

export type ChatAction =
  | { type: "RESET"; bubbles: Bubble[] }
  | { type: "APPEND_USER"; bubble: Bubble }
  | { type: "START_LIVE"; id: string; role?: "assistant" | "system"; prompt?: string }
  | { type: "LIVE_TEXT"; text: string }
  | { type: "LIVE_TOOL"; name: string; target?: string }
  | { type: "LIVE_STEP"; label: string }
  | { type: "LIVE_VERIFY"; pass: boolean }
  | { type: "LIVE_REPAIR"; iter: number; failed: string[] }
  | { type: "LIVE_QA"; checked: number; failed: string[] }
  | { type: "LIVE_RETRO"; added: number }
  | { type: "LIVE_MODEL"; model: string }
  | { type: "CLOSE_LIVE"; final?: Partial<Bubble>; stepStatus?: "done" | "error" }
  | { type: "APPEND_SYSTEM"; bubble: Bubble }
  | { type: "SET_STREAMING"; value: boolean }
  | { type: "SET_PR_URL"; url: string | null }
  | { type: "SET_PR_ERROR"; error: string | null }
  | { type: "SET_PR_LOADING"; value: boolean }
  | { type: "SET_GATE"; plan?: PlanData | null; checkpoint?: CheckpointData | null }
  | { type: "CLEAR_GATE" };

let _idCounter = 0;
export const uid = () => `b${++_idCounter}`;

export const initialChatState = (): ChatState => ({
  bubbles: [], liveId: null, streaming: false,
  prUrl: null, prError: null, prLoading: false,
  plan: null, checkpoint: null,
});

/** Apply a transform to the current live bubble (identified by liveId). */
function mapLive(state: ChatState, fn: (b: Bubble) => Bubble): ChatState {
  if (!state.liveId) return state;
  return { ...state, bubbles: state.bubbles.map((b) => (b.id === state.liveId ? fn(b) : b)) };
}

function pushSeg(state: ChatState, seg: Segment): ChatState {
  return mapLive(state, (b) => ({ ...b, segments: [...(b.segments ?? []), seg] }));
}

export function reducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "RESET":
      return { ...state, bubbles: action.bubbles, liveId: null, streaming: false };

    case "APPEND_USER":
      return { ...state, bubbles: [...state.bubbles, action.bubble] };

    case "START_LIVE": {
      const liveBubble: Bubble = {
        id: action.id,
        role: action.role ?? "assistant",
        content: "",
        segments: [],
        live: true,
        prompt: action.prompt,
      };
      return { ...state, bubbles: [...state.bubbles, liveBubble], liveId: action.id, streaming: true };
    }

    case "LIVE_TEXT":
      return mapLive(state, (b) => {
        const segs = [...(b.segments ?? [])];
        const last = segs[segs.length - 1];
        if (last && last.kind === "text") {
          segs[segs.length - 1] = { kind: "text", text: last.text + action.text };
        } else {
          segs.push({ kind: "text", text: action.text });
        }
        return { ...b, segments: segs };
      });

    case "LIVE_TOOL":
      return pushSeg(state, { kind: "tool", name: action.name, target: action.target });

    case "LIVE_STEP":
      // Provisioning phases: the new phase becomes active and any prior active
      // step is marked done, producing a self-completing checklist.
      return mapLive(state, (b) => {
        const segs = (b.segments ?? []).map((s) =>
          s.kind === "step" && s.status === "active" ? { ...s, status: "done" as const } : s
        );
        return { ...b, segments: [...segs, { kind: "step", label: action.label, status: "active" }] };
      });

    case "LIVE_VERIFY":
      return pushSeg(state, { kind: "verify", pass: action.pass });

    case "LIVE_REPAIR":
      return pushSeg(state, { kind: "repair", iter: action.iter, failed: action.failed });

    case "LIVE_QA":
      return pushSeg(state, { kind: "qa", pass: action.failed.length === 0, checked: action.checked, failed: action.failed });

    case "LIVE_RETRO":
      return pushSeg(state, { kind: "retrospective", added: action.added });

    case "LIVE_MODEL":
      return mapLive(state, (b) => ({ ...b, model: action.model }));

    case "CLOSE_LIVE": {
      if (!state.liveId) return state;
      const finalProps = action.final ?? {};
      return {
        ...state,
        bubbles: state.bubbles.map((b) => {
          if (b.id !== state.liveId) return b;
          // Resolve any still-active provisioning step to done/error.
          const segments = (b.segments ?? []).map((s) =>
            s.kind === "step" && s.status === "active"
              ? { ...s, status: action.stepStatus ?? "done" }
              : s
          );
          return { ...b, ...finalProps, segments, live: false };
        }),
        liveId: null,
        streaming: false,
      };
    }

    case "APPEND_SYSTEM":
      return { ...state, bubbles: [...state.bubbles, action.bubble] };

    case "SET_STREAMING":
      return { ...state, streaming: action.value };
    case "SET_PR_URL":
      return { ...state, prUrl: action.url };
    case "SET_PR_ERROR":
      return { ...state, prError: action.error };
    case "SET_PR_LOADING":
      return { ...state, prLoading: action.value };

    case "SET_GATE":
      return {
        ...state,
        plan: action.plan !== undefined ? action.plan : state.plan,
        checkpoint: action.checkpoint !== undefined ? action.checkpoint : state.checkpoint,
      };
    case "CLEAR_GATE":
      return { ...state, plan: null, checkpoint: null };

    default:
      return state;
  }
}
