# SSE Event Contract

> **Backend↔frontend contract.** Every event emitted by `SessionManager` generators
> is serialised by `webapp._sse` as:
>
> ```
> event: <kind>
> data: <json>
>
> ```
>
> Field-name mismatches silently break the frontend — update this file whenever a
> new `TurnEvent` kind is added or an existing `data` shape changes.

## The live event feed (`GET /api/sessions/{run_id}/events`)

Every flow also publishes its events to an in-process per-run bus
(`forge/eventbus.py`), stamped with a per-run monotonic `seq` and the driving
surface's `origin` (`"web"`, `"slack"`, `"queue"`, `"api"`). The feed endpoint
streams the same kinds as above, with `seq` and `origin` **merged into `data`**:

```
event: narration
data: {"text": "editing", "seq": 42, "origin": "slack"}
```

Query params: `since=<seq>` replays buffered events after that seq
(`since=-1` = tail-only from now); `tail=0` returns the backlog and closes
(catch-up). Idle streams get `: ping` heartbeat comments. Clients dedup by
`seq` (their own POST-stream copies carry no stamp — see `web/src/liveFeed.ts`).
The Slack mirror consumes the same bus via `bot.attach_bus` and filters
`origin ∈ {"slack", "queue"}` so nothing renders twice.

**`stream_end` (bus/feed only).** When a flow's generator finishes — by any
path — the `@published` wrapper publishes a synthetic
`{"kind": "stream_end", "data": {}}` to the bus (never yielded on POST
streams). Followers use it to release live UI state deterministically: several
flows end without a `done` (`wake` stops at `url`, `plan_task` at
`checkpoint`).

## Event kinds

### `session`
Emitted once by the webapp (not `SessionManager`) before the generator runs, so
the client knows its `run_id` before any other events arrive.

| field | type | description |
|-------|------|-------------|
| `run_id` | string | the allocated run identifier |

---

### `model`
Emitted at the start of `turn`, `plan_task`, and `_review_pass` after the model
alias is resolved.

| field | type | description |
|-------|------|-------------|
| `choice` | string | the model alias the caller supplied (e.g. `"auto"`) |
| `resolved` | string | the concrete model that will run (e.g. `"opus"`) |

---

### `phase`
Emitted at major lifecycle transitions (clone, recipe, up, agent, planning, …).

| field | type | description |
|-------|------|-------------|
| `name` | string | machine-readable phase key (e.g. `"clone"`, `"up"`, `"agent"`, `"planning"`, `"repair"`, `"qa"`, `"probe"`, `"noweb"`, `"wake"`) |
| `label` | string | human-readable label for the UI (e.g. `"Cloning"`, `"Starting stack"`) |

---

### `narration`
Emitted for each assistant narration line streamed from the worker.

| field | type | description |
|-------|------|-------------|
| `text` | string | the narration text |

---

### `tool`
Emitted for each tool-use event streamed from the worker.

| field | type | description |
|-------|------|-------------|
| `name` | string | tool name (e.g. `"Read"`, `"Bash"`) |
| `target` | string \| null | primary argument to the tool, if present |

---

### `verify`
Emitted by `_repair` (the verify-before-commit loop used by `_execute` and
`open_pr`): an initial result, then a final result after repair. Only emitted
when the repo has real verification configured (`has_real_verification`).

| field | type | description |
|-------|------|-------------|
| `ok` | boolean | `true` = all checks passed, `false` = at least one failed |
| `failed` | string[] | names of failing check commands (empty when `ok` is `true`) |
| `output` | string | combined stdout+stderr of failing checks, capped at 4 000 chars |

---

### `repair`
Emitted by `_repair` once per fix iteration of the verify-before-commit repair
loop, between the initial and final `verify` events.

| field | type | description |
|-------|------|-------------|
| `iter` | integer | 1-based repair-attempt number (bounded by `max_repair_iters`) |
| `failed` | string[] | check names still failing that this iteration is fixing |

---

### `qa`
Emitted by `_qa` after each browser-acceptance turn — once when QA is advisory
(`qa_gating` off), once per `_qa_gate` round when gated. Runs only when the plan
has `acceptance` criteria and the run has a live app.

| field | type | description |
|-------|------|-------------|
| `checked` | integer | acceptance criteria evaluated in the browser (0 = no/invalid `.forge/qa.json` → inconclusive, never gates) |
| `failed` | string[] | criteria that failed in the browser (empty = all passed or inconclusive) |
| `summary` | string | the worker's one-line QA summary |

When gated and acceptance can't be satisfied within the repair budget, `_execute`
raises a `checkpoint` of type `repair_escalation` carrying `kind: "acceptance"`
(vs the CI `repair_escalation`, which has no `kind`); it never pushes a red tree.

---

### `url`
Emitted after successful provisioning and after each `turn`/`_execute` that
refreshes the app URL.

| field | type | description |
|-------|------|-------------|
| `web_url` | string | the canonical app URL (public tunnel origin, or `http://localhost:<port>`) |
| `local_url` | string \| null | DNS-free `http://run-<id>.<domain>:<port>` proxy link; present only when a tunnel fronts the shared Caddy for this run |

---

### `plan`
Emitted by `plan_task` and `respond_checkpoint` (edit path) after the planner
writes a valid `.forge/plan.json`.

| field | type | description |
|-------|------|-------------|
| `goal` | string | one-line description of what the task achieves |
| `steps` | array | ordered list of `{id, intent}` objects |
| `acceptance` | string[] | acceptance criteria for the plan |
| `assumptions` | string[] | assumptions the planner made |
| `open_questions` | string[] | questions for the user before proceeding (empty = none) |
| `risk` | string | planner's risk assessment (`"low"`, `"medium"`, `"high"`) |

---

### `checkpoint`
Emitted by `plan_task` (plan-approval gate / open questions), `respond_checkpoint`
(edit path, after replanning), and `_execute` (repair-escalation when the repair
loop exhausts its budget still red).

| field | type | description |
|-------|------|-------------|
| `id` | integer | checkpoint row id; pass back as `cid` to `POST /checkpoints/{cid}` |
| `type` | string | `"plan_approval"`, `"ambiguity"`, or `"repair_escalation"` |
| `prompt` | string | human-readable instruction (repair-escalation lists the still-failing checks) |

---

### `checkpoint_answered`
Emitted by `respond_checkpoint` immediately after the answer is recorded and
before the resumed turn streams. The surface that did NOT answer uses it to
close its pending ask (the web clears its plan gate; Slack posts an
"answered from the web" note).

| field | type | description |
|-------|------|-------------|
| `id` | integer | the checkpoint row id that was answered |
| `action` | string | `"approve"`, `"edit"`, or `"reject"` |
| `body` | string \| null | free-text guidance for `edit` (null otherwise) |

---

### `slept`
Emitted by `_pause_if_requested` when a deferred sleep lands at a phase
boundary. The generator returns right after it.

| field | type | description |
|-------|------|-------------|
| `message` | string | human-readable pause notice |

---

### `creds_saved`
Emitted by `respond_checkpoint` when a `needs_input` answer contained parseable
credentials that were saved to the repo's knowledge overlay.

| field | type | description |
|-------|------|-------------|
| `repo` | string | repo slug the login was saved for |

---

### `review`
Emitted by `_review_pass` as the terminal event of the `review` generator.

| field | type | description |
|-------|------|-------------|
| `ok` | boolean | `true` = review posted successfully |
| `review_url` | string | GitHub review URL (present when `ok` is `true`) |
| `comments` | integer | number of inline comments kept (in-diff) |
| `dropped` | integer | number of comments folded into summary (off-diff) |
| `degraded` | boolean | `true` = posted under the user token (no GitHub App), review is branded |
| `reason` | string | present when `ok` is `false`; describes the failure |

---

### `done`
Terminal event for `turn`, `_execute`, and the reject/abort paths of
`respond_checkpoint`. On a green `_execute` completion it carries the opened PR.

| field | type | description |
|-------|------|-------------|
| `message` | string | the agent's final result text |
| `diff_files` | integer | number of files changed in the current diff |
| `verify_ok` | boolean \| null | `true` / `false` / `null` (null = no checks configured) |
| `pr_url` | string | present when `_execute` auto-completed on green — the opened PR URL |

---

### `retrospective`
Emitted by `_execute` just before `done`, after a PR is opened, when the post-PR
retrospective saved one or more durable lessons to the repo's knowledge overlay.
Best-effort — absent when learning is off (`FORGE_LEARN=0`), no lessons were
learned, or it failed. (The lessons feed the next run's planner.)

| field | type | description |
|-------|------|-------------|
| `added` | integer | new lessons saved to `~/.forge/knowledge/<owner>/<repo>.yml` (a `lessons` list of `{text, kind, evidence, added_run}`, deduped + capped) |

---

### `error`
Emitted whenever a generator encounters a fatal condition and returns early.
Always the last event when present.

| field | type | description |
|-------|------|-------------|
| `kind` | string | machine-readable error code (see table below) |
| `detail` | string | human-readable explanation, capped at 300 chars |

**`kind` values:**

| kind | emitted by | meaning |
|------|-----------|---------|
| `repo` | `start` | repo URL/slug is invalid or not a GitHub repo |
| `clone` | `start`, `review` | `git clone` / `gh pr checkout` failed |
| `ports` | `_provision` | no free Supabase port block |
| `host_pre` | `_provision` | a `host_pre` command (e.g. `supabase start`) failed |
| `up` | `_provision` | `docker compose up` failed |
| `health` | `_provision` | health check timed out after optional self-heal |
| `worker` | `turn`, `plan_task`, `_execute`, `_review_pass` | no result event from the worker stream |
| `auth` | `turn`, `_review_pass` | Claude auth/usage error |
| `plan` | `plan_task`, `respond_checkpoint` | planner produced no valid `.forge/plan.json` |
| `busy` | `turn`, `plan_task`, `respond_checkpoint`, `wake` | a turn is already in flight for this run |
| `not_provisioned` | `plan_task`, `respond_checkpoint` | session has no live container; start or wake it first |
| `gone` | `wake` | workspace directory deleted; session cannot be woken |
| `checkpoint` | `respond_checkpoint` | no matching open checkpoint |
| `prref` | `review` | PR reference string could not be parsed |
