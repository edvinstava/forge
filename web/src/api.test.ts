import { expect, test, vi } from "vitest";
import { parseSseChunk, startTask, respondCheckpoint } from "./api";

function mockFetchOnce() {
  const calls: { url: string; init: any }[] = [];
  const body = { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) };
  vi.stubGlobal("fetch", (url: string, init: any) => { calls.push({ url, init }); return Promise.resolve({ ok: true, body }); });
  return calls;
}

test("startTask posts {task,model} to the task endpoint", async () => {
  const calls = mockFetchOnce();
  await startTask("r1", "build X", "auto", () => {});
  expect(calls[0].url).toBe("/api/sessions/r1/task");
  expect(calls[0].init.method).toBe("POST");
  expect(JSON.parse(calls[0].init.body)).toEqual({ task: "build X", model: "auto" });
  vi.unstubAllGlobals();
});

test("startTask includes attachments when provided", async () => {
  const calls = mockFetchOnce();
  await startTask("r1", "build X", "auto", () => {}, ["1-a.png"]);
  expect(JSON.parse(calls[0].init.body)).toEqual({ task: "build X", model: "auto", attachments: ["1-a.png"] });
  vi.unstubAllGlobals();
});

test("respondCheckpoint posts {action,body,model} to the checkpoint endpoint", async () => {
  const calls = mockFetchOnce();
  await respondCheckpoint("r1", 7, "edit", "tweak it", "opus", () => {});
  expect(calls[0].url).toBe("/api/sessions/r1/checkpoints/7");
  expect(JSON.parse(calls[0].init.body)).toEqual({ action: "edit", body: "tweak it", model: "opus" });
  vi.unstubAllGlobals();
});

test("streamPost surfaces a non-OK server error instead of completing silently", async () => {
  // A 409 at session cap returns JSON, not SSE; the caller must see the error.
  vi.stubGlobal("fetch", () =>
    Promise.resolve({ ok: false, status: 409, json: async () => ({ error: "max reached" }) }));
  await expect(startTask("r1", "x", "auto", () => {})).rejects.toThrow("max reached");
  vi.unstubAllGlobals();
});

test("parses complete SSE frames and keeps remainder", () => {
  const buf = 'event: narration\ndata: {"text":"hi"}\n\nevent: done\ndata: {"m":1}\n\nevent: partial\ndata: {';
  const { events, rest } = parseSseChunk(buf);
  expect(events).toEqual([
    { kind: "narration", data: { text: "hi" } },
    { kind: "done", data: { m: 1 } },
  ]);
  expect(rest.startsWith("event: partial")).toBe(true);
});

test("ignores empty blocks so blank separators emit no phantom events", () => {
  // A stray blank line before a real frame must not produce a spurious
  // {kind:"message"} event.
  const buf = '\n\nevent: done\ndata: {"m":1}\n\n';
  const { events } = parseSseChunk(buf);
  expect(events).toEqual([{ kind: "done", data: { m: 1 } }]);
});
