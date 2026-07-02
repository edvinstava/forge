from forge.commands import worker_stream_cmd


def test_stream_cmd_uses_stream_json_and_verbose():
    cmd = worker_stream_cmd("do it", None)
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert "stream-json" in cmd and "--verbose" in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_stream_cmd_resumes_session():
    cmd = worker_stream_cmd("more", None, "sess-7")
    assert "--resume" in cmd and "sess-7" in cmd
