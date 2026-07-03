import pytest

from forge.knowledge import LESSONS_CAP, KnowledgeStore, merge_overlay, validate


def test_validate_rejects_unknown_keys_and_bad_pm():
    with pytest.raises(ValueError):
        validate({"pkg_manager": "cargo"})
    with pytest.raises(ValueError):
        validate({"bogus": 1})


def test_validate_passes_minimal_overlay():
    out = validate({"pkg_manager": "bun", "apt": ["libnss3"]})
    assert out["pkg_manager"] == "bun"
    assert out["apt"] == ["libnss3"]


def test_merge_overlay_delta_wins_and_apt_unions():
    base = {"pkg_manager": "npm", "apt": ["libnss3"]}
    delta = {"pkg_manager": "bun", "apt": ["libglib2.0-0"]}
    m = merge_overlay(base, delta)
    assert m["pkg_manager"] == "bun"
    assert sorted(m["apt"]) == ["libglib2.0-0", "libnss3"]


def test_store_roundtrip_keyed_by_slug(tmp_path):
    s = KnowledgeStore(tmp_path)
    assert s.load("acme/webapp") is None
    s.save("acme/webapp", {"pkg_manager": "bun"})
    assert (tmp_path / "acme" / "webapp.yml").is_file()
    assert s.load("acme/webapp")["pkg_manager"] == "bun"


def test_merge_save_accumulates(tmp_path):
    s = KnowledgeStore(tmp_path)
    s.save("o/r", {"pkg_manager": "bun"})
    merged = s.merge_save("o/r", {"apt": ["libnss3"]})
    assert merged["pkg_manager"] == "bun" and merged["apt"] == ["libnss3"]
    assert s.load("o/r")["apt"] == ["libnss3"]


def test_validate_accepts_lessons():
    out = validate({"lessons": [{"text": "use pnpm", "kind": "build"}]})
    assert out["lessons"][0]["text"] == "use pnpm"


def test_validate_rejects_lesson_without_text():
    with pytest.raises(ValueError):
        validate({"lessons": [{"kind": "build"}]})   # no text


def test_merge_overlay_unions_lessons_dedup_by_text():
    base = {"lessons": [{"text": "use pnpm"}]}
    delta = {"lessons": [{"text": "use pnpm"}, {"text": "tests need DISPLAY"}]}
    m = merge_overlay(base, delta)
    texts = [l["text"] for l in m["lessons"]]
    assert texts == ["use pnpm", "tests need DISPLAY"]   # dedup, order-stable


def test_merge_overlay_caps_lessons():
    base = {"lessons": [{"text": f"l{i}"} for i in range(LESSONS_CAP)]}
    m = merge_overlay(base, {"lessons": [{"text": "newest"}]})
    assert len(m["lessons"]) == LESSONS_CAP
    assert m["lessons"][-1]["text"] == "newest"          # most-recent kept
    assert m["lessons"][0]["text"] == "l1"               # oldest dropped


def test_qa_credentials_valid_and_unknown_key_still_rejected():
    ov = {"qa_credentials": [{"role": "admin", "username": "a@b.c", "password": "p"}]}
    assert validate(ov) == ov
    with pytest.raises(ValueError):
        validate({"bogus": 1})


def test_qa_credentials_must_be_list_of_user_pass_dicts():
    with pytest.raises(ValueError):
        validate({"qa_credentials": [{"role": "admin"}]})          # no user/pass
    with pytest.raises(ValueError):
        validate({"qa_credentials": "user::pass"})                 # not a list


def test_merge_qa_credentials_by_role_replaces_same_role_keeps_others():
    base = {"qa_credentials": [
        {"role": "admin", "username": "old@x", "password": "o"},
        {"role": "user", "username": "u@x", "password": "u"}]}
    delta = {"qa_credentials": [
        {"role": "admin", "username": "new@x", "password": "n"}]}
    merged = merge_overlay(base, delta)
    assert merged["qa_credentials"] == [
        {"role": "admin", "username": "new@x", "password": "n"},
        {"role": "user", "username": "u@x", "password": "u"}]


def test_validate_accepts_synthesis_keys():
    ov = {"image": "python:3.12-slim",
          "setup_cmds": ["pip install -e .", "python manage.py migrate"],
          "dev_cmd": "python manage.py runserver 0.0.0.0:8000",
          "web_port": 8000,
          "services": {"db": {"image": "postgres:16",
                              "environment": {"POSTGRES_PASSWORD": "forge"}},
                       "cache": {"image": "redis:7",
                                 "command": "redis-server --appendonly no"}}}
    assert validate(ov) == ov


def test_validate_rejects_bad_image_and_setup_cmds():
    with pytest.raises(ValueError):
        validate({"image": ""})                       # empty
    with pytest.raises(ValueError):
        validate({"image": "python:3 12"})            # whitespace
    with pytest.raises(ValueError):
        validate({"setup_cmds": "pip install ."})     # not a list
    with pytest.raises(ValueError):
        validate({"setup_cmds": [1]})                 # not strings


def test_validate_services_containment():
    # Only image/environment/command allowed: no volumes (host mounts), no
    # ports (nothing published), no privileged. Names can't collide with the
    # web/forge services Forge injects.
    with pytest.raises(ValueError):
        validate({"services": {"db": {"image": "postgres:16",
                                      "volumes": ["/:/host"]}}})
    with pytest.raises(ValueError):
        validate({"services": {"db": {"image": "postgres:16",
                                      "ports": ["5432:5432"]}}})
    with pytest.raises(ValueError):
        validate({"services": {"db": {"environment": {}}}})   # image required
    with pytest.raises(ValueError):
        validate({"services": {"web": {"image": "postgres:16"}}})
    with pytest.raises(ValueError):
        validate({"services": {"forge": {"image": "postgres:16"}}})
    with pytest.raises(ValueError):
        validate({"services": {"Bad Name": {"image": "postgres:16"}}})
    with pytest.raises(ValueError):
        validate({"services": [{"image": "postgres:16"}]})    # not a mapping


def test_merge_overlay_services_and_setup_cmds_replace_wholesale():
    # Partial merges of ordered command lists / service maps are ambiguous —
    # the newest description of the environment wins outright.
    base = {"setup_cmds": ["pip install -e ."],
            "services": {"db": {"image": "postgres:15"}}}
    delta = {"setup_cmds": ["pip install -r requirements.txt"],
             "services": {"cache": {"image": "redis:7"}}}
    m = merge_overlay(base, delta)
    assert m["setup_cmds"] == ["pip install -r requirements.txt"]
    assert m["services"] == {"cache": {"image": "redis:7"}}


def test_user_lessons_survive_the_cap():
    # A teammate's explicit instruction outranks auto-learned lessons: when the
    # cap trims, only retrospective lessons are evicted.
    from forge.knowledge import LESSONS_CAP, merge_overlay
    base = {"lessons": ([{"text": "user rule", "kind": "user"}]
                        + [{"text": f"auto {i}", "kind": "gotcha"}
                           for i in range(LESSONS_CAP)])}
    merged = merge_overlay(base, {"lessons": [{"text": "auto new", "kind": "gotcha"}]})
    texts = [l["text"] for l in merged["lessons"]]
    assert len(texts) == LESSONS_CAP
    assert "user rule" in texts          # pinned
    assert "auto new" in texts           # newest auto kept
    assert "auto 0" not in texts         # oldest auto evicted


def test_validate_web_port_env_and_dev_cmd_types():
    # These feed int()/dict()/f-strings during synthesis — reject garbage at
    # the validation boundary (probe output, env.yml) so a bad agent emission
    # degrades to "learned nothing" instead of crashing provisioning.
    assert validate({"web_port": 8000})["web_port"] == 8000
    assert validate({"web_port": "8000"})["web_port"] == "8000"   # digit-str ok
    with pytest.raises(ValueError):
        validate({"web_port": "eight thousand"})
    with pytest.raises(ValueError):
        validate({"web_port": 0})
    with pytest.raises(ValueError):
        validate({"web_port": 70000})
    with pytest.raises(ValueError):
        validate({"env": ["FOO=bar"]})                # list form rejected
    with pytest.raises(ValueError):
        validate({"env": {"FOO": {"nested": 1}}})     # non-scalar value
    assert validate({"env": {"FOO": "bar", "N": 3}})["env"]["N"] == 3
    with pytest.raises(ValueError):
        validate({"dev_cmd": ["python", "app.py"]})   # must be a string
