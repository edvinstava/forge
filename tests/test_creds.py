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


def test_unwraps_slack_mailto_link_markup():
    # Slack rewrites a typed email as <mailto:addr|label>; the address must be
    # recovered — the raw markup must never be parsed as a user:pass pair
    # (regression: username='<mailto', password='addr|addr>').
    assert parse_credentials(
        "<mailto:user@devotta.no|user@devotta.no> / hunter2") == [
        {"username": "user@devotta.no", "password": "hunter2"}]


def test_unwraps_slack_mailto_markup_with_role_and_colon():
    text = ("login credentials: <mailto:a@b.c|a@b.c> :: pw1 for admin, "
            "<mailto:u@b.c|u@b.c> :: pw2 for the user account")
    assert parse_credentials(text) == [
        {"role": "admin", "username": "a@b.c", "password": "pw1"},
        {"role": "user", "username": "u@b.c", "password": "pw2"}]


def test_unwraps_slack_url_link_markup():
    # Domain-looking usernames get linkified as <http://…|label>; keep the label.
    assert parse_credentials("<https://x.io|x.io> :: pw") == [
        {"username": "x.io", "password": "pw"}]


def test_unescapes_slack_html_entities():
    # Slack HTML-escapes message text; a password containing & arrives as &amp;.
    assert parse_credentials("bob :: p&amp;ss&lt;1&gt;") == [
        {"username": "bob", "password": "p&ss<1>"}]


def test_redacts_each_secret_occurrence():
    out = redact_secrets("logging in with hunter2 then hunter2 again", ["hunter2"])
    assert "hunter2" not in out
    assert out.count("••••") == 2


def test_redact_ignores_empty_secrets_and_none_text():
    assert redact_secrets("abc", ["", None]) == "abc"
    assert redact_secrets(None, ["x"]) is None
