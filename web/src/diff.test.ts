import { expect, test } from "vitest";
import { parseDiff } from "./diffModel";

const SAMPLE = `diff --git a/src/app.tsx b/src/app.tsx
index 1111111..2222222 100644
--- a/src/app.tsx
+++ b/src/app.tsx
@@ -1,4 +1,5 @@
 import React from "react";
 import "./globals.css";
-export default function App() {
+export default function App({ x }) {
+  console.log(x);
   return null;
 }
diff --git a/new.ts b/new.ts
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.ts
@@ -0,0 +1,2 @@
+export const a = 1;
+export const b = 2;
`;

test("splits into files with correct paths and status", () => {
  const files = parseDiff(SAMPLE);
  expect(files.map((f) => f.path)).toEqual(["src/app.tsx", "new.ts"]);
  expect(files[0].status).toBe("modified");
  expect(files[1].status).toBe("added");
});

test("counts additions and deletions per file", () => {
  const files = parseDiff(SAMPLE);
  expect(files[0].additions).toBe(2);
  expect(files[0].deletions).toBe(1);
  expect(files[1].additions).toBe(2);
  expect(files[1].deletions).toBe(0);
});

test("assigns gutter line numbers from the hunk header", () => {
  const [first] = parseDiff(SAMPLE);
  const firstCtx = first.rows.find((r) => r.type === "ctx");
  expect(firstCtx?.oldNo).toBe(1);
  expect(firstCtx?.newNo).toBe(1);
  const add = first.rows.find((r) => r.type === "add");
  expect(add?.oldNo).toBeNull();
  expect(typeof add?.newNo).toBe("number");
});

test("empty diff yields no files", () => {
  expect(parseDiff("")).toEqual([]);
  expect(parseDiff("   \n")).toEqual([]);
});
