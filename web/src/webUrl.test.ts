import { describe, it, expect } from "vitest";
import { pickWebUrl, localPreviewUrl } from "./webUrl";
import type { SessionSummary } from "./types";

const S = (run_id: string, web_url: string | null) =>
  ({ run_id, web_url } as unknown as SessionSummary);

describe("localPreviewUrl", () => {
  const cfg = { proxy_domain: "forge.localhost", proxy_port: 8088 };

  it("builds the DNS-free *.forge.localhost url for a tunnelled session", () => {
    expect(localPreviewUrl("abc", "https://x.trycloudflare.com", cfg)).toBe(
      "http://run-abc.forge.localhost:8088",
    );
  });

  it("returns null for a non-tunnelled (localhost) session — no proxy routes it", () => {
    expect(localPreviewUrl("abc", "http://localhost:3001", cfg)).toBe(null);
    expect(localPreviewUrl("abc", "http://127.0.0.1:3001", cfg)).toBe(null);
  });

  it("returns null when config, webUrl, or activeId is missing", () => {
    expect(localPreviewUrl("abc", "https://x.trycloudflare.com", null)).toBe(null);
    expect(localPreviewUrl("abc", null, cfg)).toBe(null);
    expect(localPreviewUrl(null, "https://x.trycloudflare.com", cfg)).toBe(null);
  });
});

describe("pickWebUrl", () => {
  it("fills the url from the polled active session", () => {
    // Regression: a session that goes live in the background (e.g. started from
    // Slack) must surface its live URL in the Inspector via the poll, not stay null.
    expect(pickWebUrl(null, [S("a", "http://x")], "a")).toBe("http://x");
  });

  it("does not clobber a known url when the poll momentarily shows null", () => {
    expect(pickWebUrl("http://x", [S("a", null)], "a")).toBe("http://x");
  });

  it("keeps prev when the active session is not in the polled list yet", () => {
    expect(pickWebUrl("http://x", [], "a")).toBe("http://x");
  });

  it("keeps prev when nothing matches the active id", () => {
    expect(pickWebUrl(null, [S("b", "http://y")], "a")).toBe(null);
  });
});
