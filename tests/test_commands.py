from forge import commands as c


def test_clone_and_branch():
    assert c.clone_cmd("a/b") == ["gh", "repo", "clone", "a/b", "."]
    assert c.branch_cmd("forge/x") == ["git", "checkout", "-b", "forge/x"]


def test_worker_cmd_with_and_without_model():
    base = c.worker_cmd("do x", None)
    assert base[:2] == ["claude", "-p"]
    assert "do x" in base
    assert "--output-format" in base and "json" in base
    assert "--dangerously-skip-permissions" in base
    assert "--model" not in base
    assert "--model" in c.worker_cmd("do x", "claude-opus-4-8")


def test_worker_cmd_resumes_when_session_given():
    cmd = c.worker_cmd("p", None, "sess-1")
    assert "--resume" in cmd and "sess-1" in cmd


def test_worker_cmd_no_resume_first_turn():
    assert "--resume" not in c.worker_cmd("p", None, None)


def test_pr_create_draft_flag():
    assert "--draft" in c.pr_create_cmd("t", "body.md", draft=True)
    assert "--draft" not in c.pr_create_cmd("t", "body.md", draft=False)


def test_commit_cmds_use_supplied_identity():
    cmds = c.commit_cmds("msg", "Dev", "dev@example.com")
    assert ["git", "config", "user.name", "Dev"] in cmds
    assert ["git", "config", "user.email", "dev@example.com"] in cmds
    assert ["git", "add", "-A"] in cmds
    assert cmds[-1][:2] == ["git", "commit"]


def test_setup_git_cmd():
    assert c.setup_git_cmd() == ["gh", "auth", "setup-git"]


def test_pr_checkout_cmd():
    assert c.pr_checkout_cmd(12) == ["gh", "pr", "checkout", "12"]


def test_pr_diff_cmd_targets_repo():
    cmd = c.pr_diff_cmd("o/r", 5)
    assert cmd[:3] == ["gh", "pr", "diff"]
    assert "5" in cmd and "-R" in cmd and "o/r" in cmd


def test_pr_review_api_cmd_posts_reviews_endpoint():
    cmd = c.pr_review_api_cmd("o", "r", 7, "/work/payload.json")
    assert cmd[:2] == ["gh", "api"]
    assert "--method" in cmd and "POST" in cmd
    assert "/repos/o/r/pulls/7/reviews" in cmd
    assert "--input" in cmd and "/work/payload.json" in cmd
