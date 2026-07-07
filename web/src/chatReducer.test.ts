import { expect, test } from "vitest";
import { reducer, initialChatState, type ChatState } from "./chatReducer";

const s0: ChatState = initialChatState();

test("SET_GATE stores plan and normalized checkpoint", () => {
  const s = reducer(s0, { type: "SET_GATE",
    plan: { goal: "g", steps: [], acceptance: [], assumptions: [], open_questions: [], risk: "low" },
    checkpoint: { id: 1, type: "plan_approval", prompt: "ok?" } });
  expect(s.plan?.goal).toBe("g");
  expect(s.checkpoint).toEqual({ id: 1, type: "plan_approval", prompt: "ok?" });
});

test("CLEAR_GATE nulls plan + checkpoint", () => {
  const s1 = reducer(s0, { type: "SET_GATE", checkpoint: { id: 1, type: "plan_approval", prompt: "x" } });
  const s2 = reducer(s1, { type: "CLEAR_GATE" });
  expect(s2.plan).toBeNull();
  expect(s2.checkpoint).toBeNull();
});

test("repair/qa/retrospective push activity segments onto the live bubble", () => {
  let s = reducer(s0, { type: "START_LIVE", id: "b1" });
  s = reducer(s, { type: "LIVE_REPAIR", iter: 2, failed: ["lint"] });
  s = reducer(s, { type: "LIVE_QA", checked: 3, failed: [], unverifiable: [] });
  s = reducer(s, { type: "LIVE_RETRO", added: 1 });
  const segs = s.bubbles.find((b) => b.id === "b1")!.segments!;
  expect(segs.map((x) => x.kind)).toEqual(["repair", "qa", "retrospective"]);
  expect(segs[1]).toMatchObject({ kind: "qa", pass: true });
});

test("LIVE_QA carries unverifiable criteria; they don't fail the segment", () => {
  let s = reducer(s0, { type: "START_LIVE", id: "b1" });
  s = reducer(s, { type: "LIVE_QA", checked: 2, failed: [],
    unverifiable: ["dashboard shows pr-42"] });
  const segs = s.bubbles.find((b) => b.id === "b1")!.segments!;
  expect(segs[0]).toMatchObject({
    kind: "qa", pass: true, checked: 2, unverifiable: ["dashboard shows pr-42"] });
});
