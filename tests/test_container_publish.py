from forge.commands import parse_host_port


def test_parse_host_port_ipv4():
    assert parse_host_port("127.0.0.1:5051\n") == 5051


def test_parse_host_port_multiline_picks_first():
    assert parse_host_port("127.0.0.1:5051\n[::1]:5051\n") == 5051


def test_parse_host_port_empty():
    assert parse_host_port("") is None
    assert parse_host_port(None) is None


def test_parse_host_port_no_match():
    assert parse_host_port("garbage") is None
