from forge.plan import Plan, parse_plan


def test_parse_full_plan():
    p = parse_plan('{"goal":"Add login","steps":[{"id":1,"intent":"form"}],'
                   '"acceptance":["can log in"],"assumptions":["next.js"],'
                   '"open_questions":[],"risk":"low"}')
    assert p.goal == "Add login"
    assert p.acceptance == ("can log in",)
    assert p.risk == "low"
    assert not p.has_open_questions


def test_parse_tolerates_missing_optional_fields():
    p = parse_plan('{"goal":"x"}')
    assert p.goal == "x"
    assert p.steps == () and p.acceptance == () and p.open_questions == ()
    assert p.risk == "unknown"


def test_open_questions_flagged():
    p = parse_plan('{"goal":"x","open_questions":["which DB?"]}')
    assert p.has_open_questions


def test_invalid_json_returns_none():
    assert parse_plan("not json") is None


def test_missing_goal_returns_none():
    assert parse_plan('{"steps":[]}') is None


def test_to_markdown_contains_goal_and_questions():
    md = parse_plan('{"goal":"x","open_questions":["q1"]}').to_markdown()
    assert "x" in md and "q1" in md
