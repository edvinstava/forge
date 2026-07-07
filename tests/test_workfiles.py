import subprocess

import pytest

from forge import workfiles


@pytest.fixture()
def run_ws(tmp_path):
    """A runs_dir with one run whose workspace is a tiny git repo: two committed
    files, one of them then modified, plus one untracked file."""
    ws = tmp_path / "r1" / "workspace"
    ws.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(ws), *args], check=True,
                       capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                            "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"})

    git("init", "-q")
    (ws / "src").mkdir()
    (ws / "src" / "app.ts").write_text("const a = 1\n")
    (ws / "README.md").write_text("hello\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    (ws / "src" / "app.ts").write_text("const a = 2\nconst b = 3\n")
    (ws / "src" / "new.ts").write_text("export {}\n")
    return tmp_path


def test_list_files_merges_tracked_and_untracked_with_status(run_ws):
    out = workfiles.list_files(run_ws, "r1")
    by_path = {f["path"]: f["status"] for f in out["files"]}
    assert by_path["README.md"] == "clean"
    assert by_path["src/app.ts"] == "modified"
    assert by_path["src/new.ts"] == "untracked"
    assert out["truncated"] is False


def test_list_files_missing_workspace_is_empty(tmp_path):
    assert workfiles.list_files(tmp_path, "nope") == {"files": [], "truncated": False}


def test_file_detail_modified_has_content_and_diff(run_ws):
    d = workfiles.file_detail(run_ws, "r1", "src/app.ts")
    assert d["status"] == "modified"
    assert "const b = 3" in d["content"]
    assert "+const b = 3" in d["diff"]
    assert d["binary"] is False and d["missing"] is False


def test_file_detail_untracked_gets_pseudo_diff(run_ws):
    d = workfiles.file_detail(run_ws, "r1", "src/new.ts")
    assert d["status"] == "untracked"
    assert "+export {}" in d["diff"]


def test_file_detail_clean_file_has_empty_diff(run_ws):
    d = workfiles.file_detail(run_ws, "r1", "README.md")
    assert d["status"] == "clean"
    assert d["content"] == "hello\n"
    assert d["diff"] == ""


def test_file_detail_deleted_file_reports_missing(run_ws):
    ws = workfiles.workspace_dir(run_ws, "r1")
    (ws / "README.md").unlink()
    d = workfiles.file_detail(run_ws, "r1", "README.md")
    assert d["missing"] is True
    assert d["status"] == "deleted"
    assert "-hello" in d["diff"]


def test_file_detail_binary_is_flagged_without_content(run_ws):
    ws = workfiles.workspace_dir(run_ws, "r1")
    (ws / "logo.png").write_bytes(b"\x89PNG\x00\x01binary")
    d = workfiles.file_detail(run_ws, "r1", "logo.png")
    assert d["binary"] is True and d["content"] == "" and d["diff"] == ""


def test_traversal_and_git_internals_are_rejected(run_ws):
    assert workfiles.file_detail(run_ws, "r1", "../../etc/passwd") is None
    assert workfiles.file_detail(run_ws, "r1", "/etc/passwd") is None
    assert workfiles.file_detail(run_ws, "r1", ".git/config") is None
    assert workfiles.file_detail(run_ws, "r1", "") is None


def test_symlink_escape_is_rejected(run_ws):
    ws = workfiles.workspace_dir(run_ws, "r1")
    (ws / "escape").symlink_to("/etc")
    assert workfiles.file_detail(run_ws, "r1", "escape/passwd") is None


def test_parse_status_rename_consumes_origin_record():
    out = "R  new.ts\0old.ts\0 M other.ts\0"
    states = workfiles._parse_status_z(out)
    assert states == {"new.ts": "renamed", "other.ts": "modified"}
