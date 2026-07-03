import subprocess
from pathlib import Path
from forge.hostops import LocalHost


def _init_repo(path: Path, origin: str | None = None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)


def test_clone_local_creates_branch_and_rewires_origin(tmp_path):
    src = tmp_path / "src"
    _init_repo(src, origin="https://github.com/o/r.git")
    dest = tmp_path / "dest"
    h = LocalHost()
    res = h.clone_local(str(src), "forge/x", str(dest))
    assert res.exit_code == 0
    assert (dest / "README.md").is_file()
    branch = subprocess.run(["git", "-C", str(dest), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "forge/x"
    assert h.origin_url(str(dest)) == "https://github.com/o/r.git"


def test_origin_url_none_when_absent(tmp_path):
    src = tmp_path / "src"
    _init_repo(src, origin=None)
    assert LocalHost().origin_url(str(src)) is None


def test_clone_local_rejects_dash_leading_src(tmp_path):
    res = LocalHost().clone_local("--upload-pack=evil", "forge/x", str(tmp_path / "dest"))
    assert res.exit_code == 2
    assert "refusing" in res.stderr


def test_clone_pr_clones_then_checks_out_pr(tmp_path, monkeypatch):
    import forge.hostops as ho
    calls = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("cwd"), (kw.get("env") or {}).get("GH_TOKEN")))
        return R()

    monkeypatch.setattr(ho.subprocess, "run", fake_run)
    dest = str(tmp_path / "ws")
    res = ho.LocalHost().clone_pr("o/r", dest, 7, "tok")
    assert res.exit_code == 0
    assert calls[0][0] == ["gh", "repo", "clone", "o/r", dest]
    assert calls[0][2] == "tok"                       # clone carries the token
    assert calls[1][0] == ["gh", "pr", "checkout", "7"]
    assert calls[1][1] == dest                        # checkout runs inside the clone
    assert calls[1][2] == "tok"


def test_clone_pr_rejects_dash_leading_args(tmp_path):
    res = LocalHost().clone_pr("-x", str(tmp_path / "ws"), 1, "tok")
    assert res.exit_code == 2 and "refusing" in res.stderr


# ---------------------------------------------------------------------------
# exclude_forge_scratch tests
# ---------------------------------------------------------------------------

from forge.hostops import exclude_forge_scratch  # noqa: E402


_SCRATCH_PATTERNS = [
    "/report.md",
    ".forge/plan.json",
    ".forge/qa.json",
    ".forge/review.json",
    ".forge/lessons.json",
    ".forge/artifacts/",
]


def test_exclude_forge_scratch_adds_all_patterns(tmp_path):
    """All six scratch patterns are written to .git/info/exclude."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git" / "info").mkdir(parents=True)
    (ws / ".git" / "info" / "exclude").write_text("# git default header\n")

    h = LocalHost()
    exclude_forge_scratch(h, str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    for pat in _SCRATCH_PATTERNS:
        assert pat in content, f"Pattern {pat!r} missing from exclude file"


def test_exclude_forge_scratch_idempotent(tmp_path):
    """Calling twice does NOT duplicate patterns."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git" / "info").mkdir(parents=True)
    (ws / ".git" / "info" / "exclude").write_text("# git default header\n")

    h = LocalHost()
    exclude_forge_scratch(h, str(ws))
    exclude_forge_scratch(h, str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    for pat in _SCRATCH_PATTERNS:
        assert content.count(pat) == 1, f"Pattern {pat!r} duplicated after two calls"


def test_exclude_forge_scratch_crlf_idempotent(tmp_path):
    """CRLF line endings: calling twice does NOT duplicate patterns."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git" / "info").mkdir(parents=True)
    # Write with CRLF line endings
    (ws / ".git" / "info" / "exclude").write_text("# header\r\n.forge/plan.json\r\n")

    h = LocalHost()
    exclude_forge_scratch(h, str(ws))
    exclude_forge_scratch(h, str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    # .forge/plan.json should appear exactly once despite CRLF
    assert content.count(".forge/plan.json") == 1, ".forge/plan.json duplicated on CRLF file"


def test_exclude_forge_scratch_preserves_existing_content(tmp_path):
    """Pre-existing exclude content (e.g. git's default header) is preserved."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git" / "info").mkdir(parents=True)
    header = "# git ls-files --others --exclude-from=.git/info/exclude\n# Lines starting with '#' are comments.\n"
    (ws / ".git" / "info" / "exclude").write_text(header)

    exclude_forge_scratch(LocalHost(), str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    assert content.startswith(header), "Original header was not preserved"


def test_exclude_forge_scratch_handles_missing_exclude_file(tmp_path):
    """Works even when .git/info/exclude doesn't exist yet."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git" / "info").mkdir(parents=True)
    # Do NOT create the exclude file

    exclude_forge_scratch(LocalHost(), str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    for pat in _SCRATCH_PATTERNS:
        assert pat in content, f"Pattern {pat!r} missing when exclude was absent"


def test_exclude_forge_scratch_real_git(tmp_path):
    """Real-git proof: all 6 scratch patterns are NOT staged; repo-authored .forge/repo.yml IS staged."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Init a real git repo with an initial commit
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=ws, check=True)
    (ws / "README.md").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

    # Write all 6 scratch patterns and repo-authored config
    (ws / ".forge").mkdir()
    (ws / ".forge" / "plan.json").write_text('{"plan": []}')
    (ws / ".forge" / "qa.json").write_text('{"qa": []}')
    (ws / ".forge" / "review.json").write_text('{"review": []}')
    (ws / ".forge" / "lessons.json").write_text('{"lessons": []}')
    (ws / ".forge" / "artifacts").mkdir()
    (ws / ".forge" / "artifacts" / "x.png").write_bytes(b"\x89PNG")
    (ws / "report.md").write_text("# PR body")
    (ws / ".forge" / "repo.yml").write_text("name: my-repo")  # repo-authored — must be staged

    # Apply the exclude
    exclude_forge_scratch(LocalHost(), str(ws))

    # Run git add -A and check what's staged
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    result = subprocess.run(
        ["git", "-C", str(ws), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    )
    staged = set(result.stdout.strip().splitlines())

    # All 6 scratch files must NOT be staged
    assert "report.md" not in staged, "report.md leaked into staged files"
    assert ".forge/plan.json" not in staged, ".forge/plan.json leaked into staged files"
    assert ".forge/qa.json" not in staged, ".forge/qa.json leaked into staged files"
    assert ".forge/review.json" not in staged, ".forge/review.json leaked into staged files"
    assert ".forge/lessons.json" not in staged, ".forge/lessons.json leaked into staged files"
    assert ".forge/artifacts/x.png" not in staged, ".forge/artifacts/x.png leaked into staged files"

    # Repo-authored config MUST be staged
    assert ".forge/repo.yml" in staged, ".forge/repo.yml was excluded but should reach the PR"


def test_exclude_covers_attachment_inbox(tmp_path):
    """Attachment inbox patterns are excluded so user images don't leak into PR diffs."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git" / "info").mkdir(parents=True)
    (ws / ".git" / "info" / "exclude").write_text("# git default header\n")

    h = LocalHost()
    exclude_forge_scratch(h, str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    assert ".forge/inbox/" in content, ".forge/inbox/ pattern missing from exclude file"


def test_hardened_git_neutralizes_repo_config_execution_vectors():
    # Host-side git against an agent-modified workspace: the repo's .git/config
    # is hostile input — fsmonitor, hooksPath and credential.helper can each
    # execute arbitrary commands on the host. hardened_git overrides all three
    # (and re-adds gh as the only credential helper so pushes still auth).
    from forge.hostops import hardened_git
    argv = hardened_git("/ws", "push", "-u", "origin", "b")
    assert argv[0] == "git"
    assert argv[-6:] == ["-C", "/ws", "push", "-u", "origin", "b"]
    cvals = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert "core.fsmonitor=false" in cvals
    assert "core.hooksPath=/dev/null" in cvals
    assert cvals.index("credential.helper=") \
        < cvals.index("credential.helper=!gh auth git-credential")
