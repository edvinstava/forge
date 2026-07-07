# Review: inline comments + live browser check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make forge PR reviews land findings as inline PR comments and drive the PR's live app in a real browser, leading the review body with a ✅/❌ live-check verdict.

**Architecture:** Harden the existing single review worker (no second QA-style worker). The review worker gets the same shared-CDP-Chromium tooling QA turns get (watchable in the #live workspace) and credentials; its `review.json` schema becomes inline-first and gains a `live_check` object. Pure logic in `review.py` snaps near-miss anchors and renders the live-check section; `session_review.py` wires browser + creds + redaction; `slackbot.py` surfaces screenshots. Everything is advisory (`event=COMMENT`) and best-effort.

**Tech Stack:** Python 3.11+, pytest (hermetic FakeHost/FakeEnv/FakeClient patterns), no new dependencies.

**Spec:** `docs/specs/2026-07-07-review-inline-live-design.md` (on `master`, commit `3da4261`).

## Global Constraints

- **Advisory only:** the posted review's `event` is ALWAYS `"COMMENT"`. A failed live check is reported loudly in the body, never `REQUEST_CHANGES`/`APPROVE`. The worker must not modify code.
- **Triggers unchanged:** web `/api/review`, Slack review intent, and the `@edvin-forge review` comment-command keep working with no interface change. `SessionManager.review(run_id, pr_ref, model="auto")` signature is unchanged.
- **Backward compatible:** `live_check` is OPTIONAL everywhere. `build_self_review_prompt` is untouched and writes no `live_check`; old workers wrote none. `parse_review` must stay tolerant and `build_payload` must omit the live-check section entirely when it is absent.
- **Best-effort, never fatal:** browserview start/stop failure, credential absence, screenshot capture failure, and artifact upload failure must never fail the review.
- **Fire-and-forget reviews:** a login wall never pauses for an interactive checkpoint — it is reported as `live_check.status = "blocked"` in the posted review.
- **Follow existing conventions:** mirror the QA turn (`session.py::_qa`) for browser/creds/redaction wiring; keep tests hermetic (FakeHost/FakeEnv/FakeClient, `monkeypatch.setattr(mgr, "_method", ...)`).

---

### Task 1: `review.py` — `live_check` parsing + posted body section

Adds an optional `live_check` field to the parsed `Review`, tolerant parsing of the worker's `live_check` object, and a live-check section at the top of the posted review body (after the degrade header, before the summary). Pure logic, no I/O.

**Files:**
- Modify: `src/forge/review.py` (dataclass `Review` ~L16-19; `parse_review` ~L22-39; `build_payload` ~L79-89)
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Review(summary: str, comments: list, live_check: dict | None = None)` — frozen dataclass, third field defaults to `None`.
  - `parse_review(data) -> Review` — now also fills `live_check` (normalized `{"status", "tested", "notes"}` or `None`).
  - `build_payload(review, valid, dropped, header="") -> dict` — body order is `header + live-check section + summary + dropped-notes`; section omitted when `review.live_check` is falsy. `event` stays `"COMMENT"`.
  - Module constants `_LIVE_STATUSES = ("pass", "fail", "skipped", "blocked")` and `_LIVE_EMOJI = {"pass": "✅", "fail": "❌", "skipped": "⏭️", "blocked": "🔒"}`.

- [ ] **Step 1: Write failing tests for `live_check` parsing + rendering**

Add to `tests/test_review.py`:

```python
def test_parse_review_live_check_absent_is_none():
    assert parse_review('{"summary":"s","comments":[]}').live_check is None


def test_parse_review_live_check_malformed_is_none():
    # non-dict, and dict with an unknown status → None (tolerant)
    assert parse_review('{"summary":"s","live_check":"nope"}').live_check is None
    assert parse_review('{"summary":"s","live_check":{"status":"weird"}}').live_check is None


def test_parse_review_live_check_normalized():
    r = parse_review('{"summary":"s","live_check":{"status":"PASS",'
                     '"tested":["login flow", 0, "logout"],"notes":"all good"}}')
    assert r.live_check == {"status": "pass",
                            "tested": ["login flow", "0", "logout"],
                            "notes": "all good"}


def test_build_payload_live_check_section_order_and_emoji():
    rev = Review("Overall solid.", [], {"status": "pass",
                                         "tested": ["ordered a widget"],
                                         "notes": "checkout worked"})
    p = build_payload(rev, [], [], header="🔨 Forge Review\n\n")
    body = p["body"]
    assert body.startswith("🔨 Forge Review")
    # live-check block appears, with the pass emoji, before the summary text
    assert "✅" in body and "Live check" in body
    assert "ordered a widget" in body and "checkout worked" in body
    assert body.index("Live check") < body.index("Overall solid.")


def test_build_payload_live_check_emoji_per_status():
    for status, emoji in [("fail", "❌"), ("skipped", "⏭️"), ("blocked", "🔒")]:
        p = build_payload(Review("s", [], {"status": status, "tested": [],
                                           "notes": ""}), [], [])
        assert emoji in p["body"]


def test_build_payload_omits_live_check_when_absent():
    # old workers / self-review: no live_check → no section, fully compatible
    p = build_payload(Review("just a summary", []), [], [])
    assert "Live check" not in p["body"]
    assert p["body"] == "just a summary"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_review.py -k "live_check" -v`
Expected: FAIL — `TypeError` (Review takes 2 positional args) / `AttributeError: 'Review' object has no attribute 'live_check'`.

- [ ] **Step 3: Add the `live_check` field to `Review`**

In `src/forge/review.py`, change the `Review` dataclass:

```python
@dataclass(frozen=True)
class Review:
    summary: str
    comments: list
    live_check: dict | None = None
```

- [ ] **Step 4: Add live-check constants + tolerant parse**

In `src/forge/review.py`, add the constants just above `parse_review` and a helper, then thread it into `parse_review`'s return:

```python
_LIVE_STATUSES = ("pass", "fail", "skipped", "blocked")


def _parse_live_check(data: dict):
    """Tolerant parse of the optional live_check object. Returns a normalized
    {status, tested, notes} only when status is a known value; otherwise None
    (old workers and self-review write no live_check)."""
    lc = data.get("live_check")
    if not isinstance(lc, dict):
        return None
    status = str(lc.get("status", "")).lower()
    if status not in _LIVE_STATUSES:
        return None
    raw = lc.get("tested")
    tested = [str(t) for t in raw if t or t == 0] if isinstance(raw, list) else []
    return {"status": status, "tested": tested, "notes": str(lc.get("notes") or "")}
```

Then change the final line of `parse_review` from:

```python
    return Review(str(data.get("summary") or ""), out)
```

to:

```python
    return Review(str(data.get("summary") or ""), out, _parse_live_check(data))
```

- [ ] **Step 5: Render the live-check section in `build_payload`**

In `src/forge/review.py`, add the emoji map + section helper above `build_payload`, and prepend the section to the body:

```python
_LIVE_EMOJI = {"pass": "✅", "fail": "❌", "skipped": "⏭️", "blocked": "🔒"}


def _live_check_section(lc) -> str:
    """Render the live-check block for the top of the review body. Empty string
    when lc is falsy (absent) — old workers and self-review stay compatible."""
    if not lc:
        return ""
    emoji = _LIVE_EMOJI.get(lc.get("status", ""), "")
    out = [f"**Live check:** {emoji} {lc.get('status', '')}".rstrip()]
    out += [f"- {t}" for t in lc.get("tested") or []]
    if lc.get("notes"):
        out.append(lc["notes"])
    return "\n".join(out) + "\n\n"
```

Change the first line of `build_payload` from:

```python
    body = header + (review.summary or "")
```

to:

```python
    body = header + _live_check_section(review.live_check) + (review.summary or "")
```

(The rest of `build_payload` — dropped-notes folding, `event="COMMENT"`, comments list — is unchanged.)

- [ ] **Step 6: Run the tests to verify they pass (and nothing regressed)**

Run: `python -m pytest tests/test_review.py -v`
Expected: PASS — the new live-check tests plus all pre-existing `test_review.py` tests (parse/partition/build_payload/url).

- [ ] **Step 7: Commit**

```bash
git add src/forge/review.py tests/test_review.py
git commit -m "feat(review): parse and render live_check in the posted review body"
```

---

### Task 2: `review.py` — snap near-miss inline anchors

`partition` gains a snap step: a comment whose `(side, line)` is not directly anchorable tries the nearest anchorable line in the same file within ±3 lines (nearest wins; ties prefer `RIGHT`). Hits are re-anchored and counted `valid`; misses are dropped to the body as today. Pure logic.

**Files:**
- Modify: `src/forge/review.py` (`partition` ~L69-76; add `_snap` helper above it)
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `Comment(path, line, side, body)` (frozen), `line_map: {path -> set((side, line))}` from `diff_line_map`.
- Produces:
  - `partition(review, line_map) -> (valid, dropped)` — `valid` now includes re-anchored copies (new `line`/`side`) of near-miss comments.
  - `_snap(comment: Comment, anchors: set) -> Comment | None` — nearest anchor within ±3 lines, sort key `(distance, 0 if RIGHT else 1)`; `None` when the window is empty.

- [ ] **Step 1: Write failing tests for snap anchoring**

Add to `tests/test_review.py` (reuses the module-level `DIFF`; `diff_line_map(DIFF)["foo.py"]` contains RIGHT 1-4 and LEFT 1-3):

```python
def test_partition_snaps_near_miss_to_nearest_line():
    # (RIGHT, 6) is off-diff; nearest anchor within ±3 is (RIGHT, 4), dist 2.
    rev = Review("s", [Comment("foo.py", 6, "RIGHT", "near miss")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert dropped == []
    assert (valid[0].line, valid[0].side, valid[0].body) == (4, "RIGHT", "near miss")


def test_partition_snap_prefers_right_on_distance_tie():
    # hand-built map: RIGHT 5 and LEFT 5 are equidistant (dist 1) from line 6.
    line_map = {"foo.py": {("RIGHT", 5), ("LEFT", 5)}}
    rev = Review("s", [Comment("foo.py", 6, "RIGHT", "tie")])
    valid, _ = partition(rev, line_map)
    assert (valid[0].line, valid[0].side) == (5, "RIGHT")


def test_partition_snap_nearest_beats_farther():
    line_map = {"foo.py": {("RIGHT", 4), ("RIGHT", 7)}}
    rev = Review("s", [Comment("foo.py", 6, "RIGHT", "x")])
    valid, _ = partition(rev, line_map)
    assert valid[0].line == 7          # dist 1 beats dist 2


def test_partition_drops_when_no_anchor_within_window():
    # (RIGHT, 8): nearest anchor (RIGHT, 4) is dist 4 (> 3) → still dropped.
    rev = Review("s", [Comment("foo.py", 8, "RIGHT", "too far"),
                       Comment("other.py", 1, "RIGHT", "no file")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert valid == []
    assert {c.body for c in dropped} == {"too far", "no file"}


def test_partition_exact_anchor_still_valid_unchanged():
    # a directly-anchorable comment is kept as-is (not re-snapped).
    rev = Review("s", [Comment("foo.py", 3, "RIGHT", "exact")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert dropped == [] and valid[0] == Comment("foo.py", 3, "RIGHT", "exact")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_review.py -k "snap or near_miss or window or tie" -v`
Expected: FAIL — the off-diff comments are currently dropped (no snap), so `test_partition_snaps_near_miss_to_nearest_line` etc. fail their assertions.

- [ ] **Step 3: Add `_snap` and rewrite `partition`**

In `src/forge/review.py`, add `_snap` above `partition` and replace `partition`'s body:

```python
def _snap(comment: Comment, anchors: set):
    """Re-anchor an off-diff comment to the nearest anchorable (side, line) in
    the same file within ±3 lines. Sort key (distance, side) prefers the nearest
    line, then RIGHT over LEFT on ties. Returns a re-anchored Comment, or None
    when nothing anchorable is within the window."""
    best = None
    for side, line in anchors:
        dist = abs(line - comment.line)
        if dist > 3:
            continue
        key = (dist, 0 if side == "RIGHT" else 1)
        if best is None or key < best[0]:
            best = (key, side, line)
    if best is None:
        return None
    _, side, line = best
    return Comment(comment.path, line, side, comment.body)


def partition(review: Review, line_map: dict):
    valid, dropped = [], []
    for c in review.comments:
        anchors = line_map.get(c.path, set())
        if (c.side, c.line) in anchors:
            valid.append(c)
            continue
        snapped = _snap(c, anchors)
        if snapped is not None:
            valid.append(snapped)
        else:
            dropped.append(c)
    return valid, dropped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_review.py -v`
Expected: PASS — new snap tests plus the pre-existing `test_partition_keeps_in_diff_drops_off_diff` (its off-diff comments at lines 50 and `other.py:1` are >3 from any anchor, so they still drop).

- [ ] **Step 5: Commit**

```bash
git add src/forge/review.py tests/test_review.py
git commit -m "feat(review): snap near-miss inline anchors within ±3 lines"
```

---

### Task 3: `prompts.py` — inline-first schema + live-check + credentials in the review prompt

Rewrites `_REVIEW_SCHEMA` to be inline-first with a required `live_check` object; extends `build_review_prompt` to add credentials, the shared-browser block, a derive-and-exercise instruction, an anti-brute-force guardrail (→ `blocked`), and a `skipped` instruction when there is no app. Extracts the credential rendering shared with QA into `_cred_block` (behaviour-preserving for `build_qa_prompt`).

**Files:**
- Modify: `src/forge/prompts.py` (`_REVIEW_SCHEMA` ~L180-192; `build_review_prompt` ~L195-207; `build_qa_prompt` ~L275-317; add `_cred_block` helper)
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `_SHARED_BROWSER` (existing module constant, contains `http://127.0.0.1:9222`).
- Produces:
  - `build_review_prompt(slug: str, number: int, app_url: str | None, credentials=None) -> str` — new optional `credentials` keyword.
  - `_cred_block(credentials) -> str` — shared credential-rows block (empty string when none). Used by both `build_qa_prompt` and `build_review_prompt`.
  - `_REVIEW_SCHEMA` now documents `summary` + `live_check` + `comments`, inline-first.

- [ ] **Step 1: Write failing tests for the new review prompt**

In `tests/test_prompts.py`, REPLACE the existing `test_build_review_prompt_anchors_and_includes_pr_and_app` and `test_build_review_prompt_without_app_url` with the versions below, and ADD the three new tests:

```python
def test_build_review_prompt_anchors_and_includes_pr_and_app():
    from forge.prompts import build_review_prompt
    p = build_review_prompt("o/r", 42, "http://web:3000")
    assert "o/r#42" in p
    assert ".forge/review.json" in p
    assert "diff" in p.lower()                       # anchor-to-diff instruction
    assert "http://web:3000" in p                    # the running app URL
    assert "127.0.0.1:9222" in p                     # shared browser (drive it live)
    assert "live_check" in p                         # verdict object required
    assert "advisory" in p.lower() and "do not modify" in p.lower()


def test_build_review_prompt_without_app_url():
    from forge.prompts import build_review_prompt
    p = build_review_prompt("o/r", 1, None)
    assert "live instance" not in p.lower()          # no app line when none
    assert "127.0.0.1:9222" not in p                 # no browser block
    assert "skipped" in p                            # instructs live_check=skipped


def test_build_review_prompt_includes_credentials_and_guardrail():
    from forge.prompts import build_review_prompt
    creds = [{"role": "admin", "username": "a@b.c", "password": "pw"}]
    p = build_review_prompt("o/r", 2, "http://web:3000", credentials=creds)
    assert "CREDENTIALS" in p
    assert "role=admin" in p and "a@b.c" in p and "pw" in p
    assert "brute" in p.lower()                      # never brute-force logins
    assert "blocked" in p                            # login wall → live_check blocked


def test_build_review_prompt_no_credentials_block_without_creds():
    from forge.prompts import build_review_prompt
    p = build_review_prompt("o/r", 2, "http://web:3000")
    assert "CREDENTIALS (use" not in p               # none supplied → no block
    assert "brute" in p.lower()                      # guardrail still present


def test_review_schema_lists_all_live_check_statuses():
    from forge.prompts import build_review_prompt
    p = build_review_prompt("o/r", 1, None)
    for status in ("pass", "fail", "skipped", "blocked"):
        assert status in p
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_prompts.py -k "review" -v`
Expected: FAIL — current prompt has no `live_check`, no `127.0.0.1:9222`, no credentials support.

- [ ] **Step 3: Extract `_cred_block` and reuse it in `build_qa_prompt`**

In `src/forge/prompts.py`, add the helper (place it just above `build_qa_prompt`):

```python
def _cred_block(credentials) -> str:
    """Render stored browser credentials for a prompt (shared by QA and review).
    Empty string when none — callers still emit the anti-brute-force guardrail."""
    if not credentials:
        return ""
    rows = "\n".join(
        "- " + " ".join(
            p for p in (f"role={c.get('role')}" if c.get("role") else "",
                        f"username={c.get('username')}",
                        f"password={c.get('password')}") if p)
        for c in credentials)
    return ("\n\nCREDENTIALS (use the entry whose role matches the "
            "criterion, e.g. admin vs user):\n" + rows)
```

Then in `build_qa_prompt`, REPLACE these lines:

```python
    cred_block = ""
    if credentials:
        rows = "\n".join(
            "- " + " ".join(
                p for p in (f"role={c.get('role')}" if c.get("role") else "",
                            f"username={c.get('username')}",
                            f"password={c.get('password')}") if p)
            for c in credentials)
        cred_block = ("\n\nCREDENTIALS (use the entry whose role matches the "
                      "criterion, e.g. admin vs user):\n" + rows)
```

with:

```python
    cred_block = _cred_block(credentials)
```

- [ ] **Step 4: Rewrite `_REVIEW_SCHEMA` (inline-first + live_check)**

In `src/forge/prompts.py`, REPLACE the entire `_REVIEW_SCHEMA` assignment with:

```python
_REVIEW_SCHEMA = (
    "Write your findings to `.forge/review.json` (create the `.forge/` dir) as "
    "strict JSON:\n"
    '{"summary": "<short markdown overview>",\n'
    ' "live_check": {"status": "pass|fail|skipped|blocked", '
    '"tested": ["<flow you exercised>"], '
    '"notes": "<what happened / why skipped or blocked>"},\n'
    ' "comments": [{"path": "<repo-relative path>", "line": <int>, '
    '"side": "RIGHT", "body": "<one finding>"}]}\n'
    "- INLINE-FIRST: every finding that names a specific file/line MUST be a "
    "`comments[]` entry anchored to a line in THIS PR's diff (an added/context "
    "line on side RIGHT, or a removed/context line on side LEFT). Do NOT restate "
    "those findings in `summary`.\n"
    "- `summary` is a SHORT overview only — what you verified and your overall "
    "assessment — not a dump of the inline findings.\n"
    "- `live_check` reports the browser check described above.\n"
    "- High-signal only: correctness/security bugs first, then "
    "reuse/simplification/efficiency. Skip nits and praise.\n"
)
```

- [ ] **Step 5: Rewrite `build_review_prompt`**

In `src/forge/prompts.py`, REPLACE the whole `build_review_prompt` function with:

```python
def build_review_prompt(slug: str, number: int, app_url: str | None,
                        credentials=None) -> str:
    if app_url:
        live = (
            f"\nLIVE CHECK — a live instance of this PR's app is running at "
            f"{app_url}, and your teammate can watch you drive it. {_SHARED_BROWSER}\n"
            "Derive what this PR should change in the running app from "
            "`.forge/pr.diff` and the PR description, exercise exactly that in the "
            "browser, and record the result in `live_check`: status `pass` ONLY if "
            "you observed the changed behavior working, `fail` (with specifics in "
            "`notes`) if it is broken." + _cred_block(credentials) + "\n"
            "Never guess, brute-force, or try common passwords at a login wall. If "
            "you have no working credentials for a role a check needs, set "
            '`live_check.status` to "blocked" naming the exact role required, and '
            "continue with the static (diff-based) review.\n"
            "Screenshots are optional and best-effort: you MAY save PNGs under "
            "`.forge/artifacts/` described in `.forge/artifacts/manifest.json` "
            '({"artifacts": [{"path": "after.png", "kind": "after", '
            '"caption": "…"}]}). Never let capture fail the review.\n")
    else:
        live = ("\nNo running app is available for this PR: set "
                '`live_check.status` to "skipped" with a one-line reason in '
                "`notes`, and review from the diff and code only.\n")
    return (
        "You are a meticulous senior code reviewer. Review the pull request "
        f"{slug}#{number} in this checked-out repository. The full PR diff is "
        "saved at `.forge/pr.diff` — read it to see exactly what changed, and "
        "read the surrounding code for context. You are advisory only: report "
        "findings, do not approve or block, and do not modify the code.\n"
        f"{live}\n{_REVIEW_SCHEMA}"
    )
```

- [ ] **Step 6: Run the review + QA prompt tests to verify pass (QA behaviour preserved)**

Run: `python -m pytest tests/test_prompts.py -v`
Expected: PASS — the new/updated review tests, AND the existing QA tests (`test_qa_prompt_renders_credentials_block_with_roles`, `test_qa_prompt_has_anti_bruteforce_guardrail_always`, etc.) which now exercise `_cred_block` unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/forge/prompts.py tests/test_prompts.py
git commit -m "feat(review): inline-first review schema with live_check + credentials in the prompt"
```

---

### Task 4: `session_review.py` — wire browser, credentials, redaction, artifact reset

`_review_pass` mirrors the QA turn (`session.py::_qa`): load the repo's stored credentials, pass them into `build_review_prompt`, redact their passwords out of the streamed events, bracket the worker with `browserview.start`/`stop` (guarded on `_app_url`, `stop` always in `finally`), and reset stale artifacts at review start. Posting (`_post_review`) already flows through the Task 1/2 `review.py` changes, so the live-check section and snapped anchors appear automatically.

**Files:**
- Modify: `src/forge/session_review.py` (`_review_pass` ~L59-89; import of `build_review_prompt` ~L15)
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `self._qa_credentials(run_id)`, `self._app_url(run_id)`, `self._reset_artifacts(run_id)`, `self._stream_worker(run_id, env, prompt, model, redact=...)` (existing, on `SessionManager`); `build_review_prompt(slug, number, app_url, credentials=...)` (Task 3); `redact_secrets` (`forge.creds`); `browserview.start/stop` (`forge.browserview`).
- Produces: `_review_pass(self, run_id, ref, model)` — generator of `TurnEvent`s (unchanged terminal `review` event); now credential-aware and browser-bracketed.

- [ ] **Step 1: Write failing session-level tests**

Add to `tests/test_session.py` (near the existing `ReviewHost` / `_review_mgr` block around L1038-1106). These reuse `ReviewHost`, `_review_mgr`, and `CapturingEnv`:

```python
def _seed_review_ws(cfg, rid, review_json):
    ws = cfg.runs_dir / rid / "workspace" / ".forge"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "review.json").write_text(review_json)
    return ws


def test_review_pass_uses_browser_and_creds_when_app_url(tmp_path, monkeypatch):
    from forge import browserview
    mgr, store, cfg = _review_mgr(tmp_path)
    mgr.env_factory = lambda rid, files: CapturingEnv(rid, files)
    _seed_review_ws(cfg, "rr", '{"summary":"ok","comments":[]}')
    monkeypatch.setattr(mgr, "_app_url", lambda rid: "http://web:3000")
    monkeypatch.setattr(mgr, "_qa_credentials",
                        lambda rid: [{"role": "admin", "username": "u@x.io",
                                      "password": "s3cret"}])
    started, stopped = [], []
    monkeypatch.setattr(browserview, "start",
                        lambda rd, rid, env, service="forge": started.append(rid))
    monkeypatch.setattr(browserview, "stop", lambda rd, rid: stopped.append(rid))

    list(mgr.review("rr", "o/r#3"))

    assert started == ["rr"] and stopped == ["rr"]        # bracketed
    prompt = _prompt_of(CapturingEnv.last_stream_argv)
    assert "127.0.0.1:9222" in prompt                     # shared browser block
    assert "s3cret" in prompt                             # creds injected


def test_review_pass_no_browser_start_without_app_url(tmp_path, monkeypatch):
    from forge import browserview
    mgr, store, cfg = _review_mgr(tmp_path)
    mgr.env_factory = lambda rid, files: CapturingEnv(rid, files)
    _seed_review_ws(cfg, "rr", '{"summary":"ok","comments":[]}')
    monkeypatch.setattr(mgr, "_app_url", lambda rid: None)
    started, stopped = [], []
    monkeypatch.setattr(browserview, "start",
                        lambda rd, rid, env, service="forge": started.append(rid))
    monkeypatch.setattr(browserview, "stop", lambda rd, rid: stopped.append(rid))

    list(mgr.review("rr", "o/r#3"))

    assert started == []                                  # no app → no stream
    assert stopped == ["rr"]                              # stop still called (finally)
    assert "127.0.0.1:9222" not in _prompt_of(CapturingEnv.last_stream_argv)


def test_review_pass_stops_browser_on_worker_error(tmp_path, monkeypatch):
    import pytest
    from forge import browserview
    mgr, store, cfg = _review_mgr(tmp_path)

    class BoomEnv(FakeEnv):
        def exec_stream(self, argv, service=None, workdir="/work"):
            raise RuntimeError("worker died")
            yield  # pragma: no cover  (make it a generator)

    mgr.env_factory = lambda rid, files: BoomEnv(rid, files)
    _seed_review_ws(cfg, "rr", '{"summary":"ok","comments":[]}')
    monkeypatch.setattr(mgr, "_app_url", lambda rid: "http://web:3000")
    stopped = []
    monkeypatch.setattr(browserview, "start", lambda *a, **k: None)
    monkeypatch.setattr(browserview, "stop", lambda rd, rid: stopped.append(rid))

    with pytest.raises(RuntimeError):
        list(mgr.review("rr", "o/r#3"))
    assert stopped == ["rr"]                              # finally ran


def test_review_pass_redacts_credentials_from_stream(tmp_path, monkeypatch):
    mgr, store, cfg = _review_mgr(tmp_path)

    class LeakyEnv(FakeEnv):
        def exec_stream(self, argv, service=None, workdir="/work"):
            import json
            yield json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "logging in with s3cret"}]}})
            yield json.dumps({"type": "result", "subtype": "success",
                              "is_error": False, "session_id": "s1",
                              "result": "done", "total_cost_usd": 0.1,
                              "num_turns": 1, "usage": {}})

    mgr.env_factory = lambda rid, files: LeakyEnv(rid, files)
    _seed_review_ws(cfg, "rr", '{"summary":"ok","comments":[]}')
    monkeypatch.setattr(mgr, "_app_url", lambda rid: "http://web:3000")
    monkeypatch.setattr(mgr, "_qa_credentials",
                        lambda rid: [{"role": "admin", "username": "u",
                                      "password": "s3cret"}])

    evs = list(mgr.review("rr", "o/r#3"))
    texts = [e.data.get("text", "") for e in evs if e.kind == "narration"]
    assert any("••••" in t for t in texts)               # redacted
    assert all("s3cret" not in t for t in texts)          # raw secret never leaked


def test_review_pass_resets_stale_artifacts_at_start(tmp_path, monkeypatch):
    mgr, store, cfg = _review_mgr(tmp_path)
    mgr.env_factory = lambda rid, files: FakeEnv(rid, files)
    _seed_review_ws(cfg, "rr", '{"summary":"ok","comments":[]}')
    # a prior turn's capture that must not survive into this review
    arts = cfg.runs_dir / "rr" / "workspace" / ".forge" / "artifacts"
    arts.mkdir(parents=True, exist_ok=True)
    (arts / "after.png").write_bytes(b"stale")
    (arts / "manifest.json").write_text('{"artifacts":[{"path":"after.png"}]}')
    monkeypatch.setattr(mgr, "_app_url", lambda rid: None)

    list(mgr.review("rr", "o/r#3"))
    assert mgr.artifacts("rr") == []                      # reset at review start
```

Add this small helper once near the top of `tests/test_session.py` (below `_drain_capture`), if not already present:

```python
def _prompt_of(argv):
    """The worker prompt element from a captured stream argv (after -p)."""
    return argv[argv.index("-p") + 1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_session.py -k "review_pass" -v`
Expected: FAIL — current `_review_pass` never calls `browserview.start/stop`, passes no credentials (no `s3cret`/`127.0.0.1:9222` in prompt, no redaction), and never resets artifacts.

- [ ] **Step 3: Rewrite `_review_pass`**

In `src/forge/session_review.py`, REPLACE the whole `_review_pass` method with the version below (imports of `browserview` and `redact_secrets` are added locally, mirroring `session.py::_qa`):

```python
    def _review_pass(self, run_id, ref, model):
        from forge import browserview
        from forge.creds import redact_secrets
        env = self._env_for(run_id)
        # Pre-fetch the PR diff HOST-side and drop it into the workspace: the
        # review agent runs on untrusted PR code, so the container gets no
        # GitHub token — it reads .forge/pr.diff instead of running `gh`.
        ws = Path(self.cfg.runs_dir) / run_id / "workspace"
        diff = self.host.run(cmd.pr_diff_cmd(ref.slug, ref.number),
                             env={"GH_TOKEN": self.cfg.gh_token}).stdout
        self.host.write_file(str(ws / ".forge" / "pr.diff"), diff or "")
        # Fresh artifact set for this review; stored creds injected + redacted,
        # exactly like the QA turn (session._qa).
        self._reset_artifacts(run_id)
        creds = self._qa_credentials(run_id)
        secrets = [c.get("password") for c in (creds or []) if c.get("password")]
        app_url = self._app_url(run_id)
        full = build_review_prompt(ref.slug, ref.number, app_url, credentials=creds)
        chosen = self.provider.resolve_model(
            model, "review for correctness bugs and security")
        yield TurnEvent("model", {"choice": model, "resolved": chosen})
        yield TurnEvent("phase", {"name": "agent", "label": "Reviewing"})
        # Live agent-browser view: start the shared CDP Chromium + screencaster
        # only when there is an app to drive (same guard as turn()); stop is
        # always attempted. Both ends best-effort — a browser failure never
        # fails the review.
        if app_url:
            browserview.start(self.cfg.runs_dir, run_id, env)
        try:
            result = yield from self._stream_worker(
                run_id, env, full, chosen,
                redact=lambda s: redact_secrets(s, secrets))
        finally:
            browserview.stop(self.cfg.runs_dir, run_id)
        if result and result.auth_error:
            yield TurnEvent("error", {"kind": "auth",
                                      "detail": result.result_text[:300]})
            return
        # Persist the agent's review narration/result so the web transcript
        # mirrors turn()'s assistant message (cross-surface parity).
        if result:
            self.store.add_message(
                run_id, "assistant", result.result_text or "(review complete)",
                meta={"cost": result.total_cost_usd, "model": chosen})
        posted = self._post_review(run_id, ref)
        msg = (f"Review posted: {posted['review_url']}" if posted.get("ok")
               else f"Review post failed: {posted.get('reason')}")
        self.store.add_message(run_id, "system", msg)
        self.store.touch_env(run_id)
        yield TurnEvent("review", posted)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_session.py -k "review" -v`
Expected: PASS — the six new `review_pass` tests, plus the pre-existing `test_review_persists_user_and_assistant_messages`, `test_review_bad_ref_yields_error`, `test_review_posts_and_validates_inline_comments` (unchanged behaviour: those don't monkeypatch `_app_url`, so `browserview.stop` runs against the real best-effort no-op — which only touches a stop file — and `_qa_credentials` returns `None`).

- [ ] **Step 5: Run the full session suite to confirm no regressions**

Run: `python -m pytest tests/test_session.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/forge/session_review.py tests/test_session.py
git commit -m "feat(review): drive the live app in review — browser + creds + redaction, reset artifacts"
```

---

### Task 5: `slackbot.py` — surface review screenshots in the thread

Slack-originated reviews post the worker's screenshots after the review-posted message, reusing the existing thread-safe, best-effort `_post_artifacts` helper (the same one QA turns use). Web reviews are unaffected. One added call.

**Files:**
- Modify: `src/forge/slackbot.py` (`_run_review` ~L509-547, inside the `if result and result.get("ok"):` success branch)
- Test: `tests/test_slackbot.py`

**Interfaces:**
- Consumes: `self._post_artifacts(run_id, channel, thread_ts)` (existing), `self.manager.artifacts(run_id)` (existing).
- Produces: no new interface — `_run_review` gains one `_post_artifacts` call after the success message.

- [ ] **Step 1: Write a failing test**

Add to `tests/test_slackbot.py` (near `test_review_intent_drives_manager_review_and_posts_url` ~L1078):

```python
def test_review_posts_artifacts_after_review(tmp_path):
    from forge.store import Store

    class RevManager(FakeManager):
        def review(self, run_id, pr, model="auto", origin="api"):
            self.calls.append(("review", run_id, pr))
            yield TE("review", ok=True, review_url="https://gh/o/r/pull/3#x",
                     comments=2, dropped=0, degraded=False)

    client = FakeClient()
    manager = RevManager(artifacts=[_art("after.png", "after", "checkout works")])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "review o/r#3")

    # the review screenshot is uploaded into the review thread
    assert len(client.uploads) == 1
    assert client.uploads[0].title == "checkout works"
    assert client.uploads[0].thread_ts is not None
    # still posts the review URL
    assert any("pull/3" in p.text for p in client.posts)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_slackbot.py -k "review_posts_artifacts" -v`
Expected: FAIL — `_run_review` doesn't call `_post_artifacts`, so `client.uploads == []`.

- [ ] **Step 3: Add the `_post_artifacts` call in `_run_review`**

In `src/forge/slackbot.py`, inside `_run_review`, in the success branch, add the artifact post AFTER the "review posted" message. Change:

```python
        if result and result.get("ok"):
            tag = " _(under your account — set up the Forge GitHub App for the " \
                  "forge[bot] avatar)_" if result.get("degraded") else ""
            self.client.chat_postMessage(
                channel=channel, thread_ts=anchor_ts,
                text=f"📝 review posted{tag}: {result['review_url']} "
                     f"({result['comments']} inline)")
```

to:

```python
        if result and result.get("ok"):
            tag = " _(under your account — set up the Forge GitHub App for the " \
                  "forge[bot] avatar)_" if result.get("degraded") else ""
            self.client.chat_postMessage(
                channel=channel, thread_ts=anchor_ts,
                text=f"📝 review posted{tag}: {result['review_url']} "
                     f"({result['comments']} inline)")
            # Surface any screenshots the review worker captured (best-effort;
            # helper is thread-safe and no-ops when there are none).
            self._post_artifacts(run_id, channel, anchor_ts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_slackbot.py -k "review" -v`
Expected: PASS — the new artifact test AND `test_review_intent_drives_manager_review_and_posts_url` (its `FakeManager` has no artifacts, so `_post_artifacts` no-ops and `uploads` stays empty).

- [ ] **Step 5: Commit**

```bash
git add src/forge/slackbot.py tests/test_slackbot.py
git commit -m "feat(review): post review screenshots to the Slack thread"
```

---

### Task 6: Full suite + spec self-check

**Files:** none (verification task).

- [ ] **Step 1: Run the complete test suite**

Run: `python -m pytest -q`
Expected: PASS (all ~1150+ tests). Investigate any failure before proceeding — do not claim completion on red.

- [ ] **Step 2: Confirm spec coverage (manual checklist)**

Verify each spec item maps to a task:
- Inline-first schema + `live_check` in prompt → Task 3. ✅
- Credentials + guardrail + `skipped`/`blocked` semantics in prompt → Task 3. ✅
- Session wiring (creds, redaction, browserview bracket, artifact reset) → Task 4. ✅
- Snap anchoring in `partition` → Task 2. ✅
- Posted body: degrade header → live-check → summary → dropped-notes; emoji per status; omitted when absent → Task 1. ✅
- Slack review screenshots → Task 5. ✅
- Advisory framing / `event=COMMENT` unchanged → preserved (Task 1 `build_payload` untouched on `event`). ✅
- Self-review untouched / parser tolerant of missing `live_check` → Task 1 (Steps 1, 6). ✅
- Failure modes best-effort → Tasks 4/5 (browser guarded + `finally`, `_post_artifacts` best-effort). ✅

- [ ] **Step 3: Report completion with evidence**

Paste the `pytest -q` summary line. State plainly that all tasks are implemented and tests are green.

---

## Self-Review (author checklist — completed at plan-writing time)

**1. Spec coverage:** every spec section (1 prompt/schema, 2 session wiring, 3 anchoring, 4 posted shape, 5 failure modes, 6 testing) maps to Tasks 1–5, verified in Task 6 Step 2. Out-of-scope items (PR-embedded screenshots, REQUEST_CHANGES/APPROVE, interactive credential checkpoints, formal acceptance criteria) are deliberately NOT implemented.

**2. Placeholder scan:** no TBD/TODO/"add error handling"/"similar to Task N" — every code and test step contains the literal content.

**3. Type consistency:** `Review.live_check` (3rd field, default `None`) is produced in Task 1 and consumed by `build_payload`/`_live_check_section` (Task 1) and by `_post_review` (unchanged, Task 4). `build_review_prompt(slug, number, app_url, credentials=None)` is defined in Task 3 and called with `credentials=creds` in Task 4. `_cred_block(credentials)` defined and used in Task 3 (QA + review). `_snap`/`partition` (Task 2) return `Comment`s consumed by `build_payload` (Task 1). `browserview.start(runs_dir, run_id, env, service="forge")` / `stop(runs_dir, run_id)` signatures match `src/forge/browserview.py`. `_prompt_of`/`_seed_review_ws` test helpers defined once in Task 4. Names are consistent across tasks.
