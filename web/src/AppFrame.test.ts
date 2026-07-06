import { describe, it, expect } from "vitest";
import { resolveEmbedSrc } from "./AppFrame";

const PUBLIC = "https://demo.trycloudflare.com";
const LOCAL = "http://run-1.forge.localhost:8088";

describe("resolveEmbedSrc", () => {
  it("prefers the local URL when the page is served locally", () => {
    for (const host of ["localhost", "127.0.0.1", "forge.localhost"]) {
      const { src, share } = resolveEmbedSrc({
        locationHostname: host, webUrl: PUBLIC, localUrl: LOCAL });
      expect(src).toBe(LOCAL);
      expect(share).toBe(PUBLIC); // public link still offered for opening out
    }
  });

  it("prefers the public URL when the page is served from a tunnel host", () => {
    const { src, share } = resolveEmbedSrc({
      locationHostname: "forge.example.com", webUrl: PUBLIC, localUrl: LOCAL });
    expect(src).toBe(PUBLIC);
    expect(share).toBeNull(); // src already is the public URL
  });

  it("falls back to whichever URL exists", () => {
    expect(resolveEmbedSrc({ locationHostname: "localhost", webUrl: PUBLIC, localUrl: null }).src).toBe(PUBLIC);
    expect(resolveEmbedSrc({ locationHostname: "forge.example.com", webUrl: null, localUrl: LOCAL }).src).toBe(LOCAL);
    expect(resolveEmbedSrc({ locationHostname: "localhost", webUrl: null, localUrl: null }).src).toBeNull();
  });
});
