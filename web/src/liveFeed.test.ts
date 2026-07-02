import { describe, it, expect } from "vitest";
import { gateFeedEvent, feedOrigin, answeredNote, inflightTail, CLOSER_KINDS } from "./liveFeed";

const ev = (kind: string, data: any = {}) => ({ kind, data });

describe("gateFeedEvent", () => {
  it("drops already-seen seqs (reconnect replay overlap)", () => {
    const d = gateFeedEvent(ev("narration", { seq: 3, text: "x" }), 5, false, false);
    expect(d).toEqual({ apply: false, opens: false, lastSeq: 5 });
  });

  it("applies fresh events and advances lastSeq", () => {
    const d = gateFeedEvent(ev("narration", { seq: 6, text: "x" }), 5, false, true);
    expect(d).toEqual({ apply: true, opens: false, lastSeq: 6 });
  });

  it("opens a foreign bubble on the first turn-y kind only", () => {
    expect(gateFeedEvent(ev("phase", { seq: 1 }), 0, false, false).opens).toBe(true);
    expect(gateFeedEvent(ev("tool", { seq: 1 }), 0, false, false).opens).toBe(true);
    expect(gateFeedEvent(ev("url", { seq: 1 }), 0, false, false).opens).toBe(false);
    expect(gateFeedEvent(ev("done", { seq: 1 }), 0, false, false).opens).toBe(false);
    expect(gateFeedEvent(ev("phase", { seq: 1 }), 0, false, true).opens).toBe(false);
  });

  it("only advances lastSeq while this tab drives its own turn", () => {
    const d = gateFeedEvent(ev("narration", { seq: 7, text: "x" }), 5, true, false);
    expect(d).toEqual({ apply: false, opens: false, lastSeq: 7 });
  });

  it("ignores heartbeat noise (kind message)", () => {
    const d = gateFeedEvent(ev("message"), 5, false, false);
    expect(d.apply).toBe(false);
  });

  it("tolerates events without seq", () => {
    const d = gateFeedEvent(ev("narration", { text: "x" }), 5, false, true);
    expect(d).toEqual({ apply: true, opens: false, lastSeq: 5 });
  });
});

describe("feedOrigin / answeredNote", () => {
  it("reads the origin stamp", () => {
    expect(feedOrigin(ev("phase", { origin: "slack" }))).toBe("slack");
    expect(feedOrigin(ev("phase", {}))).toBe("elsewhere");
  });

  it("summarizes a checkpoint answer", () => {
    expect(answeredNote(ev("checkpoint_answered",
      { origin: "slack", action: "approve" })))
      .toBe("✅ checkpoint answered from slack — approve");
    expect(answeredNote(ev("checkpoint_answered",
      { origin: "slack", action: "edit", body: "rename it" })))
      .toBe("✅ checkpoint answered from slack — edit: rename it");
  });

  it("closer kinds cover every generator-terminating event", () => {
    for (const k of ["done", "error", "checkpoint", "slept"]) {
      expect(CLOSER_KINDS.has(k)).toBe(true);
    }
  });
});

describe("stream_end", () => {
  it("is a closer and never an opener", () => {
    expect(CLOSER_KINDS.has("stream_end")).toBe(true);
    expect(gateFeedEvent(ev("stream_end", { seq: 1 }), 0, false, false).opens).toBe(false);
  });
});

describe("inflightTail", () => {
  it("returns the trailing events of a still-running turn", () => {
    const backlog = [
      ev("phase", { seq: 1 }), ev("done", { seq: 2 }), ev("stream_end", { seq: 3 }),
      ev("phase", { seq: 4 }), ev("narration", { seq: 5 }), ev("tool", { seq: 6 }),
    ];
    expect(inflightTail(backlog).map((e) => e.data.seq)).toEqual([4, 5, 6]);
  });

  it("returns [] when the backlog ends with a closer (nothing running)", () => {
    const backlog = [ev("phase", { seq: 1 }), ev("done", { seq: 2 }), ev("stream_end", { seq: 3 })];
    expect(inflightTail(backlog)).toEqual([]);
  });

  it("returns [] for an empty backlog", () => {
    expect(inflightTail([])).toEqual([]);
  });

  it("slices after the LAST closer when several turns are buffered", () => {
    const backlog = [
      ev("phase", { seq: 1 }), ev("stream_end", { seq: 2 }),
      ev("phase", { seq: 3 }), ev("checkpoint", { seq: 4 }), ev("stream_end", { seq: 5 }),
      ev("checkpoint_answered", { seq: 6 }), ev("phase", { seq: 7 }), ev("tool", { seq: 8 }),
    ];
    expect(inflightTail(backlog).map((e) => e.data.seq)).toEqual([6, 7, 8]);
  });

  it("cuts at a checkpoint too — the gate is rebuilt from persisted state", () => {
    const backlog = [ev("phase", { seq: 1 }), ev("plan", { seq: 2 }), ev("checkpoint", { seq: 3 })];
    expect(inflightTail(backlog)).toEqual([]);
  });
});
