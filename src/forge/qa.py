"""Acceptance QA results: parsed from .forge/qa.json (same read-back pattern as
.forge/review.json) — the worker drives the live app in a browser and records
pass/fail per acceptance criterion."""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class QaResult:
    results: tuple = ()      # tuple of {criterion, passed, unverifiable, evidence} dicts
    summary: str = ""
    blocked: dict | None = None   # {kind, question} when the agent needs a human

    @property
    def failures(self) -> list:
        """Criteria that failed in the running app. Excludes `unverifiable`
        ones — a criterion only observable post-merge (external dashboard, real
        PR run) is not a defect a fix turn could address, so it must never gate
        or trigger repair rounds."""
        return [str(r.get("criterion", "")) for r in self.results
                if isinstance(r, dict) and not r.get("passed", False)
                and not r.get("unverifiable", False)]

    @property
    def unverifiable(self) -> list:
        return [str(r.get("criterion", "")) for r in self.results
                if isinstance(r, dict) and r.get("unverifiable", False)
                and not r.get("passed", False)]

    @property
    def checked(self) -> int:
        return len(self.results)


def parse_qa(text: str) -> "QaResult | None":
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    results = tuple(r for r in (d.get("acceptance") or []) if isinstance(r, dict))
    b = d.get("blocked")
    blocked = b if isinstance(b, dict) and b.get("kind") else None
    return QaResult(results=results, summary=str(d.get("summary") or ""), blocked=blocked)
