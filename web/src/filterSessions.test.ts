import { expect, test } from "vitest";
import { filterSessions } from "./filterSessions";
import type { SessionSummary } from "./types";

function s(partial: Partial<SessionSummary>): SessionSummary {
  return {
    run_id: partial.run_id ?? "id",
    repo: partial.repo ?? "owner/repo",
    title: partial.title ?? null,
    state: partial.state ?? "idle",
    repo_source: partial.repo_source ?? null,
    pr_url: partial.pr_url ?? null,
    web_url: partial.web_url ?? null,
    web_service: partial.web_service ?? null,
    env_state: partial.env_state ?? null,
    last_active: partial.last_active ?? "2026-06-24T00:00:00Z",
  };
}

const SESSIONS = [
  s({ run_id: "a", repo: "dhis2/webapp", title: "Implement error boundary" }),
  s({ run_id: "b", repo: "dhis2/dhis2-core", title: "Refactor analytics" }),
  s({ run_id: "c", repo: "/Users/me/forge", title: null }),
];

test("empty query passes everything through", () => {
  expect(filterSessions(SESSIONS, "")).toEqual(SESSIONS);
  expect(filterSessions(SESSIONS, "   ")).toEqual(SESSIONS);
});

test("matches on chat title, case-insensitively", () => {
  const out = filterSessions(SESSIONS, "ERROR");
  expect(out.map((x) => x.run_id)).toEqual(["a"]);
});

test("matches on repo display name", () => {
  const out = filterSessions(SESSIONS, "analytics".slice(0, 0) + "core");
  expect(out.map((x) => x.run_id)).toEqual(["b"]);
});

test("matches local repo by its basename", () => {
  // The raw repo is an absolute path; the user types the basename they see.
  const out = filterSessions(SESSIONS, "forge");
  expect(out.map((x) => x.run_id)).toEqual(["c"]);
});

test("no match returns an empty list", () => {
  expect(filterSessions(SESSIONS, "zzz-nope")).toEqual([]);
});
