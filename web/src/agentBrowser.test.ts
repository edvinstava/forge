import { describe, it, expect } from "vitest";
import {
  resolvePane,
  nextPin,
  browserFrameUrl,
  browserStreamUrl,
  nextEpoch,
  cookieSafeWorkspaceUrl,
} from "./agentBrowser";

describe("resolvePane", () => {
  it("follows the stream in auto mode", () => {
    expect(resolvePane("auto", true)).toBe("agent");
    expect(resolvePane("auto", false)).toBe("app");
  });

  it("respects an app pin even while streaming", () => {
    expect(resolvePane("app", true)).toBe("app");
  });

  it("never shows a dead stream as live, even pinned to agent", () => {
    expect(resolvePane("agent", false)).toBe("app");
    expect(resolvePane("agent", true)).toBe("agent");
  });

  it("auto shows the files pane while a turn edits with no browser live", () => {
    expect(resolvePane("auto", false, true)).toBe("files");
    // A live browser is the closer view of the agent — it still wins.
    expect(resolvePane("auto", true, true)).toBe("agent");
    // Turn over (or no edits yet): back to the app.
    expect(resolvePane("auto", false, false)).toBe("app");
  });

  it("a files pin sticks regardless of stream or edit activity", () => {
    expect(resolvePane("files", true, false)).toBe("files");
    expect(resolvePane("files", false, true)).toBe("files");
    expect(resolvePane("files", false, false)).toBe("files");
  });
});

describe("nextPin", () => {
  it("clicking what auto already shows stays in follow mode", () => {
    expect(nextPin("auto", true, "agent")).toBe("auto");
    expect(nextPin("auto", false, "app")).toBe("auto");
    expect(nextPin("auto", false, "files", true)).toBe("auto");
  });

  it("clicking the other pane pins it", () => {
    expect(nextPin("auto", true, "app")).toBe("app");
    expect(nextPin("app", true, "agent")).toBe("agent");
    expect(nextPin("agent", true, "app")).toBe("app");
    expect(nextPin("auto", false, "files", false)).toBe("files");
    expect(nextPin("files", false, "app", true)).toBe("app");
  });
});

describe("browserFrameUrl", () => {
  it("busts the cache with the frame ts", () => {
    expect(browserFrameUrl("r1", 1234)).toBe("/api/sessions/r1/browser/frame?t=1234");
  });
});

describe("browserStreamUrl", () => {
  it("keys the MJPEG connection on the screencast epoch", () => {
    expect(browserStreamUrl("r1", 2)).toBe("/api/sessions/r1/browser/stream?e=2");
  });
});

describe("nextEpoch", () => {
  it("bumps only when a screencast starts (inactive→active edge)", () => {
    expect(nextEpoch(false, true, 0)).toBe(1);
  });

  it("holds the connection while the stream stays active", () => {
    expect(nextEpoch(true, true, 1)).toBe(1);
  });

  it("does not bump on end or while idle — no reconnect to a dead stream", () => {
    expect(nextEpoch(true, false, 1)).toBe(1);
    expect(nextEpoch(false, false, 1)).toBe(1);
  });
});

describe("cookieSafeWorkspaceUrl", () => {
  const at = (hostname: string, port = "8099") => ({
    hostname, port, pathname: "/", hash: "#live=r1",
  });

  it("hops from loopback literals to the proxy domain (same-site iframe cookies)", () => {
    expect(cookieSafeWorkspaceUrl(at("127.0.0.1"), "forge.localhost"))
      .toBe("http://forge.localhost:8099/#live=r1");
    expect(cookieSafeWorkspaceUrl(at("localhost"), "forge.localhost"))
      .toBe("http://forge.localhost:8099/#live=r1");
  });

  it("stays put when already on the proxy domain", () => {
    expect(cookieSafeWorkspaceUrl(at("forge.localhost"), "forge.localhost")).toBeNull();
  });

  it("leaves remote/tunnel viewers alone — the hop only resolves locally", () => {
    expect(cookieSafeWorkspaceUrl(at("tun.trycloudflare.com", ""), "forge.localhost"))
      .toBeNull();
  });

  it("skips custom (non-.localhost) proxy domains it can't assume resolve", () => {
    expect(cookieSafeWorkspaceUrl(at("127.0.0.1"), "forge.example.com")).toBeNull();
    expect(cookieSafeWorkspaceUrl(at("127.0.0.1"), undefined)).toBeNull();
  });

  it("preserves path and hash, drops nothing", () => {
    expect(
      cookieSafeWorkspaceUrl(
        { hostname: "127.0.0.1", port: "", pathname: "/", hash: "#live=abc" },
        "forge.localhost",
      ),
    ).toBe("http://forge.localhost/#live=abc");
  });
});
