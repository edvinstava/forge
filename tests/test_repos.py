import subprocess
from forge.repos import list_repos


def test_list_repos_finds_git_dirs_and_filters(tmp_path):
    (tmp_path / "not-a-repo").mkdir()
    for name in ("dhis2-app", "chap-frontend"):
        p = tmp_path / name
        p.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/dhis2-app.git"],
                   cwd=tmp_path / "dhis2-app", check=True)
    names = {r["name"] for r in list_repos(str(tmp_path))}
    assert names == {"dhis2-app", "chap-frontend"}
    only = list_repos(str(tmp_path), q="dhis")
    assert [r["name"] for r in only] == ["dhis2-app"]
    assert only[0]["remote"] == "https://github.com/o/dhis2-app.git"


def test_missing_dir_returns_empty(tmp_path):
    assert list_repos(str(tmp_path / "nope")) == []
