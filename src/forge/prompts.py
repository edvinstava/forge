_ROLE = (
    "You are an autonomous coding worker inside an isolated container. "
    "Make the change described below with a correct, minimal implementation. "
    "Before you finish, hold your work to the standard the repository's CI "
    "enforces: run the project's own checks — type-check, lint, formatter and "
    "build (the scripts the repo defines, e.g. `typecheck`/`ts:check`, "
    "`lint`, `format`, `build`) — and fix anything they flag, including "
    "formatting. Forge runs the same verification suite afterwards and will "
    "not open a clean (non-draft) pull request until it passes, so green "
    "checks are part of the task, not optional. Once your plan is approved, "
    "work autonomously: do not pause for confirmation mid-execution, and follow "
    "the repository's existing conventions. If something is genuinely ambiguous, "
    "raise it as an open question in the plan rather than guessing."
)


# The worker image bakes Playwright + Chromium in (see worker-image/Dockerfile,
# PLAYWRIGHT_BROWSERS_PATH). Saying so saves every browser-using turn the
# install-or-hunt dance the agent otherwise starts with.
_BROWSER = (
    "Playwright and its Chromium browser are preinstalled in this container "
    "(`playwright` on PATH, browsers at `$PLAYWRIGHT_BROWSERS_PATH`) — launch "
    "it directly; do not download browsers, run `playwright install`, or "
    "search the filesystem for one."
)


# Appended only when a live app URL exists — capture is pointless without a
# running app. The agent owns the judgement (is this even visual?) and the
# browser; forge just collects whatever lands in .forge/artifacts/. Strictly
# best-effort: capture must never fail the task or leave the tree dirty.
_CAPTURE = (
    "\n\nVISUAL ARTIFACTS (best-effort, do this LAST, after the change and your "
    "own verification):\n"
    f"- {_BROWSER}\n"
    "- If the result is visually observable in the running app, capture it with "
    "your browser and save files under `.forge/artifacts/` (create it). If the "
    "change is backend-only / not visible, skip this section entirely.\n"
    "- Bug fix: reproduce the broken state first — revert your change with "
    "`git stash`, let the dev server reload, screenshot/record it (name it "
    "`before.png`), then `git stash pop` to restore, and capture the fixed state "
    "(`after.png`). Optionally record a short repro→fix `video` (mp4/webm).\n"
    "- New feature/page: capture the result (`after.png`) and optionally a short "
    "navigation `video`.\n"
    "- Describe what you saved in `.forge/artifacts/manifest.json`: "
    '`{"artifacts": [{"path": "before.png", "kind": "before", "caption": "..."}, '
    '{"path": "after.png", "kind": "after", "caption": "..."}]}` '
    "(kind is before|after|video).\n"
    "- Limits: PNG screenshots; video ≤ 15s and ≤ 8MB; ≤ 6 files total.\n"
    "- This is best-effort: if the browser or capture fails, restore your working "
    "tree (`git stash pop` if you stashed) and finish the task anyway. Never let "
    "capture block or fail the change.\n"
)


def render_env_block(facts: dict | None, brief: bool = False) -> str:
    """Render the resolved runtime facts (see Recipe.runtime_facts) as a compact
    ENVIRONMENT block. Operational context, not documentation: reachable
    endpoints and canonical commands only. `brief` (for the planner) lists just
    the available service names; the full form (for the executor/QA) also gives
    URLs and commands. Empty string when there are no useful facts."""
    if not facts:
        return ""
    app = facts.get("app")
    endpoints = facts.get("endpoints") or []
    if brief:
        labels = (["app"] if app else []) + [e[0] for e in endpoints]
        if not labels:
            return ""
        return ("\n\nRUNTIME SERVICES AVAILABLE (already running — use them when "
                "planning tests/debugging): " + ", ".join(labels) + ".")
    lines = []
    if app:
        lines.append(f"- App: {app}")
    for label, url in endpoints:
        lines.append(f"- {label}: {url}")
    if facts.get("pkg_manager"):
        lines.append(f"- Package manager: {facts['pkg_manager']}")
    if facts.get("dev_cmd"):
        lines.append(f"- Dev command: {facts['dev_cmd']}")
    if facts.get("test_cmds"):
        lines.append("- Tests: " + " / ".join(facts["test_cmds"]))
    if not lines:
        return ""
    lines.append("- Note: these services are already running; reach them at the "
                 "URLs above (container DNS, not localhost) and do not start "
                 "duplicate servers.")
    return ("\n\nENVIRONMENT (operational facts, not documentation):\n"
            + "\n".join(lines))


# The agent that made the change writes the PR description — it knows what
# changed and why. session._finish_pr falls back to a task-derived title when
# this file is missing, but the agent-authored version is what makes forge's
# PRs read like a colleague's.
_PR_META = (
    "\n\nPULL REQUEST DESCRIPTION (required): when the change is complete, write "
    "`.forge/pr.json` (create `.forge/`) as strict JSON:\n"
    '{"title": "<imperative and specific, ≤72 chars; include the issue key from '
    'the task (e.g. ABC-123) if there is one>",\n'
    ' "body": "<markdown: ## Summary (what & why, 2-4 sentences) · ## Changes '
    "(short bullets per file/area) · ## Testing (checks you ran, what you "
    'verified)>"}\n'
    "Write it for the reviewer — concise and concrete. Never mention forge, "
    "sessions, or run ids; do not include the diff itself."
)


def lessons_block(lessons) -> str:
    """Durable per-repo lessons (retrospectives + user-taught) rendered for a
    prompt. Shared by the planner AND the executor — knowledge that only the
    planner sees is knowledge the hands never use."""
    if not lessons:
        return ""
    return ("\n\nLESSONS FROM PRIOR RUNS ON THIS REPO (apply them):\n"
            + "\n".join(f"- {l}" for l in lessons))


def attachments_block(paths) -> str:
    """User-supplied images (synced to /work/.forge/inbox/ by forge.inbox),
    rendered for a prompt. Paths only — never inline data."""
    if not paths:
        return ""
    return ("\n\nATTACHED IMAGES (sent by the user with this message — view "
            "each with the Read tool BEFORE starting; they show the bug, "
            "design, or context the task refers to):\n"
            + "\n".join(f"- {p}" for p in paths))


_URL_NUDGE = ("\n\nThe task references URL(s): open them (WebFetch or the "
              "browser) to see what they show before deciding anything.")


def _url_nudge(task: str) -> str:
    return _URL_NUDGE if ("http://" in task or "https://" in task) else ""


def build_task_prompt(task: str, app_url: str | None = None,
                      env: dict | None = None, lessons=(), attachments=()) -> str:
    live = ""
    if app_url:
        live = (
            f"\n\nA live instance of this app is running at {app_url}. "
            "Before changing code, reproduce the reported problem against it; "
            "after your fix, confirm the symptom is gone.\n"
            + _CAPTURE
        )
    return (f"{_ROLE}\n\nTASK:\n{task}\n{live}{render_env_block(env)}"
            f"{lessons_block(lessons)}{attachments_block(attachments)}"
            f"{_url_nudge(task)}{_PR_META}")


def build_fix_prompt(failures) -> str:
    blocks = "\n\n".join(
        f"### {name} failed:\n{output}" for name, output in failures
    )
    return (
        "The verification suite is still failing after your last change. "
        "Fix the cause of these failures (do not modify the tests unless the "
        "task says to). Here is the latest output:\n\n" + blocks + "\n"
    )


_REVIEW_SCHEMA = (
    "Write your findings to `.forge/review.json` (create the `.forge/` dir) as "
    "strict JSON:\n"
    '{"summary": "<markdown overview>", "comments": [\n'
    '  {"path": "<repo-relative path>", "line": <int>, "side": "RIGHT", '
    '"body": "<one finding>"}\n'
    "]}\n"
    "- `line` MUST be a line that appears in THIS PR's diff (an added/context "
    "line on side RIGHT, or a removed/context line on side LEFT). Do not comment "
    "on lines outside the diff — put any such observation in `summary` instead.\n"
    "- High-signal only: correctness/security bugs first, then "
    "reuse/simplification/efficiency. Skip nits and praise.\n"
)


def build_review_prompt(slug: str, number: int, app_url: str | None) -> str:
    live = ""
    if app_url:
        live = (f"\nA live instance of this PR's app is running at {app_url}; "
                "exercise it where it helps you judge the change.\n")
    return (
        "You are a meticulous senior code reviewer. Review the pull request "
        f"{slug}#{number} in this checked-out repository. The full PR diff is "
        "saved at `.forge/pr.diff` — read it to see exactly what changed, and "
        "read the surrounding code for context. You are advisory only: report "
        "findings, do not approve or block, and do not modify the code.\n"
        f"{live}\n{_REVIEW_SCHEMA}"
    )


def build_self_review_prompt() -> str:
    return (
        "Before this work becomes a pull request, critically review your own "
        "uncommitted changes (`git diff HEAD`) against the task. Find "
        "correctness bugs, regressions, missed edge cases, and obvious quality "
        "problems — then FIX them directly in the working tree. Keep fixes "
        "minimal and follow existing conventions; do not modify tests unless the "
        "task requires it. After fixing, record what you addressed in "
        '`.forge/review.json` as {"summary": "...", "comments": [{"path": "...", '
        '"line": <int>, "side": "RIGHT", "body": "<issue you fixed>"}]} so forge '
        "can report it. This is advisory and best-effort: never leave the tree "
        "broken."
    )


_PLAN_SCHEMA = (
    "Inspect the repository, then write your plan to `.forge/plan.json` (create "
    "the `.forge/` dir). Do NOT modify any other files yet. Strict JSON:\n"
    '{"goal": "<one-line restatement>",\n'
    ' "steps": [{"id": 1, "intent": "<what>", "files": ["<repo-relative path>"]}],\n'
    ' "acceptance": ["<observable success criterion>"],\n'
    ' "assumptions": ["<assumption you are making>"],\n'
    ' "open_questions": ["<blocker you cannot resolve from the repo>"],\n'
    ' "risk": "low|medium|high"}\n'
    "- Keep it concise and concrete. `acceptance` items must be observable.\n"
    "- `open_questions` is ONLY for blockers a human must resolve: missing "
    "credentials/secrets, contradictory requirements, or an irreversible/"
    "destructive choice. Anything you could answer yourself — by reading the "
    "repo, or by making a reasonable, reversible engineering call — you MUST "
    "decide, and record the decision under `assumptions` instead of asking. "
    "Every open question stalls a teammate; an empty list is the normal case.\n"
)


def build_plan_prompt(task, app_url=None, lessons=(), env=None, attachments=()):
    live = f"\n\nA live instance of this app is running at {app_url}." if app_url else ""
    return ("You are planning a change, not yet implementing it.\n\nTASK:\n"
            f"{task}{live}{lessons_block(lessons)}"
            f"{attachments_block(attachments)}{_url_nudge(task)}"
            f"{render_env_block(env, brief=True)}\n\n{_PLAN_SCHEMA}")


def build_replan_prompt(prior_plan_json, amendment):
    return ("Revise your plan based on the human's feedback below, then OVERWRITE "
            "`.forge/plan.json` with the updated strict JSON (same schema). Do not "
            "modify any other files yet.\n\nYOUR PRIOR PLAN:\n" + prior_plan_json
            + "\n\nHUMAN FEEDBACK:\n" + amendment + "\n\n" + _PLAN_SCHEMA)


_QA_SCHEMA = (
    "Write your results to `.forge/qa.json` (create the `.forge/` dir) as strict "
    "JSON:\n"
    '{"acceptance": [{"criterion": "<verbatim criterion>", "passed": true, '
    '"evidence": "<what you observed>"}], "summary": "<one line>", '
    '"blocked": null}\n'
    "- One entry per acceptance criterion below, `criterion` copied verbatim.\n"
    "- `passed` is true ONLY if you observed the criterion satisfied in the "
    "running app; otherwise false with `evidence` describing what went wrong.\n"
    '- If you cannot proceed without a human (e.g. a login wall and no working '
    'credentials), set `blocked` to '
    '{"kind": "needs_credentials", "question": "<one line naming exactly what '
    'you need — which account/role>"} and stop. Otherwise leave `blocked` null.\n'
)


def build_qa_prompt(acceptance, app_url, credentials=None):
    crits = "\n".join(f"- {c}" for c in acceptance)
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
    guardrail = (
        "\n\nIMPORTANT: You are given NO credentials unless listed under "
        "CREDENTIALS above. If you reach a login/authentication screen and have "
        "no working credentials, DO NOT guess, brute-force, or try common "
        "passwords. Stop and record "
        '`"blocked": {"kind": "needs_credentials", "question": "…"}` in '
        "`.forge/qa.json` naming exactly which account/role you need. Apply the "
        "same rule to any other human-only blocker (paywall, 2FA, external "
        "secret): set `blocked` and stop rather than working around it.")
    return (
        "You are QA-testing a change in a real browser — do NOT modify code in "
        f"this turn. A live instance is running at {app_url}. Open it in your "
        "browser (Playwright) and verify each acceptance criterion below by "
        f"actually exercising the UI. {_BROWSER} Capture evidence as you go: a PNG "
        "screenshot under `.forge/artifacts/` showing the key criteria "
        "satisfied (and one for any criterion you mark failed), recorded in "
        "`.forge/artifacts/manifest.json` as "
        '{"artifacts": [{"path": "after.png", "kind": "after", '
        '"caption": "<what it shows>"}]} — append if the file exists, ≤6 files '
        "total. These screenshots are shown to the teammate who asked for the "
        "change, so frame the relevant part of the UI."
        + cred_block + guardrail
        + "\n\nACCEPTANCE CRITERIA:\n" + crits + "\n\n" + _QA_SCHEMA)


def build_qa_fix_prompt(failed, app_url):
    crits = "\n".join(f"- {c}" for c in failed)
    return (
        "Browser QA found these acceptance criteria still failing in the running "
        f"app at {app_url}. Fix the app so each passes (do not weaken the "
        "criteria). Keep the repo's CI checks green.\n\nFAILING CRITERIA:\n" + crits
        + "\n")


_LESSONS_SCHEMA = (
    "Write durable lessons to `.forge/lessons.json` (create the `.forge/` dir) as "
    "strict JSON:\n"
    '{"lessons": [{"text": "<short, actionable, repo-specific fact>", '
    '"kind": "env|build|test|convention|gotcha", "evidence": "<why you know it>"}]}\n'
    "- High-signal only: things that would make the NEXT run on THIS repo faster "
    "(env quirks, build/test gotchas, conventions that tripped you up). Keep each "
    "`text` one sentence. Empty list if nothing durable was learned.\n"
)


def build_retrospective_prompt(existing=()):
    known = ""
    if existing:
        known = ("\n\nLessons already known for this repo — do NOT repeat these:\n"
                 + "\n".join(f"- {l}" for l in existing))
    return (
        "You just finished a task on this repository. Reflect on what you learned "
        "that would help a FUTURE run on the SAME repo — durable, repo-specific "
        "facts only. Do NOT modify any code in this turn." + known + "\n\n"
        + _LESSONS_SCHEMA)
