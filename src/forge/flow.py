"""Lifecycle states, checkpoint types, and per-surface checkpoint policy for the
supervised-autonomous coworker. Pure: no I/O, no agent calls. (The existing
lifecycle.py owns env reaping/TTL and is unrelated.)"""
import json
from dataclasses import dataclass

# --- lifecycle states ---
IDLE = "idle"
PLANNING = "planning"
AWAITING_APPROVAL = "awaiting_approval"
EXECUTING = "executing"
VERIFYING = "verifying"
REPAIRING = "repairing"
AWAITING_INPUT = "awaiting_input"
PUSHING = "pushing"
PR_OPEN = "pr_open"
DONE = "done"
FAILED = "failed"
SLEEPING = "sleeping"

# --- checkpoint types ---
PLAN_APPROVAL = "plan_approval"
AMBIGUITY = "ambiguity"
REPAIR_ESCALATION = "repair_escalation"
PUSH_APPROVAL = "push_approval"
# The agent is blocked and needs a human (e.g. login credentials). Unlike the
# others this ALWAYS pauses regardless of CheckpointPolicy — there is no valid
# autonomous fallback (you cannot draft-PR your way past a login wall).
NEEDS_INPUT = "needs_input"

# Allowed forward transitions (Phase 1 uses planning/approval/execute/done;
# the rest are declared so later phases don't redefine the map).
# PR_OPEN is the normal green completion terminus (Phase 2); DONE and FAILED are
# also terminal. Note: _TRANSITIONS is advisory/documentation only — it is NOT
# enforced at runtime (set_lifecycle_state is a blind UPDATE; can_transition is
# not called in the live path).
_TRANSITIONS = {
    IDLE: {PLANNING},
    PLANNING: {AWAITING_APPROVAL, EXECUTING, FAILED},
    AWAITING_APPROVAL: {PLANNING, EXECUTING, IDLE, SLEEPING},
    EXECUTING: {VERIFYING, AWAITING_INPUT, DONE, FAILED},
    VERIFYING: {REPAIRING, PUSHING, DONE, FAILED},
    REPAIRING: {VERIFYING, AWAITING_INPUT, FAILED},
    AWAITING_INPUT: {EXECUTING, REPAIRING, IDLE, SLEEPING},
    PUSHING: {PR_OPEN, FAILED},
    SLEEPING: {PLANNING, EXECUTING, AWAITING_APPROVAL, AWAITING_INPUT},
}


def can_transition(frm: str, to: str) -> bool:
    return to in _TRANSITIONS.get(frm, set())


@dataclass(frozen=True)
class CheckpointPolicy:
    active: frozenset[str]

    @classmethod
    def for_cli(cls, auto: bool = False) -> "CheckpointPolicy":
        return cls(frozenset() if auto else frozenset({PLAN_APPROVAL}))

    @classmethod
    def for_web(cls) -> "CheckpointPolicy":
        return cls(frozenset({PLAN_APPROVAL, AMBIGUITY}))

    @classmethod
    def for_slack(cls) -> "CheckpointPolicy":
        # Slack runs autonomously by default: no plan-approval gate ("just spin it
        # up and figure it out"), but still stop to ask when the plan itself is
        # unsure — open questions surface as an AMBIGUITY checkpoint. A verify/QA
        # bottom-out still escalates (see _execute); autonomy never pushes red.
        return cls(frozenset({AMBIGUITY}))

    def gates(self, ctype: str) -> bool:
        return ctype in self.active

    def to_json(self) -> str:
        return json.dumps(sorted(self.active))

    @classmethod
    def from_json(cls, s: str) -> "CheckpointPolicy":
        return cls(frozenset(json.loads(s) if s else []))
