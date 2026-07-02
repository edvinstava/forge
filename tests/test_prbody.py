import json

from forge import prbody


# --- parse_pr_meta ------------------------------------------------------------

def test_parse_pr_meta_happy_path():
    meta = prbody.parse_pr_meta(json.dumps(
        {"title": "Fix offer price column (ABC-374)",
         "body": "## Summary\nUse the latest offer's total_price."}))
    assert meta["title"] == "Fix offer price column (ABC-374)"
    assert meta["body"].startswith("## Summary")


def test_parse_pr_meta_bad_json_and_types_degrade_to_none():
    assert prbody.parse_pr_meta("not json") == {"title": None, "body": None}
    assert prbody.parse_pr_meta("[1,2]") == {"title": None, "body": None}
    assert prbody.parse_pr_meta(json.dumps({"title": 7, "body": ["x"]})) == \
        {"title": None, "body": None}
    assert prbody.parse_pr_meta("") == {"title": None, "body": None}


def test_parse_pr_meta_collapses_newlines_and_clips_title():
    meta = prbody.parse_pr_meta(json.dumps(
        {"title": "Fix the\nadmin offers table " + "x" * 100, "body": ""}))
    assert "\n" not in meta["title"]
    assert len(meta["title"]) <= prbody.TITLE_LIMIT
    assert meta["body"] is None


# --- issue refs / titles -------------------------------------------------------

def test_issue_refs_ordered_and_deduped():
    refs = prbody.issue_refs("Fix ABC-374 and ABC-1; see ABC-374 again")
    assert refs == ["ABC-374", "ABC-1"]


def test_issue_refs_ignores_ordinary_hyphenations():
    assert prbody.issue_refs("use UTF-8 and a first-class fix") == []


def test_fallback_title_uses_first_task_line_not_repo_slug():
    t = prbody.fallback_title(
        "The offer table on the admin page is reflecting the project price, "
        "not the offer price. In the webapp application. Issue number "
        "ABC-374", "acme/webapp")
    assert t.startswith("The offer table on the admin page")
    assert len(t) <= prbody.TITLE_LIMIT
    assert "acme" not in t


def test_fallback_title_empty_task_names_repo():
    assert prbody.fallback_title("", "o/r") == "forge: update o/r"


def test_ensure_issue_ref_appends_when_missing():
    assert prbody.ensure_issue_ref("Fix offer price", ["ABC-374"]) == \
        "Fix offer price (ABC-374)"
    already = prbody.ensure_issue_ref("ABC-374: fix", ["ABC-374"])
    assert already == "ABC-374: fix"


def test_ensure_issue_ref_reclips_long_titles_to_fit_the_key():
    # The key outranks the tail of the title — trackers auto-link on it.
    out = prbody.ensure_issue_ref("word " * 30, ["ABC-374"])
    assert out.endswith("(ABC-374)")
    assert len(out) <= prbody.TITLE_LIMIT


# --- compose_body --------------------------------------------------------------

def test_compose_body_prefers_agent_body_and_appends_footer():
    body = prbody.compose_body(task="fix it", run_id="abc123def456789",
                               branch="forge/fix-it-abc123",
                               meta_body="## Summary\n\nDid the thing.",
                               refs=["ABC-374"])
    assert body.startswith("## Summary")
    assert "**Refs:** ABC-374" in body
    assert "run `abc123def456`" in body
    assert "forge/fix-it-abc123" in body


def test_compose_body_falls_back_to_task_and_leads_with_warning():
    body = prbody.compose_body(task="fix the price column", run_id="r1",
                               branch="b", warning="Opened as a draft — x fails.")
    assert body.startswith("> ⚠️ **Opened as a draft")
    assert "## Task\n\nfix the price column" in body
