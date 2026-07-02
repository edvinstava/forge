"""Pure helpers for browser-QA credentials: parse a human's freeform Slack
reply into structured {role?, username, password} entries, and redact secret
values out of narration/logs. No I/O — unit-tested in isolation."""
import re

# Strip a leading label like "login credentials:" / "creds:" before parsing.
_LABEL = re.compile(r"^\s*(login\s+)?cred(ential)?s?\s*:\s*", re.I)
# A user/pass pair separated by ::, : or / — user may be an email.
_PAIR = re.compile(r"(?P<user>[^\s:/,]+(?:@[^\s:/,]+)?)\s*(?:::|:|/)\s*(?P<pass>[^\s,]+)")
_ROLE = re.compile(r"\bfor\s+(?:the\s+)?(?P<role>[\w-]+)", re.I)


def parse_credentials(text: str) -> list:
    body = _LABEL.sub("", text or "")
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
