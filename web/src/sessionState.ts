import type { SessionState } from "./types";

export interface StateMeta {
  label: string;
  cls: string;       // badge modifier class suffix (badge-<cls>)
  dormant: boolean;  // asleep|deleted → not a live env
}

// Covers every run state set by store.set_state plus the queue/lifecycle ones
// (queued/canceled/stopped/asleep/deleted). The `awaiting_*` states use the
// amber "attention" badge because they are the ones waiting on the user.
const META: Record<string, StateMeta> = {
  queued:            { label: "queued", cls: "idle",         dormant: false },
  provisioning:      { label: "prov",   cls: "provisioning", dormant: false },
  planning:          { label: "plan",   cls: "working",      dormant: false },
  running:           { label: "run",    cls: "running",      dormant: false },
  working:           { label: "work",   cls: "working",      dormant: false },
  verifying:         { label: "test",   cls: "working",      dormant: false },
  repairing:         { label: "repair", cls: "working",      dormant: false },
  qa:                { label: "QA",     cls: "working",      dormant: false },
  finalizing:        { label: "final",  cls: "working",      dormant: false },
  pushing:           { label: "push",   cls: "working",      dormant: false },
  awaiting_approval: { label: "review", cls: "attention",    dormant: false },
  awaiting_input:    { label: "input",  cls: "attention",    dormant: false },
  live:              { label: "live",   cls: "live",         dormant: false },
  pr_open:           { label: "PR",     cls: "live",         dormant: false },
  done:              { label: "done",   cls: "live",         dormant: false },
  idle:              { label: "idle",   cls: "idle",         dormant: false },
  canceled:          { label: "cancel", cls: "idle",         dormant: false },
  stopped:           { label: "stop",   cls: "idle",         dormant: false },
  stopped_budget:    { label: "budget", cls: "failed",       dormant: false },
  asleep:            { label: "sleep",  cls: "asleep",       dormant: true },
  deleted:           { label: "gone",   cls: "deleted",      dormant: true },
  failed:            { label: "fail",   cls: "failed",       dormant: false },
};

export function sessionStateMeta(state: SessionState): StateMeta {
  return META[state] ?? { label: state.slice(0, 5), cls: "idle", dormant: false };
}
