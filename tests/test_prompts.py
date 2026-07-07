from forge.prompts import build_task_prompt, build_fix_prompt, attachments_block


def test_task_prompt_contains_task_and_role():
    p = build_task_prompt("Add a /health endpoint")
    assert "Add a /health endpoint" in p
    assert "verification" in p.lower()
    assert "autonomous" in p.lower() or "do not ask" in p.lower()


def test_fix_prompt_lists_failures():
    p = build_fix_prompt([("test", "1 failing: expected 5 got -1")])
    assert "test" in p
    assert "expected 5 got -1" in p


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


def test_build_self_review_prompt_reviews_and_fixes():
    from forge.prompts import build_self_review_prompt
    p = build_self_review_prompt()
    assert "review" in p.lower() and "fix" in p.lower()
    assert ".forge/review.json" in p


def test_task_prompt_includes_app_url():
    p = build_task_prompt("fix X", "http://localhost:3000")
    assert "http://localhost:3000" in p
    assert "reproduce" in p.lower()


def test_task_prompt_without_app_url_unchanged():
    assert "localhost" not in build_task_prompt("fix X")


def test_task_prompt_includes_capture_protocol_when_app_url():
    p = build_task_prompt("fix the footer link", "http://localhost:3000")
    assert ".forge/artifacts" in p
    assert "manifest.json" in p
    # mentions the before/after + video intent so the agent knows what to do
    assert "before" in p.lower() and "video" in p.lower()


def test_task_prompt_no_capture_protocol_without_app_url():
    # No running app → nothing to screenshot.
    assert ".forge/artifacts" not in build_task_prompt("refactor internals")


def test_task_prompt_tells_agent_to_run_repo_checks():
    # The agent should self-check, not lean entirely on forge's gate.
    p = build_task_prompt("add a field").lower()
    assert "lint" in p or "type" in p or "build" in p
    assert "fix" in p


def test_task_prompt_pr_description_is_concise():
    # PR-body guidance: short Summary + one-line Testing note; no mandatory
    # per-file Changes section and no ## Testing section.
    p = build_task_prompt("fix the offer price column")
    assert ".forge/pr.json" in p
    assert "## Summary" in p
    assert "1-2 sentences" in p
    assert "**Testing:**" in p
    assert "## Changes" not in p
    assert "## Testing" not in p


def test_role_drops_dont_ask_questions():
    from forge.prompts import _ROLE
    assert "do not ask questions" not in _ROLE.lower()


def test_plan_prompt_requests_plan_json_and_includes_task():
    from forge.prompts import build_plan_prompt
    p = build_plan_prompt("Add a logout button", app_url="http://web:3000",
                          lessons=("prefer pnpm",))
    assert ".forge/plan.json" in p
    assert "Add a logout button" in p
    assert "open_questions" in p
    assert "prefer pnpm" in p
    assert "http://web:3000" in p


def test_replan_prompt_includes_amendment_and_prior():
    from forge.prompts import build_replan_prompt
    p = build_replan_prompt('{"goal":"x"}', "also handle the logged-out case")
    assert "also handle the logged-out case" in p
    assert ".forge/plan.json" in p


def test_qa_prompt_lists_criteria_and_url_and_schema():
    from forge.prompts import build_qa_prompt
    p = build_qa_prompt(["user can log in", "logout works"], "http://web:3000")
    assert "http://web:3000" in p
    assert "user can log in" in p and "logout works" in p
    assert ".forge/qa.json" in p
    assert "passed" in p          # the result schema field


def test_qa_schema_documents_unverifiable_flag():
    # Criteria that can only be observed post-merge (external dashboards, real
    # CI/PR runs) must be recordable as unverifiable, not forced into pass/fail.
    from forge.prompts import build_qa_prompt
    p = build_qa_prompt(["c1"], "http://app")
    assert '"unverifiable": true' in p
    assert "after merge" in p


def test_plan_prompt_requires_sandbox_verifiable_acceptance():
    # The planner must not emit acceptance criteria that QA can never verify
    # from the sandbox (they jam the QA repair loop with unfixable failures).
    from forge.prompts import build_plan_prompt
    p = build_plan_prompt("add logout")
    assert "running app" in p
    assert "after merge" in p


def test_qa_fix_prompt_includes_failed_criteria_and_url():
    from forge.prompts import build_qa_fix_prompt
    p = build_qa_fix_prompt(["logout works"], "http://web:3000")
    assert "logout works" in p and "http://web:3000" in p


def test_retrospective_prompt_schema_and_existing():
    from forge.prompts import build_retrospective_prompt
    p = build_retrospective_prompt(["use pnpm"])
    assert ".forge/lessons.json" in p
    assert "do not repeat" in p.lower()
    assert "use pnpm" in p                  # existing lessons listed
    assert '"kind"' in p                    # schema field


def test_retrospective_prompt_no_existing():
    from forge.prompts import build_retrospective_prompt
    p = build_retrospective_prompt()
    assert ".forge/lessons.json" in p


def test_qa_prompt_has_anti_bruteforce_guardrail_always():
    from forge.prompts import build_qa_prompt
    p = build_qa_prompt(["c1"], "http://app")
    assert "do not" in p.lower() and "brute" in p.lower()
    assert "needs_credentials" in p
    assert "CREDENTIALS (use" not in p     # none supplied -> no creds block rendered


def test_qa_prompt_renders_credentials_block_with_roles():
    from forge.prompts import build_qa_prompt
    creds = [{"role": "admin", "username": "a@b.c", "password": "pw"}]
    p = build_qa_prompt(["c1"], "http://app", credentials=creds)
    assert "CREDENTIALS" in p
    assert "role=admin" in p and "a@b.c" in p and "pw" in p


def test_qa_prompt_treats_expected_role_denial_as_correct_behavior():
    # A customer account bouncing off /admin is access control working —
    # QA must not report it as a defect/"blocker" (it did, once).
    from forge.prompts import build_qa_prompt
    p = build_qa_prompt(["c1"], "http://app")
    assert "ROLE EXPECTATIONS" in p
    assert "correct access control" in p
    assert "'blocker'" in p


def test_qa_prompt_includes_repo_lessons():
    from forge.prompts import build_qa_prompt
    p = build_qa_prompt(["c1"], "http://app",
                        lessons=["admin login is edvin@example.com"])
    assert "LESSONS FROM PRIOR RUNS" in p
    assert "admin login is edvin@example.com" in p
    assert "LESSONS" not in build_qa_prompt(["c1"], "http://app")


# --- ENVIRONMENT block: operational runtime facts for the agent --------------

_FACTS = {
    "stack": "next-supabase",
    "app": "http://web:3000",
    "endpoints": [["Supabase", "http://host.docker.internal:54321"]],
    "pkg_manager": "bun",
    "dev_cmd": "bun run dev",
    "test_cmds": ["bun run test", "bun run test:e2e"],
}


def test_render_env_block_full_has_facts_and_note():
    from forge.prompts import render_env_block
    b = render_env_block(_FACTS)
    assert "ENVIRONMENT" in b
    assert "http://web:3000" in b
    assert "Supabase: http://host.docker.internal:54321" in b
    assert "bun run dev" in b
    assert "bun run test" in b
    assert "do not start duplicate" in b.lower()
    assert "localhost" in b.lower()          # steer off assuming localhost


def test_render_env_block_empty_when_no_facts():
    from forge.prompts import render_env_block
    assert render_env_block(None) == ""
    assert render_env_block({}) == ""


def test_render_env_block_brief_lists_service_labels_only():
    from forge.prompts import render_env_block
    b = render_env_block(_FACTS, brief=True)
    assert "Supabase" in b                    # service named
    assert "http://host.docker.internal" not in b   # no raw URLs in brief mode
    assert "bun run test" not in b            # no commands in brief mode


def test_task_prompt_embeds_environment_block():
    p = build_task_prompt("fix X", "http://web:3000", env=_FACTS)
    assert "ENVIRONMENT" in p
    assert "bun run dev" in p


def test_task_prompt_without_env_has_no_environment_block():
    p = build_task_prompt("fix X", "http://web:3000")
    assert "ENVIRONMENT" not in p


def test_plan_prompt_embeds_brief_environment_block():
    from forge.prompts import build_plan_prompt
    p = build_plan_prompt("add logout", app_url="http://web:3000", env=_FACTS)
    assert "Supabase" in p
    assert "bun run dev" not in p             # planner gets the brief version


def test_task_prompt_carries_repo_lessons():
    # Knowledge that only the planner sees is knowledge the hands never use —
    # the executor prompt must carry the repo's lessons too.
    from forge.prompts import build_task_prompt
    p = build_task_prompt("fix it", lessons=["use bun, not npm"])
    assert "LESSONS FROM PRIOR RUNS" in p
    assert "- use bun, not npm" in p
    assert "LESSONS" not in build_task_prompt("fix it")


def test_plan_prompt_reserves_open_questions_for_human_blockers():
    # The planner must not bounce answerable questions back to the human —
    # open_questions is for blockers only a human can resolve; everything
    # else is a decision recorded under assumptions.
    from forge.prompts import build_plan_prompt
    p = build_plan_prompt("Add a logout button")
    low = p.lower()
    assert "open_questions" in p and "assumptions" in p
    assert "human" in low
    assert "answer yourself" in low


def test_capture_and_qa_prompts_note_preinstalled_browser():
    from forge.prompts import build_task_prompt, build_qa_prompt
    t = build_task_prompt("fix the header", app_url="http://web:3000")
    q = build_qa_prompt(["header renders"], "http://web:3000")
    for p in (t, q):
        low = p.lower()
        assert "preinstalled" in low and "playwright" in low
    assert "do not download" in t.lower()


def test_qa_prompt_directs_agent_to_shared_cdp_browser():
    # The live agent-browser view only streams what happens in the SHARED
    # Chromium browserview starts — the QA prompt must send the agent there,
    # with a fallback so QA still runs when the screencaster failed.
    from forge.prompts import build_qa_prompt
    q = build_qa_prompt(["header renders"], "http://web:3000")
    assert "connectOverCDP" in q
    assert "127.0.0.1:9222" in q
    assert "fall back" in q.lower()


def test_task_prompt_directs_agent_to_shared_cdp_browser():
    # The executor's browser work (reproducing the bug, checking the fix,
    # capturing artifacts) must happen in the same shared Chromium — a
    # self-launched browser works but streams nothing to the workspace.
    t = build_task_prompt("fix the header", app_url="http://web:3000")
    assert "connectOverCDP" in t
    assert "127.0.0.1:9222" in t
    assert "fall back" in t.lower()


def test_task_prompt_without_app_url_has_no_shared_browser():
    # No live app → no shared browser was started; don't send the agent to a
    # CDP endpoint that doesn't exist.
    t = build_task_prompt("fix the header")
    assert "connectOverCDP" not in t


def test_qa_fix_prompt_directs_agent_to_shared_cdp_browser():
    # QA-fix turns re-exercise the app while fixing; that browsing should be
    # watchable too.
    from forge.prompts import build_qa_fix_prompt
    p = build_qa_fix_prompt(["logout works"], "http://web:3000")
    assert "connectOverCDP" in p


def test_attachments_block_lists_paths():
    b = attachments_block(["/work/.forge/inbox/1-a.png"])
    assert "/work/.forge/inbox/1-a.png" in b
    assert "Read" in b            # instructs viewing with the Read tool


def test_attachments_block_empty_is_empty():
    assert attachments_block([]) == "" and attachments_block(None) == ""


def test_task_prompt_includes_attachments():
    p = build_task_prompt("fix the header", attachments=["/work/.forge/inbox/1-a.png"])
    assert "/work/.forge/inbox/1-a.png" in p


def test_plan_prompt_includes_attachments():
    from forge.prompts import build_plan_prompt
    p = build_plan_prompt("fix the header", attachments=["/work/.forge/inbox/1-a.png"])
    assert "/work/.forge/inbox/1-a.png" in p


def test_task_prompt_nudges_url_opening():
    p = build_task_prompt("match https://example.com/design")
    assert "https://example.com/design" in p and "open" in p.lower()


def test_task_prompt_no_url_no_nudge():
    assert "URL" not in build_task_prompt("fix the header")
