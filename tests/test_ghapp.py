from types import SimpleNamespace
from forge import ghapp


def _cfg(tmp_path, **kw):
    key = tmp_path / "key.pem"
    key.write_text("PEM")
    base = dict(gh_app_id="123", gh_app_private_key_path=str(key),
                gh_app_slug="forge")
    base.update(kw)
    return SimpleNamespace(**base)


def test_is_configured_requires_id_and_readable_key(tmp_path):
    assert ghapp.is_configured(_cfg(tmp_path)) is True
    assert ghapp.is_configured(_cfg(tmp_path, gh_app_id="")) is False
    assert ghapp.is_configured(
        _cfg(tmp_path, gh_app_private_key_path="/nope.pem")) is False


def test_is_configured_expands_home_in_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "key.pem").write_text("PEM")
    assert ghapp.is_configured(
        _cfg(tmp_path, gh_app_private_key_path="~/key.pem")) is True


def test_jwt_reads_key_through_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "key.pem").write_text("PEM")
    seen = {}

    def signer(app_id, key_pem, now):
        seen["key_pem"] = key_pem
        return "JWT"

    app = ghapp.GhApp(_cfg(tmp_path, gh_app_private_key_path="~/key.pem"),
                      signer=signer, http=FakeHttp(), clock=lambda: 1000.0)
    app.installation_token("o", "r")
    assert seen["key_pem"] == "PEM"


class FakeHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, token, data=None):
        self.calls.append((method, url, token, data))
        if url.endswith("/installation"):
            return {"id": 555}
        if url.endswith("/access_tokens"):
            return {"token": "ghs_inst", "expires_at": "2999-01-01T00:00:00Z"}
        if "/users/" in url:
            return {"id": 42}
        if url.endswith("/app/hook/config"):
            return {"url": (data or {}).get("url", "")}
        raise AssertionError(url)


def test_installation_token_signs_jwt_and_exchanges(tmp_path):
    http = FakeHttp()
    signed = {}

    def signer(app_id, key_pem, now):
        signed.update(app_id=app_id, key_pem=key_pem, now=now)
        return "JWT"

    app = ghapp.GhApp(_cfg(tmp_path), signer=signer, http=http,
                      clock=lambda: 1000.0)
    tok = app.installation_token("o", "r")
    assert tok == "ghs_inst"
    assert signed == {"app_id": "123", "key_pem": "PEM", "now": 1000}
    # installation lookup uses the JWT; token exchange uses the JWT too
    assert ("GET", "https://api.github.com/repos/o/r/installation", "JWT", None) \
        in http.calls
    assert http.calls[-1][0] == "POST"
    assert http.calls[-1][1].endswith("/app/installations/555/access_tokens")


def test_installation_token_is_cached(tmp_path):
    http = FakeHttp()
    app = ghapp.GhApp(_cfg(tmp_path), signer=lambda *a: "JWT", http=http,
                      clock=lambda: 1000.0)
    app.installation_token("o", "r")
    n = len(http.calls)
    app.installation_token("o", "r")          # second call → cache hit, no new HTTP
    assert len(http.calls) == n


def test_bot_identity_derives_login_and_noreply_email(tmp_path):
    http = FakeHttp()
    app = ghapp.GhApp(_cfg(tmp_path), signer=lambda *a: "JWT", http=http,
                      clock=lambda: 1000.0)
    login, email = app.bot_identity()
    assert login == "forge[bot]"
    assert email == "42+forge[bot]@users.noreply.github.com"
    # /users/* is not an App-JWT endpoint — GitHub answers 401 to a JWT there.
    # The lookup is public data and must go out unauthenticated.
    users_call = next(c for c in http.calls if "/users/" in c[1])
    assert users_call[2] == ""


def test_update_webhook_config_patches_with_jwt(tmp_path):
    http = FakeHttp()
    app = ghapp.GhApp(_cfg(tmp_path), signer=lambda *a: "JWT", http=http,
                      clock=lambda: 1000.0)
    app.update_webhook_config("https://x.trycloudflare.com/api/github/webhook",
                              "whsec")
    m, url, tok, data = http.calls[-1]
    assert (m, url, tok) == ("PATCH", "https://api.github.com/app/hook/config",
                             "JWT")
    assert data == {"url": "https://x.trycloudflare.com/api/github/webhook",
                    "content_type": "json", "secret": "whsec"}
