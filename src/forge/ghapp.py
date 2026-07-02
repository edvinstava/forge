"""GitHub App identity + installation-token minting. Used ONLY to post reviews
as forge[bot] and to resolve the bot commit identity — never for reasoning.
HTTP + JWT signing are injected so tests stay hermetic; the real signer imports
PyJWT lazily (optional `gh-app` extra) so degradation works without it."""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

_API = "https://api.github.com"


def _key_path(cfg) -> Path:
    # config.env values arrive verbatim, so "~/.forge/key.pem" must expand here
    return Path(cfg.gh_app_private_key_path).expanduser()


def is_configured(cfg) -> bool:
    return bool(getattr(cfg, "gh_app_id", "")
                and getattr(cfg, "gh_app_private_key_path", "")
                and _key_path(cfg).is_file())


def _default_signer(app_id: str, key_pem: str, now: int) -> str:
    try:
        import jwt  # PyJWT (gh-app extra)
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise RuntimeError(
            "GitHub App configured but PyJWT missing — install the 'gh-app' "
            "extra: pip install 'forge[gh-app]'") from e
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    return jwt.encode(payload, key_pem, algorithm="RS256")


def _default_http(method: str, url: str, token: str, data=None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "forge"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


class GhApp:
    def __init__(self, cfg, signer=_default_signer, http=_default_http,
                 clock=time.time):
        self.cfg, self.signer, self.http, self.clock = cfg, signer, http, clock
        self._tok_cache: dict = {}     # (owner, repo) -> (token, exp_epoch)
        self._identity = None

    def _jwt(self) -> str:
        key = _key_path(self.cfg).read_text()
        return self.signer(self.cfg.gh_app_id, key, int(self.clock()))

    def installation_token(self, owner: str, repo: str) -> str:
        now = self.clock()
        hit = self._tok_cache.get((owner, repo))
        if hit and hit[1] - 300 > now:
            return hit[0]
        jwt_tok = self._jwt()
        inst = self.http("GET", f"{_API}/repos/{owner}/{repo}/installation",
                         jwt_tok, None)
        tok = self.http("POST",
                        f"{_API}/app/installations/{inst['id']}/access_tokens",
                        jwt_tok, {})
        # Cache for ~50 min (real expiry is 1h); parse expires_at when present.
        self._tok_cache[(owner, repo)] = (tok["token"], now + 3000)
        return tok["token"]

    def bot_identity(self):
        if self._identity is None:
            slug = self.cfg.gh_app_slug or "forge"
            login = f"{slug}[bot]"
            # /users/* rejects App JWTs with 401; the bot user is public data,
            # so this lookup goes out unauthenticated.
            user = self.http(
                "GET", f"{_API}/users/{urllib.parse.quote(login, safe='')}",
                "", None)
            self._identity = (login,
                              f"{user['id']}+{login}@users.noreply.github.com")
        return self._identity

    def update_webhook_config(self, url: str, secret: str) -> None:
        """Re-point the App's own webhook (PATCH /app/hook/config, JWT-authed).
        Quick tunnels rotate hostnames every boot; the App rewriting its own
        webhook config is what makes them viable — no manual settings edit."""
        self.http("PATCH", f"{_API}/app/hook/config", self._jwt(),
                  {"url": url, "content_type": "json", "secret": secret})
