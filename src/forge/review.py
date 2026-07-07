"""Pure logic over a worker-produced review and a PR's unified diff: tolerant
parsing, diff-line anchoring, and the GitHub `/reviews` POST payload. No I/O."""
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Comment:
    path: str
    line: int
    side: str
    body: str


@dataclass(frozen=True)
class Review:
    summary: str
    comments: list
    live_check: dict | None = None


_LIVE_STATUSES = ("pass", "fail", "skipped", "blocked")


def _parse_live_check(data: dict):
    """Tolerant parse of the optional live_check object. Returns a normalized
    {status, tested, notes} only when status is a known value; otherwise None
    (old workers and self-review write no live_check)."""
    lc = data.get("live_check")
    if not isinstance(lc, dict):
        return None
    status = str(lc.get("status", "")).lower()
    if status not in _LIVE_STATUSES:
        return None
    raw = lc.get("tested")
    tested = [str(t) for t in raw if t or t == 0] if isinstance(raw, list) else []
    return {"status": status, "tested": tested, "notes": str(lc.get("notes") or "")}


def parse_review(data) -> Review:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return Review("", [])
    if not isinstance(data, dict):
        return Review("", [])
    out = []
    for c in data.get("comments") or []:
        if not isinstance(c, dict):
            continue
        path, line, body = c.get("path"), c.get("line"), c.get("body")
        if not path or not isinstance(line, int) or not body:
            continue
        side = "LEFT" if str(c.get("side", "")).upper() == "LEFT" else "RIGHT"
        out.append(Comment(str(path), line, side, str(body)))
    return Review(str(data.get("summary") or ""), out, _parse_live_check(data))


def diff_line_map(unified_diff: str) -> dict:
    """{path -> set((side, line))} an inline comment may legally anchor to."""
    out: dict = {}
    path = None
    old = new = 0
    for line in (unified_diff or "").splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else (p[2:] if p[:2] in ("b/", "a/") else p)
            if path is not None:
                out.setdefault(path, set())
        elif line.startswith("@@"):
            mo = re.search(r"-(\d+)", line)
            mn = re.search(r"\+(\d+)", line)
            old = int(mo.group(1)) if mo else 0
            new = int(mn.group(1)) if mn else 0
        elif path is not None and line and line[0] in "+- ":
            if line[0] == "+":
                out[path].add(("RIGHT", new)); new += 1
            elif line[0] == "-":
                out[path].add(("LEFT", old)); old += 1
            else:
                out[path].add(("RIGHT", new)); out[path].add(("LEFT", old))
                new += 1; old += 1
    return out


def partition(review: Review, line_map: dict):
    valid, dropped = [], []
    for c in review.comments:
        if (c.side, c.line) in line_map.get(c.path, set()):
            valid.append(c)
        else:
            dropped.append(c)
    return valid, dropped


_LIVE_EMOJI = {"pass": "✅", "fail": "❌", "skipped": "⏭️", "blocked": "🔒"}


def _live_check_section(lc) -> str:
    """Render the live-check block for the top of the review body. Empty string
    when lc is falsy (absent) — old workers and self-review stay compatible."""
    if not lc:
        return ""
    emoji = _LIVE_EMOJI.get(lc.get("status", ""), "")
    out = [f"**Live check:** {emoji} {lc.get('status', '')}".rstrip()]
    out += [f"- {t}" for t in lc.get("tested") or []]
    if lc.get("notes"):
        out.append(lc["notes"])
    return "\n".join(out) + "\n\n"


def build_payload(review: Review, valid, dropped, header: str = "") -> dict:
    body = header + _live_check_section(review.live_check) + (review.summary or "")
    if dropped:
        body += "\n\n---\n**Additional notes (couldn't anchor inline):**\n"
        body += "\n".join(f"- `{c.path}:{c.line}` — {c.body}" for c in dropped)
    return {
        "event": "COMMENT",
        "body": body,
        "comments": [{"path": c.path, "line": c.line, "side": c.side,
                      "body": c.body} for c in valid],
    }


def parse_review_url(stdout: str):
    try:
        return json.loads(stdout).get("html_url")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
