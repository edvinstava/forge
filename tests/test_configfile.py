from forge.configfile import parse_env_file


def test_basic_key_value():
    assert parse_env_file("SLACK_BOT_TOKEN=xoxb-1") == {"SLACK_BOT_TOKEN": "xoxb-1"}


def test_ignores_blank_and_comment_lines():
    text = "\n# a comment\n   # indented comment\nGH_TOKEN=ghp_1\n"
    assert parse_env_file(text) == {"GH_TOKEN": "ghp_1"}


def test_strips_export_prefix():
    assert parse_env_file("export GH_TOKEN=ghp_1") == {"GH_TOKEN": "ghp_1"}


def test_strips_trailing_whitespace_and_newline():
    # the originating bug: a pasted token with trailing spaces
    assert parse_env_file("SLACK_BOT_TOKEN=xoxb-1   ") == {"SLACK_BOT_TOKEN": "xoxb-1"}


def test_strips_whitespace_around_equals():
    assert parse_env_file("KEY = value") == {"KEY": "value"}


def test_double_quotes_stripped_interior_preserved():
    assert parse_env_file('KEY="a b"') == {"KEY": "a b"}


def test_single_quotes_stripped():
    assert parse_env_file("KEY='a b'") == {"KEY": "a b"}


def test_value_may_contain_equals():
    assert parse_env_file("KEY=a=b=c") == {"KEY": "a=b=c"}


def test_empty_quoted_value():
    assert parse_env_file('KEY=""') == {"KEY": ""}


def test_malformed_line_skipped(capsys):
    assert parse_env_file("NOEQUALS\nKEY=v") == {"KEY": "v"}
    assert "malformed" in capsys.readouterr().err


def test_invalid_key_skipped(capsys):
    assert parse_env_file("1BAD=v\nKEY=v") == {"KEY": "v"}
    assert "invalid key" in capsys.readouterr().err


def test_duplicate_key_last_wins():
    assert parse_env_file("KEY=first\nKEY=second") == {"KEY": "second"}
