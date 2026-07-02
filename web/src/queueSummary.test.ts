import { describe, it, expect } from "vitest";
import { summarize } from "./queueSummary";
import type { SessionSummary } from "./types";

const S = (state: string): SessionSummary => ({
  run_id: Math.random().toString(36).slice(2), repo: "o/r", title: null,
  state, repo_source: null, pr_url: null, web_url: null, web_service: null,
  env_state: null, last_active: "",
});

describe("summarize", () => {
  it("counts queued/running/done buckets", () => {
    const out = summarize([S("queued"), S("queued"), S("running"),
                           S("done"), S("pr_open"), S("failed")]);
    expect(out.queued).toBe(2);
    expect(out.running).toBe(1);
    expect(out.done).toBe(2);          // done + pr_open both count as done
  });
  it("handles empty", () => {
    expect(summarize([])).toEqual({ queued: 0, running: 0, done: 0 });
  });
});
