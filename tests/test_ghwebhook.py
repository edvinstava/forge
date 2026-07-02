import hashlib
import hmac

from forge import ghwebhook


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    body = b'{"a":1}'
    assert ghwebhook.verify_signature("s", body, _sig("s", body)) is True


def test_verify_signature_rejects_wrong_secret_or_body():
    body = b'{"a":1}'
    assert ghwebhook.verify_signature("s", body, _sig("wrong", body)) is False
    assert ghwebhook.verify_signature("s", b'{"a":2}', _sig("s", body)) is False


def test_verify_signature_rejects_missing_or_malformed_header():
    assert ghwebhook.verify_signature("s", b"x", "") is False
    assert ghwebhook.verify_signature("s", b"x", "sha1=abc") is False


def test_verify_signature_fails_closed_without_secret():
    body = b"x"
    assert ghwebhook.verify_signature("", body, _sig("", body)) is False


def test_delivery_log_dedups_and_evicts():
    log = ghwebhook.DeliveryLog(maxlen=2)
    assert log.seen("g1") is False
    assert log.seen("g1") is True          # repeat
    assert log.seen("g2") is False
    assert log.seen("g3") is False         # evicts g1
    assert log.seen("g1") is False         # forgotten after eviction


def test_load_or_create_secret_creates_0600_and_reuses(tmp_path):
    p = tmp_path / "sub" / "webhook.secret"
    s1 = ghwebhook.load_or_create_secret(p)
    assert len(s1) == 64                   # token_hex(32)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert ghwebhook.load_or_create_secret(p) == s1   # stable across boots


def _payload(body="@acme-forge review", assoc="OWNER", sender="User",
             is_pr=True, action="created"):
    issue = {"number": 7}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": action,
        "issue": issue,
        "comment": {"id": 991, "body": body, "author_association": assoc},
        "sender": {"type": sender},
        "repository": {"full_name": "acme/app"},
    }


def test_parse_command_happy_path():
    cmd = ghwebhook.parse_command("issue_comment", _payload(), "acme-forge")
    assert (cmd.owner, cmd.repo, cmd.number, cmd.comment_id) == \
        ("acme", "app", 7, 991)
    assert cmd.slug == "acme/app"


def test_parse_command_is_case_insensitive_and_mid_comment():
    assert ghwebhook.parse_command(
        "issue_comment", _payload(body="please @Acme-Forge REVIEW this"),
        "acme-forge") is not None


def test_parse_command_rejects_lookalikes():
    for body in ("@acme-forge reviews", "see @acme-forgex review",
                 "acme-forge review", "@acme-forge  please review"):
        assert ghwebhook.parse_command(
            "issue_comment", _payload(body=body), "acme-forge") is None, body


def test_parse_command_gates():
    p = _payload
    cases = [
        ("pull_request", p(), "wrong event"),
        ("issue_comment", p(action="edited"), "edited not created"),
        ("issue_comment", p(is_pr=False), "plain issue"),
        ("issue_comment", p(sender="Bot"), "bot sender"),
        ("issue_comment", p(assoc="NONE"), "no association"),
        ("issue_comment", p(assoc="CONTRIBUTOR"), "contributor insufficient"),
        ("issue_comment", p(body="lgtm!"), "no command"),
    ]
    for event, payload, why in cases:
        assert ghwebhook.parse_command(event, payload, "acme-forge") is None, why


def test_parse_command_allows_member_and_collaborator():
    for assoc in ("MEMBER", "COLLABORATOR"):
        assert ghwebhook.parse_command(
            "issue_comment", _payload(assoc=assoc), "acme-forge") is not None


def test_parse_command_never_raises_on_malformed_payload():
    assert ghwebhook.parse_command("issue_comment", {}, "acme-forge") is None
    assert ghwebhook.parse_command(
        "issue_comment", {"action": "created", "issue": None}, "acme-forge") is None


class FakeGhApp:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def installation_token(self, owner, repo):
        if self.fail:
            raise RuntimeError("no installation")
        return "ghs_tok"

    def http(self, method, url, token, data=None):
        self.calls.append((method, url, token, data))
        return {}


def test_ack_comment_posts_eyes_reaction():
    app = FakeGhApp()
    ghwebhook.ack_comment(app, "o", "r", 991)
    m, url, tok, data = app.calls[0]
    assert m == "POST"
    assert url.endswith("/repos/o/r/issues/comments/991/reactions")
    assert tok == "ghs_tok" and data == {"content": "eyes"}


def test_post_comment_targets_issue_number():
    app = FakeGhApp()
    ghwebhook.post_comment(app, "o", "r", 7, "hello")
    m, url, tok, data = app.calls[0]
    assert m == "POST" and url.endswith("/repos/o/r/issues/7/comments")
    assert data == {"body": "hello"}


def test_ack_and_comment_swallow_errors():
    app = FakeGhApp(fail=True)
    ghwebhook.ack_comment(app, "o", "r", 1)     # must not raise
    ghwebhook.post_comment(app, "o", "r", 1, "x")


def test_verify_signature_rejects_non_ascii_header_without_raising():
    # Starlette decodes header bytes as latin-1, so obs-text is representable;
    # a crafted header must be a clean False, not a TypeError → public 500
    # (hmac.compare_digest refuses non-ASCII str arguments).
    assert ghwebhook.verify_signature("s", b"x", "sha256=\xe9abc") is False


def test_load_or_create_secret_survives_corrupted_file(tmp_path):
    p = tmp_path / "webhook.secret"
    p.write_bytes(b"\xff\xfe\x00garbage")       # undecodable → regenerate
    s = ghwebhook.load_or_create_secret(p)      # must not crash boot
    assert len(s) == 64
    assert (p.stat().st_mode & 0o777) == 0o600
