import type { RawCheckpoint, CheckpointData, CheckpointType, CheckpointAction, PlanData } from "./types";

export const taskPath = (id: string) => `/api/sessions/${id}/task`;
export const checkpointPath = (id: string, cid: number) =>
  `/api/sessions/${id}/checkpoints/${cid}`;

export const taskBody = (task: string, model: string, attachments?: string[]) =>
  ({ task, model, ...(attachments?.length ? { attachments } : {}) });
export const checkpointBody = (action: CheckpointAction, body: string, model: string) =>
  ({ action, body: body || undefined, model });

export function derivePrompt(type: CheckpointType, payload?: RawCheckpoint["payload"]): string {
  if (type === "plan_approval") return "Approve this plan to proceed, or describe changes.";
  if (type === "ambiguity") return "Please answer the open questions, then I'll proceed.";
  if (type === "repair_escalation") {
    const failed = (payload?.failed ?? []).join(", ");
    const what = payload?.kind === "acceptance" ? "acceptance QA" : "these checks";
    return `Couldn't get ${what} green${failed ? `: ${failed}` : ""}. ` +
           "Reply with guidance to retry, or reject to stop.";
  }
  if (type === "needs_input") {
    // On reload the persisted row has no `prompt` column; the agent's actual
    // question lives in payload.blocked.question (see session.py NEEDS_INPUT).
    const q = payload?.blocked?.question;
    return (typeof q === "string" && q.trim())
      ? q
      : "I need more information to continue — please reply.";
  }
  return "Respond to continue.";
}

/** Accepts the live event payload OR the persisted record; returns canonical form. */
export function normalizeCheckpoint(raw: RawCheckpoint | null | undefined): CheckpointData | null {
  if (!raw) return null;
  const type = (raw.type ?? raw.ctype ?? "") as CheckpointType;
  const prompt = raw.prompt ?? derivePrompt(type, raw.payload);
  return { id: raw.id, type, prompt };
}

/** Coerce a plan list item to a display string. The backend planner emits steps
 *  as objects ({id, intent, files}, see prompts.py), and acceptance/questions can
 *  arrive as objects too. Rendering an object as a React child throws and blanks
 *  the whole page (no error boundary), so we flatten to text at ingestion. */
function itemText(item: unknown): string {
  if (typeof item === "string") return item;
  if (item && typeof item === "object") {
    const o = item as Record<string, unknown>;
    const cand = o.intent ?? o.text ?? o.description ?? o.title;
    if (typeof cand === "string") return cand;
    return JSON.stringify(item);
  }
  return String(item ?? "");
}

function toStringList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(itemText) : [];
}

/** Normalize a plan (live `plan` event payload OR persisted run.plan_json) into a
 *  render-safe shape: every list is a string[] and never undefined. Without this,
 *  PlanCard crashes on object steps or missing arrays. */
export function normalizePlan(raw: any): PlanData | null {
  if (!raw) return null;
  return {
    goal: typeof raw.goal === "string" ? raw.goal : String(raw.goal ?? ""),
    steps: toStringList(raw.steps),
    acceptance: toStringList(raw.acceptance),
    assumptions: toStringList(raw.assumptions),
    open_questions: toStringList(raw.open_questions),
    risk: typeof raw.risk === "string" ? raw.risk : "unknown",
  };
}
