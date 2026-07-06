import pytest
from forge.runspec import make_runspec, normalize_github_repo


def test_valid_repo_and_branch_slug():
    rs = make_runspec("acme/internship-portal", "Fix the Org Unit tree!", "abcd1234ef")
    assert rs.repo == "acme/internship-portal"
    # forge/<session>/<fitting-name>; articles dropped from the name
    assert rs.branch == "forge/abcd1234/fix-org-unit-tree"


def test_invalid_repo_rejected():
    with pytest.raises(ValueError):
        make_runspec("not-a-repo", "task", "abcd1234ef")


def test_long_task_slug_truncated():
    rs = make_runspec("a/b", "x" * 100, "abcd1234ef")
    name = rs.branch.rsplit("/", 1)[1]
    assert len(name) <= 32


# --- _slug: a fitting name from a free-form prompt ---

def test_greeting_and_filler_stripped():
    rs = make_runspec("a/b", "Hi, can you do this issue here on the webapp app?",
                      "57d7cf1cab")
    assert rs.branch == "forge/57d7cf1c/issue-webapp-app"


def test_issue_key_promoted_to_front():
    rs = make_runspec("a/b", "hey could you fix ABC-379 emission fields please",
                      "abcd1234ef")
    assert rs.branch == "forge/abcd1234/abc-379-fix-emission-fields"


def test_issue_key_pulled_out_of_pasted_url():
    rs = make_runspec("a/b", "do https://jira.example.com/browse/ABC-379 thanks",
                      "abcd1234ef")
    assert rs.branch == "forge/abcd1234/abc-379"


def test_url_noise_not_slugified():
    rs = make_runspec("a/b", "fix login at https://github.com/acme/webapp",
                      "abcd1234ef")
    assert rs.branch == "forge/abcd1234/fix-login"


def test_repo_name_and_repo_talk_dropped():
    # "For the opplandstaal project" says which repo — the branch already
    # lives there, so neither the repo's name nor "project" earns a slot.
    rs = make_runspec(
        "acme/opplandstaal",
        "Can you do this? For the opplandstaal project. OSTAL-375: Admin UI - "
        "Add emission field to product form",
        "f6750f29d5294b07")
    assert rs.branch == "forge/f6750f29/ostal-375-admin-ui-add-emission"


def test_multiword_repo_name_words_dropped():
    rs = make_runspec("acme/internship-portal",
                      "update the internship portal signup flow", "abcd1234ef")
    assert rs.branch == "forge/abcd1234/update-signup-flow"


def test_all_filler_falls_back_to_task():
    rs = make_runspec("a/b", "hi can you please", "abcd1234ef")
    assert rs.branch == "forge/abcd1234/task"


def test_repo_only_task_falls_back_to_task():
    rs = make_runspec("acme/webapp", "the webapp repo please", "abcd1234ef")
    assert rs.branch == "forge/abcd1234/task"


def test_empty_task_falls_back_to_task():
    rs = make_runspec("a/b", "", "abcd1234ef")
    assert rs.branch == "forge/abcd1234/task"


def test_truncation_lands_on_word_boundary():
    rs = make_runspec("a/b", "add emission fields configuration management dashboard "
                             "overview page", "abcd1234ef")
    name = rs.branch.rsplit("/", 1)[1]
    assert len(name) <= 32
    assert not name.endswith("-")
    # never cut mid-word: every kept word is a whole word from the task
    assert all(w in {"add", "emission", "fields", "configuration", "management",
                     "dashboard", "overview", "page"} for w in name.split("-"))


# --- normalize_github_repo: accept the URL forms a user naturally pastes ---

@pytest.mark.parametrize("value", [
    "acme/webapp",
    "https://github.com/acme/webapp",
    "https://github.com/acme/webapp.git",
    "http://github.com/acme/webapp",
    "github.com/acme/webapp",
    "git@github.com:acme/webapp.git",
    "  https://github.com/acme/webapp/  ",
    "https://github.com/acme/webapp/tree/master",
])
def test_normalize_github_repo_reduces_to_slug(value):
    assert normalize_github_repo(value) == "acme/webapp"


def test_normalize_preserves_dotted_repo_name():
    assert normalize_github_repo("https://github.com/o/my.repo") == "o/my.repo"


@pytest.mark.parametrize("value", [
    "",
    "   ",
    "not a repo",
    "https://github.com/owner",          # owner only, no repo
    "https://example.com/",
])
def test_normalize_github_repo_rejects_garbage(value):
    with pytest.raises(ValueError):
        normalize_github_repo(value)
