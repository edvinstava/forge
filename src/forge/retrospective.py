"""Parse the retrospective worker's .forge/lessons.json into clean, capped
lesson dicts for the per-repo knowledge overlay (same read-back pattern as
.forge/qa.json / .forge/review.json)."""
import json

MAX_PER_RUN = 8        # a single retrospective may contribute at most this many


def parse_lessons(text: str) -> list:
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    out = []
    for l in (d.get("lessons") or []):
        if not isinstance(l, dict) or not l.get("text"):
            continue
        out.append({"text": str(l["text"])[:300],
                    "kind": str(l.get("kind") or "gotcha"),
                    "evidence": str(l.get("evidence") or "")[:300]})
    return out[:MAX_PER_RUN]
