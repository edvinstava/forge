import { describe, it, expect } from "vitest";
import { sessionStateMeta } from "./sessionState";

describe("sessionStateMeta", () => {
  it("maps asleep to a sleeping label", () => {
    const m = sessionStateMeta("asleep");
    expect(m.label).toBe("sleep");
    expect(m.dormant).toBe(true);
  });
  it("maps deleted to a tombstone", () => {
    expect(sessionStateMeta("deleted").label).toBe("gone");
    expect(sessionStateMeta("deleted").dormant).toBe(true);
  });
  it("passes through running", () => {
    expect(sessionStateMeta("running").dormant).toBe(false);
  });
  it("truncates unknown states", () => {
    expect(sessionStateMeta("verifying").label.length).toBeLessThanOrEqual(5);
  });
});
