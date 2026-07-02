"""Detect a Slack list message (a batch) and extract its task lines. Pure logic
so it stays unit-testable without Slack."""
import re

_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")


def parse_batch_lines(text: str) -> list:
    """A multi-line list message → its task lines (bullet/number prefix stripped).
    A batch is >= 2 bulleted/numbered lines. Non-list prose → []. Non-bullet
    lines (e.g. a leading 'on <repo>:' header) are ignored."""
    items = []
    for line in (text or "").splitlines():
        m = _BULLET.match(line)
        if m:
            items.append(m.group(1).strip())
    return items if len(items) >= 2 else []
