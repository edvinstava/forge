import { expect, test } from "vitest";
import {
  taskPath, checkpointPath, taskBody, checkpointBody, derivePrompt, normalizeCheckpoint,
  normalizePlan,
} from "./coworker";

test("path + body builders", () => {
  expect(taskPath("r1")).toBe("/api/sessions/r1/task");
  expect(checkpointPath("r1", 7)).toBe("/api/sessions/r1/checkpoints/7");
  expect(taskBody("do it", "auto")).toEqual({ task: "do it", model: "auto" });   // key is `task`
  expect(checkpointBody("approve", "", "opus")).toEqual({ action: "approve", body: undefined, model: "opus" });
  expect(checkpointBody("edit", "tweak", "auto")).toEqual({ action: "edit", body: "tweak", model: "auto" });
});

test("taskBody: omits attachments when absent or empty", () => {
  expect(taskBody("t", "auto")).toEqual({ task: "t", model: "auto" });
  expect(taskBody("t", "auto", [])).toEqual({ task: "t", model: "auto" });
});

test("taskBody: includes attachments when present", () => {
  expect(taskBody("t", "auto", ["1-a.png"]))
    .toEqual({ task: "t", model: "auto", attachments: ["1-a.png"] });
});

test("derivePrompt per checkpoint type", () => {
  expect(derivePrompt("plan_approval")).toMatch(/approve this plan/i);
  expect(derivePrompt("ambiguity")).toMatch(/open questions/i);
  expect(derivePrompt("repair_escalation", { failed: ["lint", "tsc"] })).toMatch(/lint, tsc/);
  expect(derivePrompt("repair_escalation", { failed: ["a11y"], kind: "acceptance" })).toMatch(/acceptance qa/i);
});

test("normalizeCheckpoint: live shape passes through", () => {
  expect(normalizeCheckpoint({ id: 3, type: "plan_approval", prompt: "Go?" }))
    .toEqual({ id: 3, type: "plan_approval", prompt: "Go?" });
});

test("normalizeCheckpoint: persisted shape maps ctype→type and derives prompt", () => {
  const got = normalizeCheckpoint({ id: 5, ctype: "ambiguity", payload: { plan: { goal: "x" } as any } });
  expect(got!.id).toBe(5);
  expect(got!.type).toBe("ambiguity");
  expect(got!.prompt).toMatch(/open questions/i);
});

test("normalizeCheckpoint: repair_escalation derives failed-check prompt", () => {
  const got = normalizeCheckpoint({ id: 9, ctype: "repair_escalation", payload: { failed: ["build"] } });
  expect(got!.prompt).toMatch(/build/);
});

test("normalizeCheckpoint: null → null", () => {
  expect(normalizeCheckpoint(null)).toBeNull();
  expect(normalizeCheckpoint(undefined)).toBeNull();
});

test("normalizePlan: null/undefined → null", () => {
  expect(normalizePlan(null)).toBeNull();
  expect(normalizePlan(undefined)).toBeNull();
});

test("normalizePlan: coerces object steps to their intent string", () => {
  // Backend planner emits steps as objects: {id, intent, files} (prompts.py).
  // Rendering an object as a React child throws and blanks the page — coerce here.
  const got = normalizePlan({
    goal: "ship it",
    steps: [
      { id: 1, intent: "wire up the route", files: ["a.ts"] },
      { id: 2, intent: "add a test" },
    ],
    risk: "low",
  } as any);
  expect(got!.steps).toEqual(["wire up the route", "add a test"]);
});

test("normalizePlan: passes through string steps unchanged", () => {
  const got = normalizePlan({ goal: "g", steps: ["one", "two"], risk: "low" } as any);
  expect(got!.steps).toEqual(["one", "two"]);
});

test("normalizePlan: missing arrays default to empty (no undefined.length crash)", () => {
  const got = normalizePlan({ goal: "g" } as any);
  expect(got).toEqual({
    goal: "g",
    steps: [],
    acceptance: [],
    assumptions: [],
    open_questions: [],
    risk: "unknown",
  });
});

test("normalizePlan: coerces non-string acceptance/open_questions items too", () => {
  const got = normalizePlan({
    goal: "g",
    acceptance: [{ text: "builds" }],
    open_questions: [{ intent: "which db?" }],
  } as any);
  expect(got!.acceptance).toEqual(["builds"]);
  expect(got!.open_questions).toEqual(["which db?"]);
});
