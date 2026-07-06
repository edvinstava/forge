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
    ".forge/*",
    "!.forge/repo.yml",
    "!.forge/env.yml",
    "!.forge/verify.sh",
]


def test_exclude_forge_scratch_adds_all_patterns(tmp_path):
    """All scratch patterns are written to .git/info/exclude."""
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
    (ws / ".git" / "info" / "exclude").write_text("# header\r\n.forge/*\r\n")

    h = LocalHost()
    exclude_forge_scratch(h, str(ws))
    exclude_forge_scratch(h, str(ws))

    content = (ws / ".git" / "info" / "exclude").read_text()
    # .forge/* should appear exactly once despite CRLF
    assert content.count(".forge/*") == 1, ".forge/* duplicated on CRLF file"


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


def _write_scratch_and_config(ws):
    (ws / ".forge").mkdir()
    (ws / ".forge" / "plan.json").write_text('{"plan": []}')
    (ws / ".forge" / "qa.json").write_text('{"qa": []}')
    (ws / ".forge" / "review.json").write_text('{"review": []}')
    (ws / ".forge" / "lessons.json").write_text('{"lessons": []}')
    (ws / ".forge" / "artifacts").mkdir()
    (ws / ".forge" / "artifacts" / "x.png").write_bytes(b"\x89PNG")
    # The browser live view (frame dumps + screencaster) — PR #232's leak
    (ws / ".forge" / "live").mkdir()
    (ws / ".forge" / "live" / "frame.jpg").write_bytes(b"\xff\xd8JPEG")
    (ws / ".forge" / "live" / "meta.json").write_text('{"url": "x"}')
    (ws / ".forge" / "live" / "screencast.cjs").write_text("// script")
    (ws / ".forge" / "live" / "screencast.log").write_text("attached")
    (ws / ".forge" / "live" / "stop").write_text("")
    # A scratch file no one has invented yet — default-deny must cover it
    (ws / ".forge" / "future-scratch.json").write_text("{}")
    (ws / "report.md").write_text("# PR body")
    # Repo-authored config surface — must all be staged
    (ws / ".forge" / "repo.yml").write_text("name: my-repo")
    (ws / ".forge" / "env.yml").write_text("env: {}")
    (ws / ".forge" / "verify.sh").write_text("#!/bin/sh\ntrue")


def _staged(ws):
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    result = subprocess.run(
        ["git", "-C", str(ws), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    )
    return set(result.stdout.strip().splitlines())


_LEAKS = ["report.md", ".forge/plan.json", ".forge/qa.json", ".forge/review.json",
          ".forge/lessons.json", ".forge/artifacts/x.png", ".forge/live/frame.jpg",
          ".forge/live/meta.json", ".forge/live/screencast.cjs",
          ".forge/live/screencast.log", ".forge/live/stop",
          ".forge/future-scratch.json"]
_CONFIG = [".forge/repo.yml", ".forge/env.yml", ".forge/verify.sh"]


def test_exclude_forge_scratch_real_git(tmp_path):
    """Real-git proof: everything under .forge/ (known scratch, the live view,
    and files not invented yet) is NOT staged; the repo-authored config
    surface IS staged."""
    ws = tmp_path / "ws"
    _init_repo(ws)
    _write_scratch_and_config(ws)

    exclude_forge_scratch(LocalHost(), str(ws))

    staged = _staged(ws)
    for leak in _LEAKS:
        assert leak not in staged, f"{leak} leaked into staged files"
    for keep in _CONFIG:
        assert keep in staged, f"{keep} was excluded but should reach the PR"


def test_exclude_forge_scratch_upgrades_legacy_workspace(tmp_path):
    """A workspace provisioned by an older forge already has the enumerated
    per-file patterns in its exclude. Re-running the new exclude must append
    the default-deny block AFTER them and still re-include the config surface
    (gitignore is last-match-wins, so ordering is load-bearing)."""
    ws = tmp_path / "ws"
    _init_repo(ws)
    _write_scratch_and_config(ws)
    legacy = ("/report.md\n.forge/plan.json\n.forge/qa.json\n.forge/review.json\n"
              ".forge/lessons.json\n.forge/pr.json\n.forge/pr.diff\n"
              ".forge/artifacts/\n.forge/inbox/\n")
    (ws / ".git" / "info" / "exclude").write_text(legacy)

    exclude_forge_scratch(LocalHost(), str(ws))

    staged = _staged(ws)
    for leak in _LEAKS:
        assert leak not in staged, f"{leak} leaked from a legacy workspace"
    for keep in _CONFIG:
        assert keep in staged, f"{keep} lost to the legacy exclude ordering"


def test_exclude_covers_attachment_inbox(tmp_path):
    """User-attached images in .forge/inbox/ don't leak into PR diffs."""
    ws = tmp_path / "ws"
    _init_repo(ws)
    (ws / ".forge" / "inbox").mkdir(parents=True)
    (ws / ".forge" / "inbox" / "img.png").write_bytes(b"\x89PNG")

    exclude_forge_scratch(LocalHost(), str(ws))

    assert ".forge/inbox/img.png" not in _staged(ws), \
        ".forge/inbox/img.png leaked into staged files"


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
