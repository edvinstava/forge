from forge.slackbatch import parse_batch_lines


def test_dash_list():
    assert parse_batch_lines("- fix login\n- add logout\n- dark mode") == \
        ["fix login", "add logout", "dark mode"]


def test_numbered_list():
    assert parse_batch_lines("1. one\n2) two") == ["one", "two"]


def test_star_list():
    assert parse_batch_lines("* a\n* b") == ["a", "b"]


def test_single_line_is_not_a_batch():
    assert parse_batch_lines("- just one") == []


def test_prose_is_not_a_batch():
    assert parse_batch_lines("please fix the login and also add logout") == []


def test_mixed_lines_only_counts_bulleted():
    # a leading repo mention line + bullets → still returns the bullets
    assert parse_batch_lines("on webapp:\n- a\n- b") == ["a", "b"]
