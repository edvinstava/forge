from forge.health import health_poll_argv


def test_health_poll_argv_shape():
    argv = health_poll_argv(3000, "/", 90)
    assert argv[0] == "bash" and argv[1] == "-lc"
    body = argv[2]
    assert "http://localhost:3000/" in body
    assert "90" in body
    assert "curl" in body


def test_health_poll_custom_path():
    body = health_poll_argv(8080, "/api/system/info.json", 5)[2]
    assert "http://localhost:8080/api/system/info.json" in body
