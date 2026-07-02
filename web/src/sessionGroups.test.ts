import { expect, test } from "vitest";
import { groupSessionsByRepo, repoDisplayName } from "./sessionGroups";
import type { SessionSummary } from "./types";

/** Minimal SessionSummary factory — only the fields grouping cares about. */
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

test("groups sessions by repo, preserving input order within a folder", () => {
  const sessions = [
    s({ run_id: "a", repo: "dhis2/app" }),
    s({ run_id: "b", repo: "dhis2/app" }),
    s({ run_id: "c", repo: "dhis2/core" }),
  ];
  const groups = groupSessionsByRepo(sessions);
  expect(groups.map((g) => g.repo)).toEqual(["dhis2/app", "dhis2/core"]);
  expect(groups[0].sessions.map((x) => x.run_id)).toEqual(["a", "b"]);
  expect(groups[1].sessions.map((x) => x.run_id)).toEqual(["c"]);
});

test("folder order follows first appearance (most-recent-activity)", () => {
  // Input is newest-first; a folder's slot is its first/newest session.
  const sessions = [
    s({ run_id: "1", repo: "z/old-name" }),
    s({ run_id: "2", repo: "a/newer" }),
    s({ run_id: "3", repo: "z/old-name" }),
  ];
  const groups = groupSessionsByRepo(sessions);
  // z/old-name appears first in input, so it leads — not alphabetical.
  expect(groups.map((g) => g.repo)).toEqual(["z/old-name", "a/newer"]);
});

test("two repos sharing a basename stay in separate folders", () => {
  const sessions = [
    s({ run_id: "a", repo: "/Users/me/forge" }),
    s({ run_id: "b", repo: "dhis2/forge" }),
  ];
  const groups = groupSessionsByRepo(sessions);
  expect(groups.length).toBe(2);
  expect(groups.map((g) => g.repo)).toEqual(["/Users/me/forge", "dhis2/forge"]);
  // Display names differ per source: local path → basename, github → owner/repo.
  expect(groups.map((g) => g.displayName)).toEqual(["forge", "dhis2/forge"]);
});

test("missing repo falls into an (unknown) folder", () => {
  const groups = groupSessionsByRepo([s({ run_id: "a", repo: "" })]);
  expect(groups[0].repo).toBe("(unknown)");
  expect(groups[0].displayName).toBe("(unknown)");
});

test("repoDisplayName: github owner/repo shown as-is", () => {
  expect(repoDisplayName("dhis2/webapp")).toBe("dhis2/webapp");
});

test("repoDisplayName: absolute path shows the basename", () => {
  expect(repoDisplayName("/Users/me/dev/forge")).toBe("forge");
  expect(repoDisplayName("/Users/me/dev/forge/")).toBe("forge"); // trailing slash
});

test("repoDisplayName: empty / falsy input shows (unknown)", () => {
  expect(repoDisplayName("")).toBe("(unknown)");
});
