"""Pure helpers for browser-QA credentials: parse a human's freeform Slack
reply into structured {role?, username, password} entries, and redact secret
values out of narration/logs. No I/O — unit-tested in isolation."""
import re

# Strip a leading label like "login credentials:" / "creds:" before parsing.
_LABEL = re.compile(r"^\s*(login\s+)?cred(ential)?s?\s*:\s*", re.I)
# A user/pass pair separated by ::, : or / — user may be an email.
_PAIR = re.compile(r"(?P<user>[^\s:/,]+(?:@[^\s:/,]+)?)\s*(?:::|:|/)\s*(?P<pass>[^\s,]+)")
_ROLE = re.compile(r"\bfor\s+(?:the\s+)?(?P<role>[\w-]+)", re.I)
# Slack link markup: <mailto:addr|label>, <https://…|label>, <tel:…>. Keep the
# label (what the human typed) when present, else the target minus the scheme.
_SLACK_LINK = re.compile(r"<(?:mailto:|tel:)?([^|>\s]+)(?:\|([^>]+))?>")


def _unescape_slack(text: str) -> str:
    """Undo Slack mrkdwn mangling: emails typed in a reply arrive as
    <mailto:addr|addr> and &/</> arrive HTML-escaped. Without this, the link
    markup itself parses as a user:pass pair (its ':' matches _PAIR) and the
    real password is dropped."""
    out = _SLACK_LINK.sub(lambda m: m.group(2) or m.group(1), text)
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def parse_credentials(text: str) -> list:
    body = _LABEL.sub("", _unescape_slack(text or ""))
    out = []
    for seg in re.split(r"[,\n]|\band\b", body):
        m = _PAIR.search(seg)
        if not m:
            continue
        entry = {"username": m.group("user"), "password": m.group("pass")}
        rm = _ROLE.search(seg)
        if rm:
            entry = {"role": rm.group("role").lower(), **entry}
        out.append(entry)
    return out


def redact_secrets(text, secrets) -> str:
    if not text:
        return text
    out = text
    for s in secrets or []:
        if s:
            out = out.replace(s, "••••")
    return out
