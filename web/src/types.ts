export interface Repo {
  name: string;
  full_name: string;
  description: string | null;
  html_url: string;
  default_branch: string;
  path?: string;        // local repos have a path field
  remote?: string;      // local repos surface the remote URL
}

// Run states the backend sets via store.set_state (plus queue/lifecycle ones).
// Keep in sync with sessionState.ts META and the `set_state(...)` call sites.
export type SessionState =
  | "queued"
  | "provisioning"
  | "planning"
  | "running"
  | "verifying"
  | "awaiting_approval"
  | "awaiting_input"
  | "repairing"
  | "qa"
  | "finalizing"
  | "pushing"
  | "pr_open"
  | "done"
  | "failed"
  | "idle"
  | "canceled"
  | "stopped_budget"
  | "stopped"
  | "asleep"
  | "deleted"
  | string;           // allow unknown states from server

export interface SessionSummary {
  run_id: string;
  repo: string;
  title: string | null;
  state: SessionState;
  repo_source: string | null;
  pr_url: string | null;
  web_url: string | null;
  web_service: string | null;
  env_state: string | null;
  branch?: string | null;
  batch_id?: string | null;
  model?: string | null;
  last_active: string;
}

/** Response from POST /api/batch. */
export interface BatchResult {
  batch_id: string;
  run_ids: string[];
}

/** Daemon settings from /api/config — proxy fields derive each run's local
 * preview URL; provider/model_choices drive the model picker. */
export interface ProxyConfig {
  proxy_domain: string;
  proxy_port: number;
  provider?: string;
  model_choices?: string[];
}

export interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: string;
  meta?: Record<string, any> | null;
}

export interface SessionDetail extends SessionSummary {
  messages: Message[];
  checkpoint?: RawCheckpoint | null;   // from store.open_checkpoint
  plan?: PlanData | null;              // from run.plan_json
}

/** Plan proposed by the planner — mirrors Plan.to_dict() server-side. */
export interface PlanData {
  goal: string;
  steps: string[];
  acceptance: string[];
  assumptions: string[];
  open_questions: string[];
  risk: string;
}

export type CheckpointType =
  "plan_approval" | "ambiguity" | "repair_escalation" | "needs_input" | string;

/** Canonical, normalized checkpoint the UI renders. */
export interface CheckpointData {
  id: number;
  type: CheckpointType;
  prompt: string;
}

/** Raw checkpoint as received — live ({type,prompt}) or persisted ({ctype,payload}). */
export interface RawCheckpoint {
  id: number;
  type?: string;
  ctype?: string;
  prompt?: string;
  payload?: { plan?: PlanData; failed?: string[]; kind?: string;
              blocked?: { question?: string } } | null;
  [k: string]: any;
}

export type CheckpointAction = "approve" | "edit" | "reject";

export interface SseEvent {
  kind: string;
  data: any;
}

/** Response from GET /api/sessions/:id/browser — the live agent-browser
 * stream's heartbeat. `active` = a frame fresher than a few seconds exists;
 * `ts` is the frame's mtime (ms) and doubles as the <img> cache-buster. */
export interface BrowserStatus {
  active: boolean;
  ts: number;
  url: string;
  title: string;
}

/** Model choices offered in the UI; `auto` picks based on the task. The real
 * list is provider-specific and comes from GET /api/config (model_choices) —
 * this constant is only the pre-fetch fallback. */
export type ModelChoice = string;
export const MODEL_CHOICES: ModelChoice[] = ["auto", "opus", "sonnet", "haiku"];

/** Response from GET /api/sessions/:id/verify. verify_ok is tri-state:
 *  true = passed, false = failed, null = no checks configured / not run. */
export interface VerifyResult {
  verify_ok: boolean | null;
  diff_files: number | null;
  verify_failed: string[];
  verify_output: string;
  model: string | null;
}

/** One workspace file from GET /api/sessions/:id/files. `status` is the git
 * working-tree state; "clean" = tracked and unchanged. */
export type FileStatus =
  | "clean" | "modified" | "added" | "deleted" | "renamed" | "untracked";

export interface WorkspaceFile {
  path: string;
  status: FileStatus;
}

export interface FilesListing {
  files: WorkspaceFile[];
  truncated: boolean;
}

/** GET /api/sessions/:id/file?path= — content is capped server-side
 * (truncated flag) and empty for binary files; diff is the file's uncommitted
 * change (untracked files get an all-additions pseudo-diff). */
export interface FileDetail {
  path: string;
  status: FileStatus;
  size: number;
  truncated: boolean;
  binary: boolean;
  missing: boolean;
  content: string;
  diff: string;
}

/** Response from POST /api/sessions/:id/pr (open_pr). */
export interface PrResult {
  ok?: boolean;
  pr_url?: string;
  draft?: boolean;
  reason?: string;
  error?: string;
}
