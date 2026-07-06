import pytest
from forge.slackmsg import classify_intent


@pytest.mark.parametrize("text,expected", [
    ("create a sleep timer page", "build"),     # change-verb beats "sleep"
    ("can you add a contact form?", "build"),   # change-verb beats trailing ?
    ("fix the landing page", "build"),
    ("you can sleep now", "sleep"),
    ("sleep", "sleep"),
    ("go to sleep", "sleep"),
    ("wake up", "wake"),
    ("resume", "wake"),
    ("status", "status"),
    ("how's it going?", "status"),
    # Identity/meta questions about forge are now conversational (was "help").
    ("how does forge work?", "chat"),
    ("what are you?", "chat"),
    ("help", "chat"),
    ("what version are we on?", "qa"),
    ("what does this app do", "qa"),
    ("introduce yourself and tell me what you can do", "chat"),
    ("introduce yourself", "chat"),
    ("tell me what you can do", "chat"),
    # Chat is the safe default: greetings / small talk / unrecognized, non-verb,
    # non-question messages converse instead of falling into repo resolution.
    ("the landing thing", "chat"),
    ("hey forge", "chat"),
    ("hello!", "chat"),
    ("thanks!", "chat"),
    ("hmm, not sure", "chat"),
])
def test_classify_intent(text, expected):
    assert classify_intent(text) == expected


from forge.slackmsg import strip_mentions


def test_strip_mentions_removes_user_tokens():
    assert strip_mentions("<@U08BOT> introduce yourself") == "introduce yourself"
    assert strip_mentions("hey <@U1> and <@U2> fix it") == "hey and fix it"
    assert strip_mentions("<@U08BOT|forge> fix it") == "fix it"   # labelled form
    assert strip_mentions("no mentions here") == "no mentions here"
    assert strip_mentions("") == ""


from forge.slackmsg import clean_summary, greeting_head, help_blurb


def test_clean_summary_preserves_bullets_and_newlines():
    txt = "TypeScript passes cleanly. Here's what was created:\n- a page\n- a component"
    out = clean_summary(txt)
    assert out == txt                       # structure preserved verbatim


def test_clean_summary_collapses_extra_blank_lines():
    assert clean_summary("a\n\n\n\nb") == "a\n\nb"


def test_clean_summary_caps_long_text_on_line_boundary():
    out = clean_summary("line one\n" + ("x" * 5000), limit=20)
    assert out.endswith("…") and len(out) <= 25


def test_clean_summary_empty_defaults():
    assert clean_summary("") == "Done."
    assert clean_summary(None) == "Done."


def test_greeting_head_names_slug_and_has_no_quote_echo():
    h = greeting_head("acme/webapp")
    assert "acme/webapp" in h and '"' not in h


def test_help_blurb_is_short_and_mentions_forge():
    b = help_blurb()
    assert "forge" in b.lower() and b.count("\n") <= 2


def test_help_blurb_covers_both_surfaces():
    b = help_blurb()
    assert "@forge" in b          # channel usage
    assert "dm" in b.lower()      # DM usage


from forge.slackmsg import opener_prompt, qa_head


def test_opener_prompt_carries_task_and_slug():
    p = opener_prompt("add a hello page", "acme/webapp", "build")
    assert "add a hello page" in p
    assert "acme/webapp" in p


def test_opener_prompt_asks_for_a_single_short_line():
    p = opener_prompt("add a hello page", "acme/x", "build")
    assert "one" in p.lower() and "line" in p.lower()


def test_opener_prompt_build_and_qa_differ():
    build = opener_prompt("q", "acme/x", "build")
    qa = opener_prompt("q", "acme/x", "qa")
    assert build != qa


def test_qa_head_names_slug():
    h = qa_head("acme/webapp")
    assert "acme/webapp" in h


from forge.slackmsg import deep_link

BASE = "https://x.trycloudflare.com"


def _new(path):
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n+x\n"


def test_deep_link_app_router_page():
    assert deep_link(BASE, _new("app/devotta/page.tsx")) == BASE + "/devotta"


def test_deep_link_app_router_strips_route_group():
    assert deep_link(BASE, _new("src/app/(marketing)/devotta/page.tsx")) == BASE + "/devotta"


def test_deep_link_app_router_root():
    assert deep_link(BASE, _new("app/page.tsx")) == BASE


def test_deep_link_pages_router_index():
    assert deep_link(BASE, _new("pages/devotta/index.tsx")) == BASE + "/devotta"


def test_deep_link_pages_router_file():
    assert deep_link(BASE, _new("pages/devotta.tsx")) == BASE + "/devotta"


def test_deep_link_no_new_page_falls_back_to_base():
    assert deep_link(BASE, _new("src/lib/util.ts")) == BASE


def test_deep_link_dynamic_only_falls_back():
    assert deep_link(BASE, _new("app/users/[id]/page.tsx")) == BASE


def test_deep_link_prefers_static_and_shortest():
    diff = _new("app/a/b/page.tsx") + _new("app/devotta/page.tsx")
    assert deep_link(BASE, diff) == BASE + "/devotta"


def test_web_workspace_link_builds_live_hash():
    from forge.slackmsg import web_workspace_link
    assert web_workspace_link("https://forge.example.com", "run-1") \
        == "https://forge.example.com/#live=run-1"


def test_web_workspace_link_empty_base_is_blank():
    from forge.slackmsg import web_workspace_link
    assert web_workspace_link("", "run-1") == ""


from forge.slackmsg import concise_verify_reason


def test_concise_verify_reason_prefers_error_line():
    out = "Compiling…\nlint: 'page.tsx' 2 problems\nError: missing libnss3\n"
    assert concise_verify_reason(out) == "Error: missing libnss3"


def test_concise_verify_reason_falls_back_to_last_line():
    assert concise_verify_reason("step one\nstep two done") == "step two done"


def test_concise_verify_reason_empty():
    assert concise_verify_reason("") == "no output captured"


def test_concise_verify_reason_truncates():
    assert len(concise_verify_reason("x" * 500)) <= 140


def test_classify_intent_review_with_pr_ref():
    from forge.slackmsg import classify_intent
    assert classify_intent("review dhis2/forge#12") == "review"
    assert classify_intent("can you review https://github.com/o/r/pull/9") == "review"
    assert classify_intent("o/r#3") == "review"


def test_classify_intent_plain_build_unaffected():
    from forge.slackmsg import classify_intent
    assert classify_intent("add a logout button") == "build"


def test_classify_intent_build_verb_with_pr_ref_is_build():
    from forge.slackmsg import classify_intent
    # Referencing the relevant issue/PR in a change request is normal; the build
    # verb must win over the embedded ref so the user gets the build they asked
    # for, not a PR review of that ref.
    assert classify_intent("fix the crash in acme/web#5") == "build"
    assert classify_intent("add login to acme/web#5 please") == "build"
    # A bare ref with no build verb still routes to review.
    assert classify_intent("acme/web#5") == "review"
    assert classify_intent("review acme/web#5") == "review"


from forge.slackmsg import chat_prompt, format_transcript, route_prompt


def test_route_prompt_asks_for_json_actions():
    p = route_prompt("", "lag en about-side")
    assert "lag en about-side" in p             # the message to decide on
    low = p.lower()
    assert "json" in low                        # strict structured output
    assert "build" in low and "chat" in low     # the action vocabulary


def test_route_prompt_carries_transcript():
    p = route_prompt("User: hi\nforge: hey", "fiks knappen")
    assert "User: hi" in p
    assert "fiks knappen" in p


def test_route_prompt_flags_non_english_builds():
    # The whole point: a Norwegian build verb must be recognized as a build, so
    # the prompt must tell the model that builds come in any language.
    low = route_prompt("", "lag en side").lower()
    assert "lag" in low or "norwegian" in low or "language" in low


def test_chat_prompt_contains_persona_transcript_and_latest():
    p = chat_prompt("User: hi\nforge: hey", "what can you do?")
    assert "forge" in p.lower()
    assert "what can you do?" in p          # the message to reply to
    assert "User: hi" in p                  # prior context carried in


def test_chat_prompt_grounds_real_capabilities():
    low = chat_prompt("", "hello").lower()
    # It must describe what forge actually does so it doesn't invent features.
    assert "sandbox" in low
    assert "pr" in low
    assert "sleep" in low and "status" in low


def test_chat_prompt_handles_empty_transcript():
    p = chat_prompt("", "hi")
    assert "hi" in p                        # still has a message to answer


def test_format_transcript_maps_user_and_bot_roles():
    msgs = [{"user": "U1", "text": "hi there"}, {"bot_id": "B9", "text": "hey!"}]
    out = format_transcript(msgs, bot_user_id="UBOT")
    assert "User: hi there" in out
    assert "forge: hey!" in out


def test_format_transcript_treats_bot_user_id_as_forge():
    out = format_transcript([{"user": "UBOT", "text": "I'm forge"}], "UBOT")
    assert "forge: I'm forge" in out


def test_format_transcript_strips_mentions():
    out = format_transcript([{"user": "U1", "text": "<@UBOT> what's up"}], "UBOT")
    assert "User: what's up" in out


def test_format_transcript_skips_empty_messages():
    out = format_transcript([{"user": "U1", "text": ""}, {"user": "U1", "text": "real"}], "UBOT")
    assert out == "User: real"


def test_format_transcript_truncates_very_long_messages():
    out = format_transcript([{"user": "U1", "text": "x" * 5000}], "UBOT")
    assert len(out) < 1000


def test_stop_and_cancel_classify_as_stop():
    for t in ("stop", "cancel", "stop!", "abort", "halt"):
        assert classify_intent(t) == "stop"


def test_forget_creds_classifies():
    assert classify_intent("forget creds") == "forget_creds"
    assert classify_intent("forget the credentials") == "forget_creds"


def test_build_verb_still_wins_over_stop_substring():
    # "stop the polling loop" is a build request, not an interrupt.
    assert classify_intent("add code to stop the polling loop") == "build"


def test_truncate_for_slack_prefers_line_boundaries():
    from forge.slackmsg import truncate_for_slack, SLACK_TEXT_LIMIT
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    out = truncate_for_slack(text)
    assert len(out) <= SLACK_TEXT_LIMIT + 2
    assert out.endswith("…")
    body = out[:-2]
    assert body == text[:len(body)]          # a clean prefix, cut on a line
    assert truncate_for_slack("short") == "short"


def test_narration_line_collapses_to_one_capped_line():
    from forge.slackmsg import narration_line
    out = narration_line("First I read the file.\n\nThen I " + "y" * 500)
    assert "\n" not in out
    assert len(out) <= 241
    assert out.endswith("…")
    assert narration_line("  tidy   spaces  ") == "tidy spaces"


def test_remember_intent_and_lesson_extraction():
    from forge.slackmsg import classify_intent, remember_text
    # The separator is what distinguishes teaching from a build request that
    # happens to start with "remember".
    assert classify_intent("remember: always run bun install before dev") == "remember"
    assert classify_intent("remember for o/r: seed the db with `make seed`") == "remember"
    assert classify_intent("remember to add a login page") == "build"
    assert remember_text("remember:  use   bun, not npm ") == "use bun, not npm"
    assert remember_text("<@UBOT> remember that: port 3001 is the api") == \
        "port 3001 is the api"
    assert remember_text("fix the bug") == ""


def test_digest_short_text_passes_through():
    from forge.slackmsg import digest_for_slack
    short, full = digest_for_slack("All done — added the button.")
    assert short == "All done — added the button."
    assert full is None


def test_digest_long_text_cuts_at_paragraph_and_returns_full():
    from forge.slackmsg import digest_for_slack
    paras = ["Para %d: " % i + "x" * 200 for i in range(8)]
    text = "\n\n".join(paras)
    short, full = digest_for_slack(text)
    assert full == text                       # the snippet carries everything
    assert len(short) < len(text)
    assert short.endswith("…")
    # A clean prefix cut on a paragraph boundary — no mid-word truncation.
    assert text.startswith(short[:-2].rstrip())


def test_digest_respects_custom_limit():
    from forge.slackmsg import digest_for_slack
    short, full = digest_for_slack("word " * 100, limit=50)
    assert full is not None
    assert len(short) <= 52


from forge.slackmsg import unwrap_links


def test_unwrap_labelled_link():
    assert unwrap_links("match <https://x.com/d|the design>") == \
        "match https://x.com/d (the design)"


def test_unwrap_bare_link():
    assert unwrap_links("see <https://x.com/d>") == "see https://x.com/d"


def test_unwrap_leaves_mentions_and_plain_text():
    assert unwrap_links("<@U123> fix it") == "<@U123> fix it"
    assert unwrap_links("no links here") == "no links here"


def test_unwrap_label_same_as_url_not_duplicated():
    assert unwrap_links("<https://x.com|https://x.com>") == "https://x.com"
