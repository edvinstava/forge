import { describe, it, expect } from "vitest";
import { parseSessionHash, sessionHash } from "./deepLink";

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
