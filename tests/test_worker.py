import json
from forge.worker import parse_worker_result

SPIKE = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "num_turns": 5, "duration_ms": 11198, "total_cost_usd": 0.0727632,
    "usage": {"input_tokens": 6, "output_tokens": 382,
              "cache_read_input_tokens": 86714, "cache_creation_input_tokens": 6733},
    "session_id": "abc", "result": "Fixed the bug.",
})


def test_parses_success():
    r = parse_worker_result(SPIKE)
    assert r.ok is True
    assert r.num_turns == 5
    assert r.duration_ms == 11198
    assert r.total_cost_usd == 0.0727632
    assert r.input_tokens == 6 and r.output_tokens == 382
    assert r.session_id == "abc"
    assert r.auth_error is False


def test_unparseable_is_auth_error():
    r = parse_worker_result("")
    assert r.ok is False
    assert r.auth_error is True


def test_error_subtype_not_ok():
    r = parse_worker_result(json.dumps({"subtype": "error_during_execution", "is_error": True}))
    assert r.ok is False


def _failed(text):
    return json.dumps({"subtype": "error_during_execution", "is_error": True,
                       "result": text, "session_id": "s", "usage": {}})


def test_auth_error_not_triggered_by_task_words():
    # A non-success turn whose result text merely mentions a login/credit FEATURE
    # must not be misreported as a Claude auth/usage problem.
    assert parse_worker_result(_failed("Added a login page but a test crashed")).auth_error is False
    assert parse_worker_result(_failed("Wired up the credit calculation, then hit a bug")).auth_error is False


def test_auth_error_detects_real_auth_failures():
    for msg in ("Invalid API key · Please run /login",
                "Credit balance is too low",
                "You have hit your usage limit",
                "OAuth token expired",
                "401 Unauthorized"):
        assert parse_worker_result(_failed(msg)).auth_error is True, msg
