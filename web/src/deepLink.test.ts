import { describe, it, expect } from "vitest";
import { parseSessionHash, sessionHash, parseRoute, workspaceHash } from "./deepLink";

describe("deepLink", () => {
  it("parses #s=<run_id>", () => {
    expect(parseSessionHash("#s=abc123")).toBe("abc123");
    expect(parseSessionHash("#s=62e343a71be14f1d852edb05628e03b9"))
      .toBe("62e343a71be14f1d852edb05628e03b9");
  });

  it("rejects other hashes", () => {
    expect(parseSessionHash("")).toBeNull();
    expect(parseSessionHash("#")).toBeNull();
    expect(parseSessionHash("#settings")).toBeNull();
    expect(parseSessionHash("#s=")).toBeNull();
    expect(parseSessionHash('#s=<script>')).toBeNull();
  });

  it("round-trips", () => {
    expect(parseSessionHash(sessionHash("run-1"))).toBe("run-1");
  });
});

describe("parseRoute", () => {
  it("routes #live=<id> to the workspace", () => {
    expect(parseRoute("#live=abc123")).toEqual({ view: "workspace", runId: "abc123" });
  });
  it("routes #s=<id> to the dashboard with the id", () => {
    expect(parseRoute("#s=abc123")).toEqual({ view: "dashboard", runId: "abc123" });
  });
  it("routes anything else to the dashboard with no id", () => {
    expect(parseRoute("")).toEqual({ view: "dashboard", runId: null });
    expect(parseRoute("#live=")).toEqual({ view: "dashboard", runId: null });
    expect(parseRoute("#live=<script>")).toEqual({ view: "dashboard", runId: null });
  });
  it("round-trips workspaceHash", () => {
    expect(parseRoute(workspaceHash("run-1"))).toEqual({ view: "workspace", runId: "run-1" });
  });
});
