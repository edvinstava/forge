from forge.rundir import RunDir


def test_creates_dir_and_writes(tmp_path):
    rd = RunDir.for_run(tmp_path, "r1")
    assert (tmp_path / "r1").is_dir()
    p = rd.write("report.md", "hello")
    assert p.read_text() == "hello"


def test_timeline_appends_with_injected_ts(tmp_path):
    rd = RunDir.for_run(tmp_path, "r1")
    rd.timeline("Run created", ts="20:14")
    rd.timeline("PR opened", ts="20:22")
    content = (tmp_path / "r1" / "timeline.md").read_text()
    assert content == "20:14  Run created\n20:22  PR opened\n"
