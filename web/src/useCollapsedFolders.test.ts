import { expect, test } from "vitest";
import {
  parseCollapsed,
  serializeCollapsed,
  toggleInSet,
  isFolderOpen,
} from "./useCollapsedFolders";

test("parseCollapsed: null / empty array → empty set", () => {
  expect(parseCollapsed(null).size).toBe(0);
  expect(parseCollapsed("[]").size).toBe(0);
});

test("parseCollapsed: JSON array of keys → set", () => {
  const set = parseCollapsed('["a/b","c/d"]');
  expect([...set].sort()).toEqual(["a/b", "c/d"]);
});

test("parseCollapsed: malformed JSON → empty set, never throws", () => {
  expect(parseCollapsed("not json").size).toBe(0);
});

test("parseCollapsed: non-array JSON → empty set", () => {
  expect(parseCollapsed('{"a":1}').size).toBe(0);
});

test("serialize → parse round-trips", () => {
  const set = new Set(["x", "y"]);
  expect([...parseCollapsed(serializeCollapsed(set))].sort()).toEqual(["x", "y"]);
});

test("toggleInSet adds then removes, returning a new set each time", () => {
  const empty = new Set<string>();
  const added = toggleInSet(empty, "k");
  expect(added.has("k")).toBe(true);
  expect(empty.has("k")).toBe(false); // original untouched (immutable for React state)
  const removed = toggleInSet(added, "k");
  expect(removed.has("k")).toBe(false);
});

test("isFolderOpen: open when not in the collapsed set", () => {
  const collapsed = new Set<string>();
  expect(isFolderOpen({ collapsed, repo: "a/b", activeRepo: null, filtering: false })).toBe(true);
});

test("isFolderOpen: closed when in the collapsed set", () => {
  const collapsed = new Set(["a/b"]);
  expect(isFolderOpen({ collapsed, repo: "a/b", activeRepo: null, filtering: false })).toBe(false);
});

test("isFolderOpen: active folder is always open even if collapsed", () => {
  const collapsed = new Set(["a/b"]);
  expect(isFolderOpen({ collapsed, repo: "a/b", activeRepo: "a/b", filtering: false })).toBe(true);
});

test("isFolderOpen: filtering forces every folder open", () => {
  const collapsed = new Set(["a/b"]);
  expect(isFolderOpen({ collapsed, repo: "a/b", activeRepo: null, filtering: true })).toBe(true);
});
