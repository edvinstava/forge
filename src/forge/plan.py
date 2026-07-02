"""The agent's pre-execution plan: parsed from .forge/plan.json (same read-back
pattern as .forge/review.json) and surfaced to the human for approval."""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple = ()
    acceptance: tuple = ()
    assumptions: tuple = ()
    open_questions: tuple = ()
    risk: str = "unknown"

    @property
    def has_open_questions(self) -> bool:
        return len(self.open_questions) > 0

    def to_dict(self) -> dict:
        return {"goal": self.goal, "steps": list(self.steps),
                "acceptance": list(self.acceptance),
                "assumptions": list(self.assumptions),
                "open_questions": list(self.open_questions), "risk": self.risk}

    def to_markdown(self) -> str:
        lines = [f"**Plan:** {self.goal}", f"_risk: {self.risk}_", ""]
        for i, s in enumerate(self.steps, 1):
            intent = s.get("intent", "") if isinstance(s, dict) else str(s)
            lines.append(f"{i}. {intent}")
        if self.acceptance:
            lines += ["", "**Acceptance:**"] + [f"- {a}" for a in self.acceptance]
        if self.open_questions:
            lines += ["", "**Open questions:**"] + [f"- {q}" for q in self.open_questions]
        return "\n".join(lines)


def parse_plan(text: str) -> "Plan | None":
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict) or not d.get("goal"):
        return None
    return Plan(
        goal=str(d["goal"]),
        steps=tuple(d.get("steps") or ()),
        acceptance=tuple(d.get("acceptance") or ()),
        assumptions=tuple(d.get("assumptions") or ()),
        open_questions=tuple(d.get("open_questions") or ()),
        risk=str(d.get("risk") or "unknown"),
    )
