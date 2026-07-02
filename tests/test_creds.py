from forge.creds import parse_credentials, redact_secrets


def test_parses_double_colon_pairs_with_roles():
    text = ("login credentials: dev@example.com :: admin for admin account, "
            "user@devotta.no :: user for user account")
    assert parse_credentials(text) == [
        {"role": "admin", "username": "dev@example.com", "password": "admin"},
        {"role": "user", "username": "user@devotta.no", "password": "user"},
    ]


def test_parses_single_pair_without_role():
    assert parse_credentials("user@x.com / hunter2") == [
        {"username": "user@x.com", "password": "hunter2"}]


def test_returns_empty_when_no_pair():
    assert parse_credentials("just some prose with no creds") == []
    assert parse_credentials("") == []


def test_redacts_each_secret_occurrence():
    out = redact_secrets("logging in with hunter2 then hunter2 again", ["hunter2"])
    assert "hunter2" not in out
    assert out.count("••••") == 2


def test_redact_ignores_empty_secrets_and_none_text():
    assert redact_secrets("abc", ["", None]) == "abc"
    assert redact_secrets(None, ["x"]) is None
