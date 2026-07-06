import { describe, it, expect } from "vitest";
import {
  clampSplit,
  parseSplit,
  splitFromPointer,
  DEFAULT_SPLIT,
  MIN_PANE_PX,
} from "./splitPane";

describe("clampSplit", () => {
  it("passes a mid-range percentage through unchanged", () => {
    expect(clampSplit(50)).toBe(50);
  });
  it("clamps below the floor and above the ceiling", () => {
    expect(clampSplit(2)).toBe(10);
    expect(clampSplit(99)).toBe(90);
  });
  it("falls back to the default for non-finite input", () => {
    expect(clampSplit(NaN)).toBe(DEFAULT_SPLIT);
    expect(clampSplit(Infinity)).toBe(DEFAULT_SPLIT);
  });
});

describe("parseSplit", () => {
  it("returns the default for null / empty", () => {
    expect(parseSplit(null)).toBe(DEFAULT_SPLIT);
    expect(parseSplit("")).toBe(DEFAULT_SPLIT);
  });
  it("parses and clamps a stored numeric string", () => {
    expect(parseSplit("60")).toBe(60);
    expect(parseSplit("3")).toBe(10);
  });
  it("returns the default for junk, never throws", () => {
    expect(parseSplit("not a number")).toBe(DEFAULT_SPLIT);
  });
});

describe("splitFromPointer", () => {
  it("maps a pointer at the container midpoint to ~50%", () => {
    expect(splitFromPointer(500, 0, 1000)).toBe(50);
  });
  it("accounts for the container's left offset", () => {
    expect(splitFromPointer(600, 100, 1000)).toBe(50);
  });
  it("keeps the left (app) pane at least MIN_PANE_PX wide", () => {
    // width 1000, min 280px → floor at 28%; a far-left drag clamps up to it
    expect(splitFromPointer(50, 0, 1000)).toBe((MIN_PANE_PX / 1000) * 100);
  });
  it("keeps the right (control) pane at least MIN_PANE_PX wide", () => {
    // far-right drag clamps down so the right pane stays >= 280px
    expect(splitFromPointer(990, 0, 1000)).toBe(100 - (MIN_PANE_PX / 1000) * 100);
  });
  it("returns the default when the container has no width", () => {
    expect(splitFromPointer(500, 0, 0)).toBe(DEFAULT_SPLIT);
  });
});
