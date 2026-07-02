from forge.qa import QaResult, parse_qa


def test_parse_qa_failures_and_checked():
    q = parse_qa('{"acceptance":[{"criterion":"login works","passed":true},'
                 '{"criterion":"logout works","passed":false,"evidence":"500"}],'
                 '"summary":"1/2"}')
    assert q.checked == 2
    assert q.failures == ["logout works"]
    assert q.summary == "1/2"


def test_parse_qa_all_pass_no_failures():
    q = parse_qa('{"acceptance":[{"criterion":"x","passed":true}]}')
    assert q.failures == []


def test_parse_qa_invalid_returns_none():
    assert parse_qa("not json") is None
    assert parse_qa("[1,2,3]") is None      # non-dict


def test_parse_qa_missing_passed_counts_as_fail():
    q = parse_qa('{"acceptance":[{"criterion":"x"}]}')   # no "passed" → falsy → fail
    assert q.failures == ["x"]


def test_parse_qa_reads_blocked_object():
    q = parse_qa('{"acceptance": [], "summary": "s", '
                 '"blocked": {"kind": "needs_credentials", "question": "which login?"}}')
    assert q.blocked == {"kind": "needs_credentials", "question": "which login?"}


def test_parse_qa_blocked_absent_or_malformed_is_none():
    assert parse_qa('{"acceptance": []}').blocked is None
    assert parse_qa('{"acceptance": [], "blocked": {"question": "x"}}').blocked is None  # no kind
