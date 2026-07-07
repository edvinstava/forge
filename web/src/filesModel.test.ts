import { describe, it, expect } from "vitest";
import {
  touchAction, pushTouch, lastEdit, selectedPath, buildTree,
  ancestorDirs, autoOpenDirs, isDirOpen, type FileTouch,
} from "./filesModel";
import type { WorkspaceFile } from "./types";

const touch = (path: string, action: "read" | "edit", ts = 0): FileTouch =>
  ({ path, action, ts });

describe("touchAction", () => {
  it("maps edit-like tools to edit and read-like to read", () => {
    expect(touchAction("Edit")).toBe("edit");
    expect(touchAction("Write")).toBe("edit");
    expect(touchAction("NotebookEdit")).toBe("edit");
    expect(touchAction("Read")).toBe("read");
    expect(touchAction("Bash")).toBeNull();
    expect(touchAction("Grep")).toBeNull();
  });
});

describe("pushTouch", () => {
  it("appends and stays bounded", () => {
    let ts: FileTouch[] = [];
    for (let i = 0; i < 250; i++) ts = pushTouch(ts, touch(`f${i}`, "edit", i), 200);
    expect(ts.length).toBe(200);
    expect(ts[ts.length - 1].path).toBe("f249");
    expect(ts[0].path).toBe("f50");
  });
});

describe("selectedPath / lastEdit", () => {
  const stream = [
    touch("a.ts", "read", 1),
    touch("b.ts", "edit", 2),
    touch("c.ts", "read", 3),
  ];
  it("follow mode tracks the last EDIT, not the last read", () => {
    expect(lastEdit(stream)?.path).toBe("b.ts");
    expect(selectedPath(null, stream)).toBe("b.ts");
  });
  it("an explicit pick wins over follow", () => {
    expect(selectedPath("z.ts", stream)).toBe("z.ts");
  });
  it("no edits yet → nothing selected", () => {
    expect(selectedPath(null, [touch("a.ts", "read", 1)])).toBeNull();
  });
});

describe("buildTree", () => {
  const files: WorkspaceFile[] = [
    { path: "README.md", status: "clean" },
    { path: "src/app/page.tsx", status: "modified" },
    { path: "src/app/new.tsx", status: "untracked" },
    { path: "src/lib.ts", status: "clean" },
  ];
  it("folds flat paths into nested dirs preserving order", () => {
    const root = buildTree(files);
    expect(root.files.map((f) => f.path)).toEqual(["README.md"]);
    expect(root.dirs.map((d) => d.path)).toEqual(["src"]);
    const src = root.dirs[0];
    expect(src.dirs.map((d) => d.path)).toEqual(["src/app"]);
    expect(src.files.map((f) => f.path)).toEqual(["src/lib.ts"]);
    expect(src.dirs[0].files.map((f) => f.path)).toEqual(
      ["src/app/page.tsx", "src/app/new.tsx"]);
  });
});

describe("open dirs", () => {
  const files: WorkspaceFile[] = [
    { path: "src/app/page.tsx", status: "modified" },
    { path: "docs/notes.md", status: "clean" },
  ];
  it("ancestors of changed files and the selection auto-open", () => {
    const auto = autoOpenDirs(files, "docs/notes.md");
    expect(auto.has("src")).toBe(true);
    expect(auto.has("src/app")).toBe(true);
    expect(auto.has("docs")).toBe(true);
  });
  it("clean, unselected dirs stay closed; user override wins both ways", () => {
    const auto = autoOpenDirs(files, null);
    expect(auto.has("docs")).toBe(false);
    expect(isDirOpen("docs", new Map(), auto)).toBe(false);
    expect(isDirOpen("docs", new Map([["docs", true]]), auto)).toBe(true);
    expect(isDirOpen("src", new Map([["src", false]]), auto)).toBe(false);
  });
  it("ancestorDirs lists every prefix", () => {
    expect(ancestorDirs("a/b/c.ts")).toEqual(["a", "a/b"]);
    expect(ancestorDirs("c.ts")).toEqual([]);
  });
});
