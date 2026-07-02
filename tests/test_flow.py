from forge import flow


def test_cli_policy_gates_plan_unless_auto():
    assert flow.CheckpointPolicy.for_cli(auto=False).gates(flow.PLAN_APPROVAL)
    assert not flow.CheckpointPolicy.for_cli(auto=True).gates(flow.PLAN_APPROVAL)


def test_web_policy_gates_plan_and_ambiguity_not_push():
    p = flow.CheckpointPolicy.for_web()
    assert p.gates(flow.PLAN_APPROVAL)
    assert p.gates(flow.AMBIGUITY)
    assert not p.gates(flow.PUSH_APPROVAL)


def test_slack_policy_is_autonomous_gating_only_ambiguity():
    # Slack runs autonomously by default: no plan-approval gate ("just figure it
    # out"), but still asks when the plan is unsure (open questions -> AMBIGUITY).
    # Never gates push.
    p = flow.CheckpointPolicy.for_slack()
    assert not p.gates(flow.PLAN_APPROVAL)
    assert p.gates(flow.AMBIGUITY)
    assert not p.gates(flow.PUSH_APPROVAL)


def test_policy_json_roundtrip():
    p = flow.CheckpointPolicy.for_slack()
    assert flow.CheckpointPolicy.from_json(p.to_json()).active == p.active


def test_transitions():
    assert flow.can_transition(flow.IDLE, flow.PLANNING)
    assert flow.can_transition(flow.AWAITING_APPROVAL, flow.EXECUTING)
    assert flow.can_transition(flow.EXECUTING, flow.DONE)
    assert not flow.can_transition(flow.DONE, flow.PLANNING)
    assert not flow.can_transition(flow.IDLE, flow.PR_OPEN)
