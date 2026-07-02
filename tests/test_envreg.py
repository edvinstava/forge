from forge.envreg import superseded_run_ids, web_url


def test_web_url():
    assert web_url(5051) == "http://localhost:5051"


def test_superseded_excludes_keeper():
    envs = [{"run_id": "a"}, {"run_id": "b"}, {"run_id": "keep"}]
    assert superseded_run_ids(envs, "keep") == ["a", "b"]


def test_superseded_empty():
    assert superseded_run_ids([], "keep") == []
