# Live files view — watch the agent edit the workspace

2026-07-07

## Problem

The workspace's left pane follows the agent's browser while a turn screencasts
it (QA, executor browsing). But most of a build turn is *editing* — explore,
plan, write code — and during that stretch the pane shows the (often stale)
app and the only signal is the chat's one-line tool stream. Watching a session
build a feature, nothing appears to happen.

## Shape

A third left-pane source, `files`, joining `app` and `agent`:

- **Tree** of the run's workspace (tracked + untracked-not-ignored), each file
  tagged with its git status; the file the agent just touched pulses (accent
  for an edit, neutral for a read).
- **Detail** panel showing the picked file's uncommitted diff (untracked files
  render as an all-additions `--no-index` pseudo-diff) or its raw content.
- **Follow mode** (default): the detail tracks whatever the agent last
  edited/wrote and re-fetches when it's edited again; clicking a file pins it,
  `⦿ follow` resumes.
- **Auto pane**: browser screencast when live (it's the closer view of the
  agent) → files while a turn is streaming edits → app otherwise. Explicit
  pins stick; the toggle is always visible (`agent` only while streaming).

## Plumbing

- `worker.StreamEvent` gains `path` — the workspace-relative path for
  file-touching tools (`/work/` stripped; paths outside `/work` dropped).
  Both providers emit it (claude via `_tool_path`, codex via `file_change`
  items). `session._stream_worker` forwards it on the `tool` TurnEvent, so
  every surface (web POST stream, bus feed → Slack-driven turns) carries it.
- `forge/workfiles.py` + two routes:
  - `GET /api/sessions/{id}/files` → `{files: [{path, status}], truncated}`
  - `GET /api/sessions/{id}/file?path=` → `{path, status, size, truncated,
    binary, missing, content, diff}`
  Host-side and **read-only** (`status`/`ls-files`/`diff` via `hardened_git` —
  the repo's `.git/config` is agent-writable, treat as hostile). Reading the
  bind-mounted workspace from the host works while the agent holds the
  container busy mid-turn. Listing goes through git, so the
  `.git/info/exclude` scratch patterns keep `.forge/*` invisible here just as
  in PRs. Client paths are resolved strictly inside the workspace (traversal,
  symlink escape and `.git/**` → 404). Same loopback-only exposure as `/diff`.
- Frontend: `filesModel.ts` (pure: touch stream, tree fold, auto-open dirs,
  follow selection) + `FilesView.tsx`; `Chat` gains `onFile` so the Workspace
  hears file touches from foreign turns too; `agentBrowser.resolvePane/nextPin`
  extended with the `files` pane and an `editsLive` input.

## Deliberate choices

- **No SSE for file content** — frames taught us content doesn't belong on the
  bus (replay buffer, Slack tap). Tool events carry only the path; the pane
  fetches listing/detail over plain GETs, debounced (800ms list / 400ms
  detail), so a fast-editing turn can't hammer git.
- **Follow edits, not reads** — reads flash in the tree but don't steal the
  detail panel; following the agent's greps around the repo would be seasick.
- **Auto-return** — `editsLive` drops on `done`/turn end, so the pane hands
  back to the app right when the reload nudge fires and the change is visible.
- Content/diff caps at 200 kB, listing at 20k files, binary sniff by NUL byte.
