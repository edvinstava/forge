"""GitHub webhook ingress: comment-command trigger for PR reviews.

Pure logic (HMAC verification, command parsing, delivery dedup) plus thin
best-effort GitHub side-effects that ride an injected GhApp. The FastAPI
route lives in webapp.attach_github_webhook; the CLI wiring in cli._cmd_web.
"""
import hashlib
import hmac
import logging
import re
import secrets as _secrets
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("forge.ghwebhook")

_API = "https://api.github.com"

_ASSOC_OK = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass(frozen=True)
class ReviewCommand:
    owner: str
    repo: str
    number: int
    comment_id: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_command(event: str, payload: dict, slug: str):
    """A ReviewCommand iff this delivery is a freshly created PR comment by a
    non-bot with owner/member/collaborator association whose body mentions
    `@{slug} review`. Anything else — including malformed payloads — is None,
    never an exception (webhooks are untrusted input)."""
    if event != "issue_comment":
        return None
    try:
        if payload.get("action") != "created":
            return None
        issue = payload["issue"]
        if "pull_request" not in issue:
            return None
        if payload["sender"]["type"] == "Bot":
            return None
        comment = payload["comment"]
        if comment.get("author_association") not in _ASSOC_OK:
            return None
        pat = re.compile(rf"(?:^|\s)@{re.escape(slug)}\s+review\b", re.I)
        if not pat.search(comment.get("body") or ""):
            return None
        owner, repo = payload["repository"]["full_name"].split("/", 1)
        return ReviewCommand(owner, repo, int(issue["number"]),
                             int(comment["id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """HMAC-SHA256 over the raw request body vs `X-Hub-Signature-256`.
    Fails closed: empty secret, absent or non-sha256 header all reject."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str, and
    # the header is attacker-controlled (Starlette decodes headers as latin-1).
    return hmac.compare_digest(f"sha256={digest}".encode(),
                               header.encode("latin-1", "replace"))


class DeliveryLog:
    """Remember recent X-GitHub-Delivery GUIDs — GitHub delivers
    at-least-once, and a redelivered command must not start a second run.
    In-memory on purpose: a daemon restart forgetting old GUIDs at worst
    re-runs one review, and this keeps store.py untouched."""

    def __init__(self, maxlen: int = 1024):
        self._maxlen = maxlen
        self._order: deque = deque()
        self._seen: set = set()
        # Today seen() runs only on the server's event loop, but the lock
        # keeps check-then-add atomic if a thread ever calls it.
        self._lock = threading.Lock()

    def seen(self, guid: str) -> bool:
        with self._lock:
            if guid in self._seen:
                return True
            self._seen.add(guid)
            self._order.append(guid)
            if len(self._order) > self._maxlen:
                self._seen.discard(self._order.popleft())
            return False


def load_or_create_secret(path: Path) -> str:
    """The webhook secret must survive restarts (GitHub keeps the copy we
    PATCH into the App config); generate once, 0600, reuse thereafter."""
    p = Path(path).expanduser()
    try:
        text = p.read_text().strip()
        if text:
            return text
    except (OSError, UnicodeError):     # unreadable or corrupted → regenerate
        pass
    tok = _secrets.token_hex(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(mode=0o600)                 # perms pinned before any secret bytes land
    p.chmod(0o600)                      # …and corrected if the file pre-existed
    p.write_text(tok)
    return tok


def ack_comment(app, owner: str, repo: str, comment_id: int) -> None:
    """👀 on the command comment: 'forge heard you'. Best-effort — an ack
    must never abort the run (same rule as Slack renders)."""
    try:
        tok = app.installation_token(owner, repo)
        app.http("POST",
                 f"{_API}/repos/{owner}/{repo}/issues/comments/"
                 f"{comment_id}/reactions",
                 tok, {"content": "eyes"})
    except Exception:
        logger.debug("reaction ack failed for %s/%s#c%s",
                     owner, repo, comment_id, exc_info=True)


def post_comment(app, owner: str, repo: str, number: int, body: str) -> None:
    """Best-effort PR conversation comment (capacity refusals, failure
    notices). Failures are logged and dropped."""
    try:
        tok = app.installation_token(owner, repo)
        app.http("POST", f"{_API}/repos/{owner}/{repo}/issues/{number}/comments",
                 tok, {"body": body})
    except Exception:
        logger.debug("comment post failed for %s/%s#%s",
                     owner, repo, number, exc_info=True)
