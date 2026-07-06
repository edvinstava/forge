"""Compose the title and body of the pull requests forge opens.

The agent that made the change writes `.forge/pr.json` ({"title", "body"}) as
part of its task — it knows what changed and why, so its description beats
anything forge could synthesize. Everything here is the safety net around that:
validate what the agent wrote, fall back to a title derived from the task when
it wrote nothing, carry issue keys (e.g. ABC-374) from the task into the
title, and frame the body with forge's own metadata footer and draft warnings.
Pure functions — no I/O — so the composition rules are unit-testable."""
import json
import re

TITLE_LIMIT = 72
# Cap for the no-body fallback (the agent wrote no description, so we quote the
# task). Keeps even that path concise instead of dumping the whole task text.
BODY_FALLBACK_LIMIT = 280

# Jira/Linear-style issue keys: ABC-374, ABC-1.
_ISSUE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
# Uppercase-token-dash-number sequences that are tech vocabulary, not trackers.
_NOT_ISSUE_PREFIXES = {"UTF", "SHA", "ISO", "RFC", "AES", "RSA", "TLS", "CVE",
                       "IPV", "HTTP", "MD"}


def _clip(text: str, limit: int) -> str:
    """Truncate at a word boundary with an ellipsis; never mid-word."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return (cut or text[: limit - 1]) + "…"


def clip_summary(text: str, limit: int = BODY_FALLBACK_LIMIT) -> str:
    """Condense a task into a short summary for the no-body PR fallback:
    first paragraph only, whitespace-collapsed, clipped at a word boundary."""
    first = (text or "").strip().split("\n\n", 1)[0]
    first = re.sub(r"\s+", " ", first).strip()
    return _clip(first, limit)


def issue_refs(text: str) -> list:
    """Ordered, deduped issue keys mentioned in the task text."""
    seen, out = set(), []
    for m in _ISSUE_RE.findall(text or ""):
        if m.split("-", 1)[0] in _NOT_ISSUE_PREFIXES:
            continue
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def parse_pr_meta(text: str) -> dict:
    """`.forge/pr.json` content -> {"title": str|None, "body": str|None}.
    Tolerant: bad JSON, wrong types, or blank fields degrade to None so the
    caller's fallbacks kick in instead of a broken PR."""
    try:
        d = json.loads(text or "")
    except (ValueError, TypeError):
        return {"title": None, "body": None}
    if not isinstance(d, dict):
        return {"title": None, "body": None}
    title = d.get("title")
    title = " ".join(str(title).split()) if isinstance(title, str) else ""
    body = d.get("body")
    body = str(body).strip() if isinstance(body, str) else ""
    return {"title": _clip(title, TITLE_LIMIT) if title else None,
            "body": body or None}


def fallback_title(task: str, repo: str = "") -> str:
    """A readable title from the task's first line — never the bare repo slug.
    Used only when the agent didn't write a title itself."""
    first = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    first = re.sub(r"\s+", " ", first).rstrip(".!")
    if first:
        return _clip(first, TITLE_LIMIT)
    return f"forge: update {repo}" if repo else "forge: update"


def ensure_issue_ref(title: str, refs) -> str:
    """Append the task's first issue key to the title when it isn't already
    mentioned — trackers auto-link on it, so it outranks the tail of a long
    title (which gets re-clipped to make room)."""
    if not refs or any(r in title for r in refs):
        return title
    ref = refs[0]
    tagged = f"{title} ({ref})"
    if len(tagged) <= TITLE_LIMIT:
        return tagged
    return f"{_clip(title, TITLE_LIMIT - len(ref) - 3)} ({ref})"


def compose_body(*, task: str, run_id: str, branch: str, meta_body: str = None,
                 refs=(), warning: str = None) -> str:
    """The PR body: draft warning first (reviewers must see it), then the
    agent's own description (or a short clip of the task when it wrote none),
    issue refs, and a small provenance footer."""
    parts = []
    if warning:
        parts.append(f"> ⚠️ **{warning}**")
    parts.append(meta_body or (f"## Task\n\n{clip_summary(task)}" if (task or "").strip()
                               else "_No description provided._"))
    if refs:
        parts.append("**Refs:** " + ", ".join(refs))
    parts.append(f"---\n_Opened by forge · run `{run_id[:12]}` · branch `{branch}`_")
    return "\n\n".join(parts) + "\n"
