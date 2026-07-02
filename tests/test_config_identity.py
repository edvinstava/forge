from forge.cli import resolve_identity


def test_env_wins():
    assert resolve_identity("E", "e@x", "G", "g@x", "H", "h@x") == ("E", "e@x")


def test_falls_back_to_git_then_gh():
    assert resolve_identity("", "", "G", "g@x", "H", "h@x") == ("G", "g@x")
    assert resolve_identity("", "", "", "", "H", "h@x") == ("H", "h@x")


def test_name_and_email_resolved_independently():
    # name from git, email from gh
    assert resolve_identity("", "", "G", "", "", "h@x") == ("G", "h@x")


def test_all_empty():
    assert resolve_identity("", "", "", "", "", "") == ("", "")
